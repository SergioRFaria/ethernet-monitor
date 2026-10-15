from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InterfaceSelectionRequest(BaseModel):
    interface: str = Field(min_length=1)


class InterfaceInfo(BaseModel):
    name: str
    ipv4_address: str = ""
    mac_address: str = ""
    state: str = "DOWN"
    status: str = "DOWN"
    link_speed_mbps: int = 0
    link_speed_label: str = "Unknown"


class HistoryPoint(BaseModel):
    timestamp: str
    rx_mbps: float = 0.0
    tx_mbps: float = 0.0
    rx_utilization: float = 0.0
    tx_utilization: float = 0.0


class NetworkStatus(BaseModel):
    timestamp: str
    interface: str
    status: str
    message: str | None = None

    ipv4_address: str = ""
    mac_address: str = ""
    link_speed_mbps: int = 0
    link_speed_label: str = "Unknown"

    rx_mbps: float = 0.0
    tx_mbps: float = 0.0
    rx_peak_mbps: float = 0.0
    tx_peak_mbps: float = 0.0
    rx_average_mbps: float = 0.0
    tx_average_mbps: float = 0.0
    rx_utilization: float = 0.0
    tx_utilization: float = 0.0
    total_rx_bytes: int = 0
    total_tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_drops: int = 0
    tx_drops: int = 0
    monitoring_duration_seconds: float = 0.0
    history: list[HistoryPoint] = Field(default_factory=list)
    available_interfaces: list[InterfaceInfo] = Field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        return self.model_dump()


_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_server_address(value: str) -> str:
    """Accept an IPv4 literal or an RFC 1123 hostname; reject anything that could act as a CLI flag."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("Server address is required.")
    if len(candidate) > 253:
        raise ValueError("Server address is too long.")
    if candidate.startswith("-"):
        raise ValueError("Server address must not start with '-'.")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        if address.version != 4:
            raise ValueError("Only IPv4 addresses or hostnames are supported.")
        if address.is_unspecified or address.is_multicast or candidate == "255.255.255.255":
            raise ValueError("Server address must be a unicast IPv4 address.")
        return str(address)

    labels = candidate.rstrip(".").split(".")
    if not labels or not all(_HOSTNAME_LABEL.match(label) for label in labels):
        raise ValueError("Server address is not a valid IPv4 address or hostname.")
    if all(label.isdigit() for label in labels):
        raise ValueError("Server address is not a valid IPv4 address.")
    return candidate.rstrip(".")


def validate_ipv4_bind_address(value: str) -> str:
    candidate = (value or "").strip()
    try:
        address = ipaddress.IPv4Address(candidate)
    except ValueError as exc:
        raise ValueError("Bind address must be an IPv4 address.") from exc
    if address.is_multicast:
        raise ValueError("Bind address must not be multicast.")
    return str(address)


class SpeedTestStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interface: str = Field(min_length=1, max_length=256)
    server: str
    port: int = Field(default=5201, ge=1, le=65535)
    mode: Literal["quick", "tcp", "udp", "full"] = "full"
    duration_seconds: Literal[5, 10, 20, 30] = 10
    parallel_streams: Literal[1, 2, 4, 8] = 4
    udp_bandwidth_mbps: float = Field(default=100.0, ge=1.0, le=10000.0)
    bidirectional: bool = False

    @field_validator("server")
    @classmethod
    def _check_server(cls, value: str) -> str:
        return validate_server_address(value)

    @field_validator("interface")
    @classmethod
    def _check_interface(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Invalid interface name.")
        return value


class SpeedTestConfigRequest(BaseModel):
    server: str
    port: int = Field(default=5201, ge=1, le=65535)

    @field_validator("server")
    @classmethod
    def _check_server(cls, value: str) -> str:
        return validate_server_address(value)


class LocalServerStartRequest(BaseModel):
    bind_address: str
    port: int = Field(default=5201, ge=1, le=65535)

    @field_validator("bind_address")
    @classmethod
    def _check_bind(cls, value: str) -> str:
        return validate_ipv4_bind_address(value)
