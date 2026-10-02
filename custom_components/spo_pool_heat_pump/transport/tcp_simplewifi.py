"""TCP client for the Simple-WiFi module's own framed protocol.

Not Modbus RTU, not a DR164-style transparent serial gateway: the module
speaks a fixed-size, marker-prefixed binary protocol directly over a
dedicated TCP socket (no shared RS-485 bus, no idle-gap framing, no DE
collision concerns). It accepts exactly one client at a time and may reset
the connection on purpose when a second one connects — never run two of
these (or a second config entry) against the same module.

Frames are always 50 bytes, starting with the marker b"\\xAA\\x5A\\xB1". The
module may push them on its own every ~0.6s, or it may only answer an
explicit read request — this client tolerates either by sending that
request whenever the line has gone quiet for a few seconds (see
KICK_AFTER_S). This client only does byte-level framing; CRC validation and
all protocol semantics are the driver's job (SimpleWifiDriver), matching how
TcpRtuClient leaves Modbus-frame validation to its drivers.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

ConnectionCallback = Callable[[bool], None]
FrameCallback = Callable[[bytes], "Awaitable[None] | None"]

_LOGGER = logging.getLogger(__name__)

MARKER = b"\xaa\x5a\xb1"
FRAME_LEN = 50
# AA 5A B1 01 + 46 zero bytes: the one read request this protocol defines.
READ_REQUEST = MARKER + bytes([0x01]) + bytes(46)

# Device transmits on its own roughly every 0.6s when it is pushing; these
# thresholds assume that cadence. Decided with the user: kick after 3s of
# silence (repeat every 3s while still silent), give up on the socket and
# redial after 10s silence. The entity-availability watchdog (STALE_SECONDS,
# 15s) lives in the coordinator and is unaffected by this module.
KICK_AFTER_S = 3.0
RECONNECT_AFTER_S = 10.0
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 30.0
WATCHDOG_TICK_S = 0.5


class SimpleWifiTcpClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._on_frame: FrameCallback | None = None
        self._on_connection: ConnectionCallback | None = None
        self._stop = asyncio.Event()
        self._last_rx = 0.0
        self._last_kick = 0.0
        self._unavailable_logged = False
        self._conn_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def start(
        self,
        on_frame: FrameCallback,
        on_connection: ConnectionCallback | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._on_connection = on_connection
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="spo_pool_heat_pump_simplewifi_tcp")
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name="spo_pool_heat_pump_simplewifi_watchdog"
        )

    @staticmethod
    async def probe(host: str, port: int, timeout: float = 5.0) -> None:
        """Open and close a TCP connection. Raises OSError if the module is down."""
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except TimeoutError as err:
            raise OSError(f"timed out connecting to {host}:{port}") from err
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._watchdog_task, self._task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._watchdog_task = None
        await self._close()

    async def send(self, frame: bytes, *, solicited: bool = False) -> None:
        del solicited  # no shared-bus collision concept on a dedicated socket
        async with self._write_lock:
            if not self.connected:
                raise ConnectionError("Simple-WiFi TCP not connected")
            assert self._writer is not None
            self._writer.write(frame)
            await self._writer.drain()

    async def reconnect(self) -> None:
        """Drop the socket so the run loop redials right away."""
        await self._close()

    def _notify_connection(self, connected: bool) -> None:
        if self._on_connection:
            self._on_connection(connected)

    def _note_unavailable(self, err: BaseException) -> None:
        if self._unavailable_logged:
            _LOGGER.debug("Simple-WiFi TCP drop (%s); reconnecting", err)
            return
        _LOGGER.info("Simple-WiFi module at %s:%s unavailable: %s", self.host, self.port, err)
        self._unavailable_logged = True
        self._notify_connection(False)

    def _note_reconnected(self) -> None:
        if self._unavailable_logged:
            _LOGGER.info("Simple-WiFi module at %s:%s reconnected", self.host, self.port)
            self._unavailable_logged = False
        self._notify_connection(True)

    def _set_nodelay(self) -> None:
        writer = self._writer
        extra = getattr(writer, "get_extra_info", None)
        sock = extra("socket") if extra else None
        if sock is None:
            return
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            _LOGGER.debug("TCP_NODELAY not set on %s:%s", self.host, self.port, exc_info=True)

    async def _close(self) -> None:
        async with self._conn_lock:
            writer, self._writer = self._writer, None
            self._reader = None
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

    async def _run(self) -> None:
        backoff = BACKOFF_INITIAL_S
        while not self._stop.is_set():
            try:
                _LOGGER.debug("Simple-WiFi connecting to %s:%s", self.host, self.port)
                async with self._conn_lock:
                    self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                    self._set_nodelay()
                _LOGGER.debug("Simple-WiFi connected to %s:%s", self.host, self.port)
                now = asyncio.get_running_loop().time()
                self._last_rx = now
                self._last_kick = 0.0
                backoff = BACKOFF_INITIAL_S
                self._note_reconnected()
                await self._read_until_closed()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Simple-WiFi connection to %s:%s failed (%s); backoff %.1fs",
                    self.host,
                    self.port,
                    err,
                    backoff,
                )
                self._note_unavailable(err)
            await self._close()
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _read_until_closed(self) -> None:
        buf = bytearray()
        assert self._reader is not None
        while not self._stop.is_set():
            chunk = await self._reader.read(4096)
            if not chunk:
                raise ConnectionError("peer closed")
            self._last_rx = asyncio.get_running_loop().time()
            buf.extend(chunk)
            await self._drain_frames(buf)

    async def _drain_frames(self, buf: bytearray) -> None:
        while True:
            idx = buf.find(MARKER)
            if idx < 0:
                # Keep a short tail: the marker itself may be split across
                # two reads from the socket.
                keep = min(len(buf), len(MARKER) - 1)
                del buf[: len(buf) - keep]
                return
            if idx > 0:
                del buf[:idx]
            if len(buf) < FRAME_LEN:
                return
            frame = bytes(buf[:FRAME_LEN])
            del buf[:FRAME_LEN]
            if self._on_frame:
                result = self._on_frame(frame)
                if asyncio.iscoroutine(result):
                    await result

    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=WATCHDOG_TICK_S)
                return
            except TimeoutError:
                pass
            if not self.connected:
                continue
            now = asyncio.get_running_loop().time()
            silence = now - self._last_rx
            if silence >= RECONNECT_AFTER_S:
                _LOGGER.debug("Simple-WiFi silent for %.1fs; redialing", silence)
                await self._close()
                continue
            if silence >= KICK_AFTER_S and (now - self._last_kick) >= KICK_AFTER_S:
                self._last_kick = now
                try:
                    await self.send(READ_REQUEST)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Simple-WiFi kick send failed", exc_info=True)
