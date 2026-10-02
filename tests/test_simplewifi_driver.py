"""SimpleWifiDriver: byte-addressed caching, secret pages, writes, confirm/retry."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from spo_pool_heat_pump.drivers.simplewifi import SimpleWifiDriver, WriteNotConfirmedError
from spo_pool_heat_pump.modbus_rtu import crc16
from spo_pool_heat_pump.profiles import load_profile

MAC = bytes.fromhex("020000000001")


def _frame(typ: int, page: int, body: bytes, mac: bytes = MAC) -> bytes:
    f = bytes([0xAA, 0x5A, 0xB1, typ, page]) + mac + body
    f += crc16(f).to_bytes(2, "little")
    return f.ljust(50, b"\x00")


def _body80_1(power: int = 1, setpoint_raw: int = 125, byte15: int = 0) -> bytearray:
    body = bytearray(
        [power, 1, 0x0C, 0x74, setpoint_raw, 0x74, 0x8C, 0x54, 0x8C, 0x54, 0x8C, 0x54, 0x3E, 0x3E]
        + [0] * 8
    )
    body[15] = byte15
    return body


def _group(gid: int, data: bytes, bad_header: bool = False) -> bytes:
    header = (0x20, 0x3E) if bad_header else (0x01, len(data))
    return bytes([gid, len(data), *header]) + data


def _body_d0_1(t_in: int, t_out: int, t_amb: int, outputs_byte: int = 0, fault: bytes = b"    ") -> bytes:
    return bytes([0, t_in, t_out, t_amb, 0, 2, outputs_byte, 0, 0, 0]) + fault + bytes(8)


def _driver() -> SimpleWifiDriver:
    profile = load_profile("astral_top12_simplewifi")
    return SimpleWifiDriver(profile, send=lambda frame: None)


def test_is_live_frame_validates_marker_length_and_crc() -> None:
    driver = _driver()
    good = _frame(0x80, 1, bytes(_body80_1()))
    assert driver.is_live_frame(good) is True

    corrupted = bytearray(good)
    corrupted[12] ^= 0xFF
    assert driver.is_live_frame(bytes(corrupted)) is False

    assert driver.is_live_frame(good[:40]) is False  # wrong length
    assert driver.is_live_frame(b"\x00" * 50) is False  # no marker


def test_handle_frame_caches_known_pages() -> None:
    driver = _driver()
    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1())))
    assert driver.state.power is True
    assert driver.state.setpoint == 32.5


def test_handle_frame_rejects_bad_crc() -> None:
    driver = _driver()
    frame = bytearray(_frame(0x80, 1, bytes(_body80_1())))
    frame[12] ^= 0xFF  # corrupt a payload byte so the stored CRC no longer matches
    driver.handle_frame(bytes(frame))
    assert (0x80, 1) not in driver._pages
    assert driver.state.power is False  # never published


def test_handle_frame_never_caches_wifi_password_pages() -> None:
    driver = _driver()
    secret_body = b"super-secret-wifi-password".ljust(39, b"\x00")
    driver.handle_frame(_frame(0x80, 6, secret_body))
    driver.handle_frame(_frame(0x80, 7, secret_body))
    assert (0x80, 6) not in driver._pages
    assert (0x80, 7) not in driver._pages
    assert not any(b"secret" in v for v in driver._pages.values())


def test_handle_frame_filters_page0_header_variant() -> None:
    driver = _driver()
    good = _group(0x21, bytes(range(10)))
    bad = _group(0x21, bytes(range(10)), bad_header=True)
    driver.handle_frame(_frame(0x80, 0, bad.ljust(35, b"\x00")))
    assert (0x80, 0x21) not in driver._groups
    driver.handle_frame(_frame(0x80, 0, good.ljust(35, b"\x00")))
    assert (0x80, 0x21) in driver._groups
    # A later "no data" variant for the same group must not clobber the good one.
    driver.handle_frame(_frame(0x80, 0, bad.ljust(35, b"\x00")))
    assert driver._groups[(0x80, 0x21)][:4] == good[:4]


def test_handle_frame_caps_unknown_page0_groups() -> None:
    driver = _driver()
    for gid in range(0x60, 0x60 + 40):  # none of these are in the known list
        driver.handle_frame(_frame(0xD0, 0, _group(gid, b"\x01\x02").ljust(35, b"\x00")))
    assert len(driver._groups) <= 32


def test_write_register_confirms_on_next_page_echo() -> None:
    async def run() -> None:
        sent: list[bytes] = []
        profile = load_profile("astral_top12_simplewifi")
        driver = SimpleWifiDriver(profile, send=sent.append)
        driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0, setpoint_raw=100))))

        async def confirm_shortly() -> None:
            await asyncio.sleep(0.005)
            # The pump "echoes" the write by pushing the page back.
            driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=1, setpoint_raw=100))))

        with patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.01):
            asyncio.create_task(confirm_shortly())
            await driver.write_register("power", True)

        assert sent, "a write frame was transmitted"
        assert sent[0][3] == 0x83 and sent[0][4] == 0x01
        assert driver.state.power is True
        assert "power" not in driver.pending

    asyncio.run(run())


def test_write_register_raises_when_never_confirmed() -> None:
    async def run() -> None:
        sent: list[bytes] = []
        profile = load_profile("astral_top12_simplewifi")
        driver = SimpleWifiDriver(profile, send=sent.append)
        driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0))))

        with patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.001):
            with pytest.raises(WriteNotConfirmedError):
                await driver.write_register("power", True)
        assert "power" not in driver.pending  # reverted, not left dangling
        # Tried byte15=0x00 (default) then the 0x02 fallback once.
        assert len(sent) == 2

    asyncio.run(run())


def test_back_to_back_writes_do_not_clobber_each_other() -> None:
    """Turning on then changing the setpoint must not lose the power bit."""

    async def run() -> None:
        sent: list[bytes] = []
        profile = load_profile("astral_top12_simplewifi")
        driver = SimpleWifiDriver(profile, send=sent.append)
        driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0, setpoint_raw=100))))

        with patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.01):
            write1 = asyncio.create_task(driver.write_register("power", True))
            await asyncio.sleep(0)  # let write1 take the lock and send first
            write2 = asyncio.create_task(driver.write_register("setpoint", 32.5))

            async def echo_final() -> None:
                await asyncio.sleep(0.05)
                driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=1, setpoint_raw=125))))

            asyncio.create_task(echo_final())
            await asyncio.gather(write1, write2)

        # write2's frame must carry write1's power=1 forward, not a stale 0.
        setpoint_frame = next(f for f in sent if f[11 + 4] == 125)
        assert setpoint_frame[11] == 1

    asyncio.run(run())


def test_power_retry_carries_forward_an_interleaved_setpoint_write() -> None:
    """ligar ignorado (estilo --drop-writes 1) -> setpoint aplicado em seguida
    -> retentativa do ligar: a retentativa monta o quadro a partir do
    last_write_body ATUAL (que ja tem o setpoint novo, porque a escrita do
    setpoint partiu do corpo que o ligar tinha acabado de mandar), nao do
    corpo capturado quando o ligar foi tentado pela primeira vez."""

    async def run() -> None:
        sent: list[bytes] = []
        profile = load_profile("astral_top12_simplewifi")
        driver = SimpleWifiDriver(profile, send=sent.append)
        driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0, setpoint_raw=100))))

        with patch("spo_pool_heat_pump.drivers.simplewifi._CONFIRM_WAIT_S", 0.01):
            write1 = asyncio.create_task(driver.write_register("power", True))
            await asyncio.sleep(0)  # write1 sends its first attempt, then blocks on confirm

            assert len(sent) == 1
            assert sent[0][11 + 0] == 1  # power bit requested
            assert sent[0][11 + 4] == 100  # still the old setpoint

            write2 = asyncio.create_task(driver.write_register("setpoint", 32.5))
            await asyncio.sleep(0)  # write2 sends its own attempt, then blocks on confirm too

            assert len(sent) == 2
            assert sent[1][11 + 0] == 1  # setpoint's write already carries power=1 forward
            assert sent[1][11 + 4] == 125

            # Echo only the setpoint change. Power keeps reading back 0 — the
            # pump never actually took the power bit (the dropped-write
            # case) — so write1 is forced all the way to its retry.
            driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0, setpoint_raw=125))))
            await write2

            # write1's confirm window (8 x 0.01s) now runs out with no
            # matching echo; it must build and send a retry.
            await asyncio.sleep(0.1)
            assert len(sent) == 3, "power write did not retry"
            retry_frame = sent[2]
            assert retry_frame[11 + 0] == 1  # still asking for power on
            assert retry_frame[11 + 4] == 125  # *** the new setpoint, not the stale 100 ***
            assert retry_frame[11 + 15] == 0x02  # byte15 fallback

            # Now let the retry confirm: the pump finally shows power on too.
            driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=1, setpoint_raw=125))))
            await write1

        assert driver.state.power is True
        assert driver.state.setpoint == 32.5
        assert not driver.pending

    asyncio.run(run())


def test_set_mode_rejects_anything_but_heat() -> None:
    async def run() -> None:
        driver = _driver()
        await driver.set_mode("heat")  # no-op, does not raise
        with pytest.raises(ValueError):
            await driver.set_mode("cool")

    asyncio.run(run())


def test_unavailable_until_80_1_and_d0_1_arrive() -> None:
    """80/1 and D0/1 must both arrive at least once before the driver
    reports available — a field whose source hasn't arrived yet must never
    look like a decoded "off"."""
    driver = _driver()
    assert driver.state.available is False

    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0))))
    assert driver.state.available is False  # still missing D0/1
    assert driver.state.power is False  # 80/1 itself decodes fine already

    driver.handle_frame(_frame(0xD0, 1, _body_d0_1(112, 114, 102, outputs_byte=0b1001)))
    assert driver.state.available is True  # both halves of page 1 have arrived
    # compressor/fan live in D0/1 byte 6 — real values now, not guesses.
    assert driver.state.compressor_on is True
    assert driver.state.outputs.get("fan") is True
    assert driver.state.card_status() != "Idle"  # never falsely "Parado" either


def test_fan_alt_and_alarm_stay_unknown_without_blocking_availability() -> None:
    """The page-0 groups (fan_alt from 0x4F, alarm from 0x53) are diagnostics
    on their own rotating schedule — not arriving yet must not revoke
    availability, and must not look like a decoded False either."""
    driver = _driver()
    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=1))))
    driver.handle_frame(_frame(0xD0, 1, _body_d0_1(112, 114, 102, outputs_byte=0b0001)))
    assert driver.state.available is True
    assert driver.state.extras.get("fan_alt") is None
    assert driver.state.extras.get("alarm") is None

    driver.handle_frame(_frame(0xD0, 0, _group(0x4F, bytes(14)).ljust(35, b"\x00")))
    assert driver.state.available is True  # unaffected
    assert driver.state.extras.get("fan_alt") is False  # now known
    assert driver.state.extras.get("alarm") is None  # group 0x53 still missing

    driver.handle_frame(_frame(0xD0, 0, _group(0x53, bytes(8)).ljust(35, b"\x00")))
    assert driver.state.available is True
    assert driver.state.extras.get("alarm") is False


def test_card_status_is_no_data_while_unavailable() -> None:
    driver = _driver()
    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1(power=0))))
    assert driver.state.available is False
    assert driver.state.card_status() == "No data"


def test_logs_first_frame_first_pages_and_available(caplog: pytest.LogCaptureFixture) -> None:
    driver = _driver()
    caplog.set_level(logging.DEBUG, logger="spo_pool_heat_pump.drivers.simplewifi")

    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1())))
    assert "first frame received (type=0x80 page=1)" in caplog.text
    assert "first 80/1 frame received" in caplog.text
    assert "available=True" not in caplog.text  # D0/1 has not arrived yet

    caplog.clear()
    driver.handle_frame(_frame(0xD0, 1, _body_d0_1(112, 114, 102)))
    assert "first D0/1 frame received" in caplog.text
    assert "available=True" in caplog.text

    # Each "first" event logs only once, even on later identical frames.
    caplog.clear()
    driver.handle_frame(_frame(0x80, 1, bytes(_body80_1())))
    driver.handle_frame(_frame(0xD0, 1, _body_d0_1(112, 114, 102)))
    assert caplog.text == ""


def test_first_frame_log_never_names_a_secret_page(caplog: pytest.LogCaptureFixture) -> None:
    driver = _driver()
    caplog.set_level(logging.DEBUG, logger="spo_pool_heat_pump.drivers.simplewifi")

    driver.handle_frame(_frame(0x80, 6, bytes(22)))  # Wi-Fi password page
    assert "first frame received" in caplog.text
    assert "page=6" not in caplog.text
    assert "0x80" not in caplog.text
