"""build_transport dispatches a profile's driver.type to a transport client."""

from __future__ import annotations

import pytest

from spo_pool_heat_pump.drivers import SimpleWifiDriver, build_driver
from spo_pool_heat_pump.profiles import load_profile
from spo_pool_heat_pump.transport import SimpleWifiTcpClient, TcpRtuClient, build_transport


@pytest.mark.parametrize("driver_type", ["pc1002_bus", "poll_master", "listen_only"])
def test_build_transport_returns_tcp_rtu_client_for_modbus_profiles(driver_type: str) -> None:
    profile = {"driver": {"type": driver_type}}
    client = build_transport(profile, "10.0.0.8", 8899)
    assert isinstance(client, TcpRtuClient)
    assert client.host == "10.0.0.8"
    assert client.port == 8899


def test_build_transport_returns_simplewifi_client() -> None:
    profile = {"driver": {"type": "simplewifi_tcp"}}
    client = build_transport(profile, "192.0.2.10", 60000)
    assert isinstance(client, SimpleWifiTcpClient)
    assert client.host == "192.0.2.10"
    assert client.port == 60000


def test_real_astral_profile_dispatches_to_simplewifi() -> None:
    profile = load_profile("astral_top12_simplewifi")

    client = build_transport(profile, "192.0.2.10", 60000)
    assert isinstance(client, SimpleWifiTcpClient)
    assert client.host == "192.0.2.10"
    assert client.port == 60000

    driver = build_driver(profile, send=client.send, write_path="")
    assert isinstance(driver, SimpleWifiDriver)


def test_build_transport_rejects_unknown_driver_type() -> None:
    profile = {"driver": {"type": "not_a_real_driver"}}
    with pytest.raises(ValueError):
        build_transport(profile, "10.0.0.8", 60000)
