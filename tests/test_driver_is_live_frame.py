"""HeatPumpDriver.is_live_frame's Modbus-RTU default, exercised via PollMasterDriver.

No homeassistant import anywhere in this chain (drivers/base.py, drivers/poll_master.py,
modbus_rtu.py, profiles/__init__.py are all plain Python), so this runs without it.
"""

from __future__ import annotations

from spo_pool_heat_pump.drivers.poll_master import PollMasterDriver
from spo_pool_heat_pump.modbus_rtu import encode_fc03_reply
from spo_pool_heat_pump.profiles import load_profile


def _driver() -> PollMasterDriver:
    profile = load_profile("fairland_pc1004_cn13")
    return PollMasterDriver(profile, send=lambda frame: None)


def test_is_live_frame_true_for_a_valid_modbus_frame() -> None:
    driver = _driver()
    frame = encode_fc03_reply(50, [1, 1, 310])
    assert driver.is_live_frame(frame) is True


def test_is_live_frame_false_for_meaningless_bytes() -> None:
    driver = _driver()
    assert driver.is_live_frame(b"not a modbus frame at all") is False


def test_is_live_frame_false_for_modbus_frame_with_wrong_crc() -> None:
    driver = _driver()
    frame = bytearray(encode_fc03_reply(50, [1, 1, 310]))
    frame[-1] ^= 0xFF  # corrupt the CRC; the rest of the frame is still well-formed
    assert driver.is_live_frame(bytes(frame)) is False
