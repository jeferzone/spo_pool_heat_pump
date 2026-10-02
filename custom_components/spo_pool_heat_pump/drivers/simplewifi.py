"""Driver for the Simple-WiFi module's own framed TCP protocol.

Not Modbus RTU. A 50-byte read-reply frame is
``AA 5A B1 | type | page | MAC(6) | body | CRC16-LE``; a write frame is
``AA 5A B1 83 01 | MAC(6) | body(22) | CRC16-LE`` and always carries a full
copy of page 0x80/1 with only the intended byte(s) patched — never a partial
page. The module may push frames on its own (~0.6s cadence) or only answer
an explicit read request; this driver does not care which, since the
transport (transport/tcp_simplewifi.py) handles that by kicking a request
whenever the line goes quiet. ``is_push = True`` because, either way, state
only ever changes here in reaction to a frame handed up by the transport.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..modbus_rtu import crc16
from ..profiles import encode_value, lookup_write_spec
from .base import HeatPumpDriver, HeatPumpState
from .decode_simplewifi import apply_simplewifi_map
from .pending import PendingWrites

_LOGGER = logging.getLogger(__name__)

FRAME_LEN = 50
MARKER = b"\xaa\x5a\xb1"
WRITE_BODY_LEN = 22
_PAGE_80_1 = (0x80, 1)
_PAGE_D0_1 = (0xD0, 1)
# Pages 80/6 and 80/7 carry the Wi-Fi password in plaintext: never cache,
# never decode, never let them reach self._pages even transiently.
_SECRET_PAGES = frozenset({(0x80, 6), (0x80, 7)})

_KNOWN_PAGE0_GROUPS = frozenset(
    {
        (0x80, 0x21),
        (0x80, 0x44),
        (0x80, 0x45),
        (0x80, 0x46),
        (0x80, 0x48),
        (0x80, 0x50),
        (0x80, 0x52),
        (0xD0, 0x4F),
        (0xD0, 0x53),
        (0xD0, 0x54),
        (0xD0, 0x23),
        (0xD0, 0x24),
    }
)
_MAX_UNKNOWN_PAGE0_GROUPS = 32

_CONFIRM_RETRIES = 8
_CONFIRM_WAIT_S = 1.0
_BYTE15_FALLBACK = 0x02


class WriteNotConfirmedError(RuntimeError):
    """The pump never echoed the written value within the retry window."""


def _find_crc_pos(frame: bytes) -> int | None:
    """Where the CRC sits inside a 50-byte frame.

    Payload length is not fixed (it varies by frame type/page/group); the
    CRC sits right after the real payload and the rest of the 50-byte frame
    is zero-padded. Ported from the validated reference
    (docs/astral/phnix_poll.py::crc_pos), which found this by scanning real
    captures rather than assuming a fixed tail position.
    """
    for pos in range(12, FRAME_LEN - 1):
        candidate = frame[pos : pos + 2]
        if candidate == b"\x00\x00":
            continue
        if crc16(frame[:pos]) == int.from_bytes(candidate, "little"):
            return pos
    return None


class SimpleWifiDriver(HeatPumpDriver):
    is_push = True

    def __init__(
        self,
        profile: dict[str, Any],
        send: Callable,
        on_state: Callable[[HeatPumpState], None] | None = None,
    ) -> None:
        self.profile = profile
        self._send = send
        self._on_state = on_state
        self.state = HeatPumpState()
        self.pending = PendingWrites(profile)
        self._pages: dict[tuple[int, int], bytes] = {}
        self._groups: dict[tuple[int, int], bytes] = {}
        self._mac: bytes | None = None
        self._write_lock = asyncio.Lock()
        self._last_write_body: bytes | None = None
        self._first_frame_logged = False
        self._available_logged = False

    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None

    def is_live_frame(self, frame: bytes) -> bool:
        """Marker + fixed length + a CRC that validates somewhere inside it.

        Not Modbus RTU, so the base class's parse_frame-based check never
        matches this link — overridden here with this protocol's own notion
        of "a real frame", so the coordinator's stale-bus watchdog does not
        starve on this driver the way it would on the inherited default.
        """
        return (
            len(frame) == FRAME_LEN
            and frame[:3] == MARKER
            and _find_crc_pos(frame) is not None
        )

    def handle_frame(self, frame: bytes) -> bytes | None:
        if not self.is_live_frame(frame):
            return None
        typ, page = frame[3], frame[4]
        is_secret = (typ, page) in _SECRET_PAGES
        if not self._first_frame_logged:
            self._first_frame_logged = True
            # Never name the page here if it is a secret one (80/6, 80/7):
            # the standing rule is to never log anything about those frames,
            # not just their body.
            if is_secret:
                _LOGGER.debug("Simple-WiFi first frame received")
            else:
                _LOGGER.debug("Simple-WiFi first frame received (type=0x%02X page=%d)", typ, page)
        if is_secret:
            return None
        if self._mac is None:
            self._mac = bytes(frame[5:11])
        body = bytes(frame[11:])
        if page == 0:
            self._absorb_page0(typ, body)
        else:
            if (typ, page) == _PAGE_80_1 and _PAGE_80_1 not in self._pages:
                _LOGGER.debug("Simple-WiFi first 80/1 frame received")
            elif (typ, page) == _PAGE_D0_1 and _PAGE_D0_1 not in self._pages:
                _LOGGER.debug("Simple-WiFi first D0/1 frame received")
            self._pages[(typ, page)] = body
        self._publish()
        return None

    def _absorb_page0(self, typ: int, body: bytes) -> None:
        if len(body) < 4:
            return
        group_id, header = body[0], body[2:4]
        if header[:1] != b"\x01":
            # The alternating "no data" header variant (e.g. "20 3E") — the
            # same group id at the same moment, carrying no real payload.
            return
        key = (typ, group_id)
        if key not in self._groups and key not in _KNOWN_PAGE0_GROUPS:
            unknown = sum(1 for k in self._groups if k not in _KNOWN_PAGE0_GROUPS)
            if unknown >= _MAX_UNKNOWN_PAGE0_GROUPS:
                return
        self._groups[key] = body

    def _publish(self) -> None:
        state = apply_simplewifi_map(self.profile, self._pages, self._groups)
        if state.available and not self._available_logged:
            self._available_logged = True
            _LOGGER.debug("Simple-WiFi available=True (80/1 and D0/1 both seen)")
        self.pending.overlay(state)
        self.state = state
        if self._on_state:
            self._on_state(state)

    def _build_write_frame(self, body: bytes) -> bytes:
        mac = self._mac or bytes(6)
        payload = bytes([0xAA, 0x5A, 0xB1, 0x83, 0x01]) + mac + body
        return payload + crc16(payload).to_bytes(2, "little")

    async def _wait_for_page(self, key: tuple[int, int], timeout: float) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            body = self._pages.get(key)
            if body is not None:
                return body
            if loop.time() >= deadline:
                raise RuntimeError(f"no {key} frame received yet")
            await asyncio.sleep(0.2)

    async def _confirm(self, byte: int, expected: int) -> bool:
        """Did the pump's own copy of 80/1 come back showing our byte?

        Checks only the byte this call wrote, not the whole page: a second
        write queued right behind this one (see write_register) legitimately
        changes a different byte in the same page before this write's echo
        arrives, and that must not look like our write failing.
        """
        for _ in range(_CONFIRM_RETRIES):
            await asyncio.sleep(_CONFIRM_WAIT_S)
            body = self._pages.get(_PAGE_80_1)
            if body is not None and byte < len(body) and body[byte] == expected:
                return True
        return False

    async def _send_body(self, body: bytes) -> None:
        result = self._send(self._build_write_frame(body))
        if asyncio.iscoroutine(result):
            await result

    async def write_register(self, name: str, value: int | float) -> None:
        spec = lookup_write_spec(self.profile, name)
        if not spec.get("write") or "byte" not in spec:
            raise KeyError(name)
        byte = int(spec["byte"])
        encoded = encode_value(spec, value, self.profile.get("enums") or {}) & 0xFF

        async with self._write_lock:
            # Base the new frame on the most recent write's own target, not
            # just the last confirmed page: a second write (e.g. setpoint
            # right after power) must carry the first write's byte forward
            # even before the pump has echoed it back. The lock only guards
            # this quick read-patch-send step, not the ~8s confirm wait below
            # — otherwise a second write would stall behind the first one's
            # whole confirmation window instead of coalescing with it.
            base = self._last_write_body or self._pages.get(_PAGE_80_1)
            if base is None:
                base = await self._wait_for_page(_PAGE_80_1, timeout=5.0)
            body = bytearray(base[:WRITE_BODY_LEN].ljust(WRITE_BODY_LEN, b"\x00"))
            body[byte] = encoded
            sent_body = bytes(body)
            self._last_write_body = sent_body
            self.pending.mark(name, encoded)
            await self._send_body(sent_body)

        try:
            if await self._confirm(byte, encoded):
                return
            # Rebuild the retry from whatever is the *current* last-write
            # body, not the one this call originally sent: a write for a
            # different field may have gone out (and even been confirmed) in
            # the meantime, and the retry must carry that forward rather than
            # resurrect the stale snapshot captured before it. Re-stamp our
            # own byte on top in case nothing else has touched this body yet.
            async with self._write_lock:
                current = self._last_write_body or sent_body
                retry_body = bytearray(current)
                retry_body[byte] = encoded
                retry_body[15] = _BYTE15_FALLBACK if retry_body[15] != _BYTE15_FALLBACK else 0x00
                retry_body = bytes(retry_body)
                self._last_write_body = retry_body
                await self._send_body(retry_body)
            if await self._confirm(byte, encoded):
                return
            raise WriteNotConfirmedError(name)
        except Exception:
            self.pending.discard(name)
            raise

    async def set_power(self, on: bool) -> None:
        await self.write_register("power", on)

    async def set_mode(self, mode: str) -> None:
        if mode != "heat":
            raise ValueError(f"{self.profile['identity']['id']} only supports heat mode")

    async def set_setpoint(self, celsius: float, which: str | None = None) -> None:
        del which  # single mode, single setpoint
        await self.write_register("setpoint", celsius)

    async def set_silent(self, on: bool) -> None:
        await self.write_register("silent", on)
