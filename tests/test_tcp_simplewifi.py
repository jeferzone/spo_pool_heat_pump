"""SimpleWifiTcpClient: marker+fixed-length framing, kick, and reconnection."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from spo_pool_heat_pump.modbus_rtu import crc16
from spo_pool_heat_pump.transport.tcp_simplewifi import READ_REQUEST, SimpleWifiTcpClient


def _frame(typ: int, page: int, body: bytes = b"") -> bytes:
    f = bytes([0xAA, 0x5A, 0xB1, typ, page]) + bytes(6) + body.ljust(22, b"\x00")
    f += crc16(f).to_bytes(2, "little")
    return f.ljust(50, b"\x00")


def test_reassembles_a_frame_split_across_tcp_segments() -> None:
    async def run() -> None:
        frame = _frame(0x80, 1, bytes([1]))

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"\x11\x22")  # noise before the marker, no frame boundary
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(frame[:20])  # first half of a real frame
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(frame[20:])  # second half, in a separate TCP write
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        got: list[bytes] = []

        async def on_frame(f: bytes) -> None:
            got.append(f)

        client = SimpleWifiTcpClient("127.0.0.1", port)
        with patch("spo_pool_heat_pump.transport.tcp_simplewifi.KICK_AFTER_S", 999.0):
            await client.start(on_frame)
            await asyncio.sleep(0.15)
            await client.stop()
        server.close()
        await server.wait_closed()
        assert got == [frame]

    asyncio.run(run())


def test_discards_garbage_with_no_marker_before_a_real_frame() -> None:
    async def run() -> None:
        frame = _frame(0xD0, 1)

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"\x00\x01\x02\x03\x04")  # noise, no marker byte sequence at all
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(frame)
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        got: list[bytes] = []

        async def on_frame(f: bytes) -> None:
            got.append(f)

        client = SimpleWifiTcpClient("127.0.0.1", port)
        with patch("spo_pool_heat_pump.transport.tcp_simplewifi.KICK_AFTER_S", 999.0):
            await client.start(on_frame)
            await asyncio.sleep(0.08)
            await client.stop()
        server.close()
        await server.wait_closed()
        assert got == [frame]

    asyncio.run(run())


def test_sends_kick_request_after_silence() -> None:
    async def run() -> None:
        received: list[bytes] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(50)
            received.append(data)
            await asyncio.sleep(0.2)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = SimpleWifiTcpClient("127.0.0.1", port)
        with (
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.KICK_AFTER_S", 0.03),
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.RECONNECT_AFTER_S", 999.0),
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.WATCHDOG_TICK_S", 0.01),
        ):
            await client.start(lambda f: None)
            await asyncio.sleep(0.1)
            await client.stop()
        server.close()
        await server.wait_closed()
        assert received and received[0] == READ_REQUEST

    asyncio.run(run())


def test_redials_after_silence() -> None:
    async def run() -> None:
        connections = 0
        done = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal connections
            connections += 1
            if connections >= 2:
                done.set()
            try:
                await reader.read(4096)
            except (ConnectionError, asyncio.CancelledError):
                pass

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = SimpleWifiTcpClient("127.0.0.1", port)
        with (
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.KICK_AFTER_S", 999.0),
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.RECONNECT_AFTER_S", 0.03),
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.WATCHDOG_TICK_S", 0.01),
            patch("spo_pool_heat_pump.transport.tcp_simplewifi.BACKOFF_INITIAL_S", 0.01),
        ):
            await client.start(lambda f: None)
            await asyncio.wait_for(done.wait(), timeout=1.0)
            await client.stop()
        server.close()
        await server.wait_closed()
        assert connections >= 2

    asyncio.run(run())


def test_send_without_connection_raises() -> None:
    async def run() -> None:
        client = SimpleWifiTcpClient("127.0.0.1", 1)  # nothing listens on port 1
        with pytest.raises(ConnectionError):
            await client.send(READ_REQUEST)

    asyncio.run(run())


def test_probe_raises_when_nothing_listens() -> None:
    async def run() -> None:
        with pytest.raises(OSError):
            await SimpleWifiTcpClient.probe("10.0.0.8", 60000, timeout=0.05)

    asyncio.run(run())


def test_logs_connecting_and_connected(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(0.2)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = SimpleWifiTcpClient("127.0.0.1", port)
        with patch("spo_pool_heat_pump.transport.tcp_simplewifi.KICK_AFTER_S", 999.0):
            await client.start(lambda f: None)
            await asyncio.sleep(0.05)
            await client.stop()
        server.close()
        await server.wait_closed()
        return port

    caplog.set_level(logging.DEBUG, logger="spo_pool_heat_pump.transport.tcp_simplewifi")
    port = asyncio.run(run())
    assert f"connecting to 127.0.0.1:{port}" in caplog.text
    assert f"connected to 127.0.0.1:{port}" in caplog.text


def test_logs_connection_failure_with_reason_and_backoff(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        client = SimpleWifiTcpClient("127.0.0.1", 1)  # nothing listens on port 1
        with patch("spo_pool_heat_pump.transport.tcp_simplewifi.BACKOFF_INITIAL_S", 0.01):
            await client.start(lambda f: None)
            await asyncio.sleep(0.1)
            await client.stop()

    caplog.set_level(logging.DEBUG, logger="spo_pool_heat_pump.transport.tcp_simplewifi")
    asyncio.run(run())
    assert "connection to 127.0.0.1:1 failed" in caplog.text
    assert "backoff" in caplog.text
