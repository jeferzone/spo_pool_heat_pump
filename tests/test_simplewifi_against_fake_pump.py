"""End-to-end smoke test against tests/fake_simplewifi_pump.py.

That script is a reference simulator for this protocol, with only fabricated
data (fake MAC, fake temperatures, no Wi-Fi password) — it is not a
substitute for testing against the real Astral Top +12, but it does
exercise the whole wire path: real TCP, real framing, real CRC, and a real
write + confirm round trip.

Run as a subprocess, not an in-process asyncio task: the simulator's own
`srv.serve_forever()` does not reliably respond to Task.cancel() once a
client has connected and disconnected (an asyncio Server.wait_closed() quirk
in its own cancellation path, unrelated to this driver) — a separate process
can just be killed, sidestepping that entirely.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

FAKE_PUMP = Path(__file__).resolve().parent / "fake_simplewifi_pump.py"

from spo_pool_heat_pump.drivers.simplewifi import SimpleWifiDriver  # noqa: E402
from spo_pool_heat_pump.profiles import load_profile  # noqa: E402
from spo_pool_heat_pump.transport.tcp_simplewifi import SimpleWifiTcpClient  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakePump:
    """Launches fake_simplewifi_pump.py as a subprocess and tears it down on exit."""

    def __init__(self, *extra_args: str) -> None:
        self.port = _free_port()
        self._extra_args = extra_args
        self._proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> "_FakePump":
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(FAKE_PUMP),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--apply-delay",
            "0.05",
            *self._extra_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.3)  # let it bind and start listening
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._proc is not None
        self._proc.terminate()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()


def test_decodes_initial_state_and_confirms_a_write() -> None:
    async def run() -> None:
        async with _FakePump() as pump:
            profile = load_profile("astral_top12_simplewifi")
            client = SimpleWifiTcpClient("127.0.0.1", pump.port)
            driver = SimpleWifiDriver(profile, send=client.send)
            try:
                await client.start(lambda f: driver.handle_frame(f))
                await asyncio.sleep(1.0)  # at least one full push burst (~0.6s cadence)

                assert driver.state.power is True
                assert driver.state.setpoint == pytest.approx(33.0, abs=0.01)
                assert driver.state.t_inlet == pytest.approx(26.0, abs=0.01)
                assert driver.state.t_outlet == pytest.approx(27.0, abs=0.01)
                assert driver.state.t_ambient == pytest.approx(21.0, abs=0.01)
                assert driver.state.compressor_on is True  # t_inlet < setpoint
                assert driver.state.mode == "heat"

                with patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.1):
                    await driver.write_register("setpoint", 30.0)
                assert driver.state.setpoint == pytest.approx(30.0, abs=0.01)
            finally:
                await client.stop()

    asyncio.run(run())


def test_write_survives_one_dropped_attempt() -> None:
    """fake_pump --drop-writes 1 reproduces the one intermittent silent-ignore
    CONTEXTO.md observed on the real unit; the retry/confirm loop must recover."""

    async def run() -> None:
        async with _FakePump("--drop-writes", "1") as pump:
            profile = load_profile("astral_top12_simplewifi")
            client = SimpleWifiTcpClient("127.0.0.1", pump.port)
            driver = SimpleWifiDriver(profile, send=client.send)
            try:
                await client.start(lambda f: driver.handle_frame(f))
                await asyncio.sleep(1.0)
                # Generous margin over the simulator's ~0.6s push cadence:
                # the dropped first attempt burns a full confirm window
                # before the retry even goes out.
                with (
                    patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.15),
                    patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_RETRIES", 12),
                ):
                    await driver.write_register("power", False)
                assert driver.state.power is False
            finally:
                await client.stop()

    asyncio.run(run())
