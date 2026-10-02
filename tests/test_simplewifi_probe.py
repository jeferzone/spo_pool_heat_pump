"""tools/simplewifi_probe.py must never hand pages 80/6 or 80/7 to the driver.

Exercises the script's own _run() against a short-lived fake server, instead
of re-implementing its skip check — so a future change that silently drops
the guard (or stops counting it) still fails this test, and a leaked Wi-Fi
password would actually show up in the captured output we assert against.
"""

from __future__ import annotations

import asyncio

import tools.simplewifi_probe as probe

SECRET_MARKER = b"MY-REAL-WIFI-PASSWORD-DO-NOT-LEAK"


def _frame(typ: int, page: int, body: bytes = b"") -> bytes:
    return (bytes([0xAA, 0x5A, 0xB1, typ, page]) + body).ljust(50, b"\x00")[:50]


def test_probe_never_prints_the_secret_pages_and_counts_them_skipped(capsys) -> None:
    async def run() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Two secret-page frames (the Wi-Fi password pages), each carrying
            # a marker that must never reach stdout, then one ordinary frame.
            writer.write(_frame(0x80, 6, SECRET_MARKER))
            writer.write(_frame(0x80, 7, SECRET_MARKER))
            writer.write(_frame(0x80, 1, b"\x01"))
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await probe._run("127.0.0.1", port, total_seconds=0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())

    out = capsys.readouterr().out
    assert SECRET_MARKER not in out.encode()
    assert "80/6" not in out
    assert "80/7" not in out
    assert "2 quadro(s) de senha ignorado(s)" in out


def test_probe_still_counts_and_processes_normal_frames(capsys) -> None:
    async def run() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(_frame(0x80, 1, b"\x01"))
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await probe._run("127.0.0.1", port, total_seconds=0.1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())

    out = capsys.readouterr().out
    assert "0 quadro(s) de senha ignorado(s)" in out
    assert "1 quadro(s) recebido" in out
