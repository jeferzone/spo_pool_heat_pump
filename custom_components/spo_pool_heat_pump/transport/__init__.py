"""Transports."""

from __future__ import annotations

from typing import Any

from .tcp import TcpRtuClient
from .tcp_simplewifi import SimpleWifiTcpClient

__all__ = ["SimpleWifiTcpClient", "TcpRtuClient", "build_transport"]

# Every pc1002_bus/poll_master/listen_only profile talks Modbus RTU over a
# transparent TCP pipe (DR164-style). simplewifi_tcp speaks its own framed
# protocol directly over a dedicated socket — a different client entirely.
_MODBUS_DRIVER_TYPES = {"pc1002_bus", "poll_master", "listen_only"}


def build_transport(profile: dict[str, Any], host: str, port: int) -> TcpRtuClient | SimpleWifiTcpClient:
    kind = profile["driver"]["type"]
    if kind in _MODBUS_DRIVER_TYPES:
        return TcpRtuClient(host, port)
    if kind == "simplewifi_tcp":
        return SimpleWifiTcpClient(host, port)
    raise ValueError(f"no transport for driver type {kind!r}")
