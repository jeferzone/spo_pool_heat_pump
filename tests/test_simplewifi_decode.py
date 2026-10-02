"""apply_simplewifi_map: byte-addressed decode for the Simple-WiFi profile."""

from __future__ import annotations

from spo_pool_heat_pump.drivers.decode_simplewifi import apply_simplewifi_map
from spo_pool_heat_pump.profiles import load_profile


def _body80_1(power: int = 1, setpoint_raw: int = 125) -> bytes:
    return bytes(
        [power, 1, 0x0C, 0x74, setpoint_raw, 0x74, 0x8C, 0x54, 0x8C, 0x54, 0x8C, 0x54, 0x3E, 0x3E]
        + [0] * 8
    )


def _body_d0_1(t_in: int, t_out: int, t_amb: int, outputs_byte: int, fault: bytes = b"    ") -> bytes:
    return bytes([0, t_in, t_out, t_amb, 0, 2, outputs_byte, 0, 0, 0]) + fault + bytes(8)


def _group(gid: int, data: bytes) -> bytes:
    return bytes([gid, len(data), 0x01, len(data)]) + data


def test_decode_power_and_setpoint() -> None:
    profile = load_profile("astral_top12_simplewifi")
    pages = {(0x80, 1): _body80_1(power=1, setpoint_raw=125)}
    state = apply_simplewifi_map(profile, pages)
    assert state.power is True
    assert state.setpoint == 32.5
    assert state.mode == "heat"


def test_decode_temperatures_and_outputs() -> None:
    profile = load_profile("astral_top12_simplewifi")
    pages = {(0x80, 1): _body80_1(), (0xD0, 1): _body_d0_1(112, 114, 102, outputs_byte=0b0001)}
    state = apply_simplewifi_map(profile, pages)
    assert state.t_inlet == 26.0
    assert state.t_outlet == 27.0
    assert state.t_ambient == 21.0
    assert state.compressor_on is True
    assert state.outputs["fan"] is False


def test_decode_fault_text_strips_and_empties() -> None:
    profile = load_profile("astral_top12_simplewifi")
    pages = {(0xD0, 1): _body_d0_1(100, 100, 100, 0, fault=b" E03")}
    state = apply_simplewifi_map(profile, pages)
    assert state.faults == ["E03"]
    assert state.fault_code == "E03"

    pages_ok = {(0xD0, 1): _body_d0_1(100, 100, 100, 0, fault=b"    ")}
    state_ok = apply_simplewifi_map(profile, pages_ok)
    assert state_ok.faults == []


def test_decode_missing_pages_leaves_fields_none() -> None:
    profile = load_profile("astral_top12_simplewifi")
    state = apply_simplewifi_map(profile, {})
    assert state.power is False
    assert state.setpoint is None
    assert state.t_inlet is None
    assert state.mode == "heat"  # always known even with no data yet
    assert state.available is False


def test_available_once_80_1_and_d0_1_seen_groups_not_required() -> None:
    profile = load_profile("astral_top12_simplewifi")
    page80_1 = _body80_1()
    page_d0_1 = _body_d0_1(112, 114, 102, outputs_byte=0b0001)

    assert apply_simplewifi_map(profile, {}, {}).available is False

    # Only 80/1: still unavailable, missing D0/1.
    state = apply_simplewifi_map(profile, {(0x80, 1): page80_1}, {})
    assert state.available is False

    # 80/1 + D0/1, no page-0 groups at all: already available.
    state = apply_simplewifi_map(profile, {(0x80, 1): page80_1, (0xD0, 1): page_d0_1}, {})
    assert state.available is True
    # compressor/fan live in D0/1 byte 6 — already real values, not guesses.
    assert state.compressor_on is True
    assert state.outputs["fan"] is False
    # fan_alt/alarm depend on page-0 groups that have not arrived yet.
    assert state.extras.get("fan_alt") is None
    assert state.extras.get("alarm") is None


def test_fan_alt_and_alarm_populate_independently_once_their_group_arrives() -> None:
    profile = load_profile("astral_top12_simplewifi")
    page80_1 = _body80_1()
    page_d0_1 = _body_d0_1(112, 114, 102, outputs_byte=0b0001)
    # Group body = [gid, len, header(2), data...]; data starts at body index 4,
    # so body[7] is data[3] and body[5] is data[1].
    group_4f = _group(0x4F, bytes([0, 0, 0, 1, 0, 0, 0, 0]))  # body[7] = True
    group_53 = _group(0x53, bytes([0, 1, 0, 0, 0]))  # body[5] = True

    state = apply_simplewifi_map(
        profile,
        {(0x80, 1): page80_1, (0xD0, 1): page_d0_1},
        {(0xD0, 0x4F): group_4f},
    )
    assert state.available is True  # group arriving later never revokes availability
    assert state.extras.get("fan_alt") is True
    assert state.extras.get("alarm") is None  # group 0x53 still hasn't arrived

    state = apply_simplewifi_map(
        profile,
        {(0x80, 1): page80_1, (0xD0, 1): page_d0_1},
        {(0xD0, 0x4F): group_4f, (0xD0, 0x53): group_53},
    )
    assert state.extras.get("alarm") is True


def test_unavailable_state_reports_no_data_not_idle() -> None:
    profile = load_profile("astral_top12_simplewifi")
    state = apply_simplewifi_map(profile, {(0x80, 1): _body80_1(power=0)}, {})
    assert state.available is False
    assert state.card_status() == "No data"
