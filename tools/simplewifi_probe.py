#!/usr/bin/env python3
"""Read-only manual check for a Simple-WiFi module (Astral Top +12 and others).

Uses the integration's own transport/driver — SimpleWifiTcpClient and
SimpleWifiDriver from custom_components/spo_pool_heat_pump/ — not the
docs/astral/ reverse-engineering scripts, so this exercises exactly the
code the Home Assistant component runs.

Never writes to the pump: no set_power / set_setpoint / write_register call
anywhere in this script, and the driver's own encoded_write()/service-menu
write path is never reached either.

Never prints or logs pages 80/6 and 80/7 (plaintext Wi-Fi password) — the
driver already refuses to cache or decode them, and this script also checks
the raw frame's own (type, page) itself before handing it to the driver, so
that guarantee does not depend only on the driver doing it right. Also never
prints the module's MAC address or serial number: this script only ever
reads the decoded fields listed below off HeatPumpState, never driver._mac,
never raw frame bytes.

Usage:
    python tools/simplewifi_probe.py 192.0.2.10
    python tools/simplewifi_probe.py 192.0.2.10 --port 60000 --seconds 20

Behavior:
    1. Connects and sends nothing for the first 10s, watching whether the
       module pushes frames on its own.
    2. If nothing arrived by then, leaves the client's own kick mechanism
       (SimpleWifiTcpClient's KICK_AFTER_S watchdog) to send the read
       request, and reports once frames start arriving either way.
    3. Prints the decoded state every 2s (power, setpoint, inlet/outlet/
       ambient temperatures, compressor, fan, fault) until --seconds total
       has elapsed (counted from the connection, not from step 3), then
       disconnects and prints a one-line summary: frames received,
       reconnections, and whether the module pushed on its own.

Ctrl+C prints the same summary before exiting.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from spo_pool_heat_pump.drivers.simplewifi import SimpleWifiDriver  # noqa: E402
from spo_pool_heat_pump.profiles import load_profile  # noqa: E402
from spo_pool_heat_pump.transport import tcp_simplewifi as _simplewifi_transport  # noqa: E402
from spo_pool_heat_pump.transport.tcp_simplewifi import SimpleWifiTcpClient  # noqa: E402

REAL_KICK_AFTER_S = _simplewifi_transport.KICK_AFTER_S
REAL_RECONNECT_AFTER_S = _simplewifi_transport.RECONNECT_AFTER_S

LISTEN_ONLY_WINDOW_S = 10.0
PRINT_INTERVAL_S = 2.0

# Mirrors drivers/simplewifi.py::_SECRET_PAGES. Checked again here, on the raw
# frame, before this script hands it to the driver at all: this is the tool
# whose output people paste into a public issue, so its own "never touches
# the Wi-Fi password" guarantee should not rest solely on the driver getting
# it right.
_SECRET_FRAME_PAGES = frozenset({(0x80, 6), (0x80, 7)})


def _fmt_temp(value: float | None) -> str:
    return f"{value:.1f}C" if value is not None else "?"


def _fmt_state(state) -> str:
    outputs = state.outputs or {}
    parts = [
        f"ligada={state.power}",
        f"setpoint={_fmt_temp(state.setpoint)}",
        f"entrada={_fmt_temp(state.t_inlet)}",
        f"saida={_fmt_temp(state.t_outlet)}",
        f"ambiente={_fmt_temp(state.t_ambient)}",
        f"compressor={outputs.get('compressor')}",
        f"ventoinha={outputs.get('fan')}",
    ]
    parts.append(f"erro={state.fault_code}" if state.fault_code else "erro=nenhum")
    return " ".join(parts)


class Counters:
    def __init__(self) -> None:
        self.frames = 0
        self.secret_frames_skipped = 0
        self.reconnects = 0
        self.pushed_on_its_own = False

    def summary(self) -> str:
        modo = "push (transmitiu sozinho)" if self.pushed_on_its_own else "só respondeu a pedido"
        return (
            f"[probe] resumo: {self.frames} quadro(s) recebido(s), "
            f"{self.secret_frames_skipped} quadro(s) de senha ignorado(s) (nunca lidos), "
            f"{self.reconnects} reconexao(oes), modo: {modo}"
        )


async def _run(host: str, port: int, total_seconds: float) -> None:
    profile = load_profile("astral_top12_simplewifi")
    client = SimpleWifiTcpClient(host, port)
    driver = SimpleWifiDriver(profile, send=client.send)
    counters = Counters()
    loop = asyncio.get_running_loop()

    def on_frame(frame: bytes) -> None:
        counters.frames += 1
        # Belt-and-suspenders: the transport only frames by marker+length (no
        # CRC check), so this reads (type, page) straight off the raw bytes,
        # ahead of the driver's own identical check in handle_frame. Counted,
        # never printed with any page/content detail — the summary only ever
        # says how many were skipped, not which ones or what was in them.
        if len(frame) >= 5 and (frame[3], frame[4]) in _SECRET_FRAME_PAGES:
            counters.secret_frames_skipped += 1
            return
        driver.handle_frame(frame)

    def on_connection(connected: bool) -> None:
        if connected:
            if counters.reconnects:
                print("[probe] reconectado")
        else:
            counters.reconnects += 1
            print("[probe] conexao caiu; o cliente vai tentar reconectar sozinho")

    # SimpleWifiTcpClient's own watchdog kicks a read request after just
    # REAL_KICK_AFTER_S (normally 3s) of silence, and redials the socket
    # after REAL_RECONNECT_AFTER_S (normally 10s) — both well inside, or
    # exactly at the edge of, the 10s window below. Push both out past that
    # window for now: a reply arriving during the window can then only be
    # the module transmitting on its own, not our own kick, and the
    # redial-on-silence must not fire just because we are deliberately
    # staying quiet. Restored before the fallback phase (and in `finally`,
    # in case we never get there) so both behave normally afterwards.
    _simplewifi_transport.KICK_AFTER_S = LISTEN_ONLY_WINDOW_S + 1.0
    _simplewifi_transport.RECONNECT_AFTER_S = LISTEN_ONLY_WINDOW_S + 1.0

    await client.start(on_frame, on_connection)
    start = loop.time()
    print(f"[probe] conectado a {host}:{port}; escutando {LISTEN_ONLY_WINDOW_S:.0f}s sem mandar nada...")

    # Everything below is wrapped so Ctrl+C (which asyncio.run() turns into a
    # cancellation of this coroutine, not a bare KeyboardInterrupt here) still
    # prints the summary, instead of exiting silently mid-run.
    try:
        while loop.time() - start < LISTEN_ONLY_WINDOW_S:
            await asyncio.sleep(0.5)
            if counters.frames > 0:
                counters.pushed_on_its_own = True
                print(f"[probe] o modulo transmite por conta propria ({counters.frames} quadro(s) ate agora)")
                break
        _simplewifi_transport.KICK_AFTER_S = REAL_KICK_AFTER_S
        _simplewifi_transport.RECONNECT_AFTER_S = REAL_RECONNECT_AFTER_S
        # The watchdog measures silence from the real connection time, which
        # by now already exceeds the real thresholds just restored above —
        # restart its clock so "silent" is judged from this moment on, not
        # retroactively over the window we spent deliberately not kicking.
        client._last_rx = loop.time()
        client._last_kick = 0.0

        if not counters.pushed_on_its_own:
            print(
                f"[probe] nada chegou sozinho; o cliente deve mandar o pedido de leitura "
                f"apos {REAL_KICK_AFTER_S:.0f}s de silencio (mecanismo de kick) — aguardando..."
            )
            while counters.frames == 0:
                if loop.time() - start >= total_seconds:
                    print("[probe] nenhum quadro chegou dentro do tempo total; encerrando")
                    return
                await asyncio.sleep(0.5)
            print("[probe] o pedido de leitura funcionou; quadros comecaram a chegar")

        print("[probe] estado decodificado (Ctrl+C para parar antes do tempo):")
        while loop.time() - start < total_seconds:
            print(f"[probe] {_fmt_state(driver.state)}")
            await asyncio.sleep(PRINT_INTERVAL_S)
    finally:
        _simplewifi_transport.KICK_AFTER_S = REAL_KICK_AFTER_S
        _simplewifi_transport.RECONNECT_AFTER_S = REAL_RECONNECT_AFTER_S
        await client.stop()
        print(counters.summary())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verificacao manual, somente leitura, de um modulo Simple-WiFi.",
        epilog="Exemplo: python tools/simplewifi_probe.py 192.0.2.10 --port 60000 --seconds 20",
    )
    parser.add_argument("host", help="IP do modulo, ex.: 192.0.2.10")
    parser.add_argument("--port", type=int, default=60000, help="porta TCP (default: 60000)")
    parser.add_argument(
        "--seconds",
        type=float,
        default=20.0,
        help="duracao total a partir da conexao, incluindo os 10s iniciais de escuta (default: 20)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.host, args.port, args.seconds))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
