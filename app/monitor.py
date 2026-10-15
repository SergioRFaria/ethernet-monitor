from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Any
import socket

import psutil

from .config import AppConfig


@dataclass(slots=True)
class CounterSnapshot:
    bytes_recv: int
    bytes_sent: int
    packets_recv: int
    packets_sent: int
    errin: int
    errout: int
    dropin: int
    dropout: int


class NetworkMonitor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.refresh_interval_seconds = max(0.25, config.refresh_interval_ms / 1000.0)
        self.history_limit = max(30, config.history_seconds)
        self._selected_interface = config.interface.strip()
        self._lock = Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=self.history_limit)
        self._previous_counters: CounterSnapshot | None = None
        self._previous_perf_time: float | None = None
        self._monitoring_started_at: float | None = None
        self._total_rx_bytes = 0
        self._total_tx_bytes = 0
        self._rx_peak_mbps = 0.0
        self._tx_peak_mbps = 0.0
        self._last_snapshot: dict[str, Any] = self._build_snapshot(
            timestamp=self._now_iso(),
            interface=self._selected_interface,
            status="MONITORING_UNAVAILABLE",
            message="Waiting for the first measurement.",
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _human_link_speed(speed_mbps: int) -> str:
        if speed_mbps <= 0:
            return "Unknown"
        if speed_mbps >= 1000:
            gigabits = speed_mbps / 1000
            if gigabits.is_integer():
                return f"{int(gigabits)} Gbps"
            return f"{gigabits:.1f} Gbps"
        return f"{speed_mbps} Mbps"

    @staticmethod
    def _extract_addresses(addresses: list[Any]) -> tuple[str, str]:
        ipv4_address = ""
        mac_address = ""
        link_families = {
            value
            for value in (
                getattr(psutil, "AF_LINK", None),
                getattr(socket, "AF_PACKET", None),
                getattr(socket, "AF_LINK", None),
            )
            if value is not None
        }

        for addr in addresses:
            family = getattr(addr, "family", None)
            if family == socket.AF_INET and not ipv4_address:
                ipv4_address = getattr(addr, "address", "") or ""
            elif family in link_families and not mac_address:
                candidate = getattr(addr, "address", "") or ""
                if candidate and candidate != "00:00:00:00:00:00":
                    mac_address = candidate
        return ipv4_address, mac_address

    @staticmethod
    def _counter_snapshot(pernic: dict[str, Any], interface: str) -> CounterSnapshot | None:
        current = pernic.get(interface)
        if current is None:
            return None
        return CounterSnapshot(
            bytes_recv=NetworkMonitor._safe_int(getattr(current, "bytes_recv", 0)),
            bytes_sent=NetworkMonitor._safe_int(getattr(current, "bytes_sent", 0)),
            packets_recv=NetworkMonitor._safe_int(getattr(current, "packets_recv", 0)),
            packets_sent=NetworkMonitor._safe_int(getattr(current, "packets_sent", 0)),
            errin=NetworkMonitor._safe_int(getattr(current, "errin", 0)),
            errout=NetworkMonitor._safe_int(getattr(current, "errout", 0)),
            dropin=NetworkMonitor._safe_int(getattr(current, "dropin", 0)),
            dropout=NetworkMonitor._safe_int(getattr(current, "dropout", 0)),
        )

    @staticmethod
    def calculate_throughput(current_bytes: int, elapsed_seconds: float) -> float:
        if elapsed_seconds <= 0:
            return 0.0
        return (current_bytes * 8) / elapsed_seconds / 1_000_000

    @staticmethod
    def calculate_utilization(current_mbps: float, link_speed_mbps: int) -> float:
        if link_speed_mbps <= 0:
            return 0.0
        return max(0.0, min(100.0, (current_mbps / link_speed_mbps) * 100))

    def discover_interfaces(self) -> list[dict[str, Any]]:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        names = sorted(set(stats) | set(addrs), key=str.lower)
        interfaces: list[dict[str, Any]] = []

        for name in names:
            stat = stats.get(name)
            ipv4_address, mac_address = self._extract_addresses(addrs.get(name, []))
            link_speed_mbps = self._safe_int(getattr(stat, "speed", 0)) if stat else 0
            is_up = bool(getattr(stat, "isup", False)) if stat else False
            status = "UP" if is_up else "DOWN"
            interfaces.append(
                {
                    "name": name,
                    "ipv4_address": ipv4_address,
                    "mac_address": mac_address,
                    "state": status,
                    "status": status,
                    "link_speed_mbps": link_speed_mbps,
                    "link_speed_label": self._human_link_speed(link_speed_mbps),
                }
            )
        return interfaces

    def get_interface_info(self, interface_name: str) -> dict[str, Any] | None:
        for interface in self.discover_interfaces():
            if interface["name"] == interface_name:
                return interface
        return None

    def read_counters(self, interface_name: str) -> CounterSnapshot | None:
        return self._counter_snapshot(psutil.net_io_counters(pernic=True), interface_name)

    def _build_snapshot(
        self,
        *,
        timestamp: str,
        interface: str,
        status: str,
        message: str | None = None,
        info: dict[str, Any] | None = None,
        counters: CounterSnapshot | None = None,
        rx_mbps: float = 0.0,
        tx_mbps: float = 0.0,
        monitoring_duration_seconds: float = 0.0,
    ) -> dict[str, Any]:
        info = info or {}
        counters = counters or CounterSnapshot(0, 0, 0, 0, 0, 0, 0, 0)
        link_speed_mbps = self._safe_int(info.get("link_speed_mbps", 0))
        snapshot = {
            "timestamp": timestamp,
            "interface": interface,
            "status": status,
            "message": message,
            "ipv4_address": info.get("ipv4_address", ""),
            "mac_address": info.get("mac_address", ""),
            "link_speed_mbps": link_speed_mbps,
            "link_speed_label": info.get("link_speed_label", self._human_link_speed(link_speed_mbps)),
            "rx_mbps": round(rx_mbps, 3),
            "tx_mbps": round(tx_mbps, 3),
            "rx_peak_mbps": round(self._rx_peak_mbps, 3),
            "tx_peak_mbps": round(self._tx_peak_mbps, 3),
            "rx_average_mbps": round(
                self.calculate_throughput(self._total_rx_bytes, monitoring_duration_seconds)
                if monitoring_duration_seconds > 0
                else 0.0,
                3,
            ),
            "tx_average_mbps": round(
                self.calculate_throughput(self._total_tx_bytes, monitoring_duration_seconds)
                if monitoring_duration_seconds > 0
                else 0.0,
                3,
            ),
            "rx_utilization": round(self.calculate_utilization(rx_mbps, link_speed_mbps), 3),
            "tx_utilization": round(self.calculate_utilization(tx_mbps, link_speed_mbps), 3),
            "total_rx_bytes": self._total_rx_bytes,
            "total_tx_bytes": self._total_tx_bytes,
            "rx_packets": counters.packets_recv,
            "tx_packets": counters.packets_sent,
            "rx_errors": counters.errin,
            "tx_errors": counters.errout,
            "rx_drops": counters.dropin,
            "tx_drops": counters.dropout,
            "monitoring_duration_seconds": round(max(0.0, monitoring_duration_seconds), 3),
            "history": list(self._history),
            "available_interfaces": self.discover_interfaces(),
        }
        return snapshot

    def _reset_runtime_state(self) -> None:
        self._history.clear()
        self._previous_counters = None
        self._previous_perf_time = None
        self._monitoring_started_at = None
        self._total_rx_bytes = 0
        self._total_tx_bytes = 0
        self._rx_peak_mbps = 0.0
        self._tx_peak_mbps = 0.0

    def reset_statistics(self) -> dict[str, Any]:
        with self._lock:
            self._reset_runtime_state()
            timestamp = self._now_iso()
            info = self.get_interface_info(self._selected_interface) or {}
            counters = CounterSnapshot(0, 0, 0, 0, 0, 0, 0, 0)
            self._last_snapshot = self._build_snapshot(
                timestamp=timestamp,
                interface=self._selected_interface,
                status="MONITORING_UNAVAILABLE" if not info else info.get("status", "DOWN"),
                message="Statistics reset.",
                info=info,
                counters=counters,
            )
            self._history.append(
                {
                    "timestamp": timestamp,
                    "rx_mbps": 0.0,
                    "tx_mbps": 0.0,
                    "rx_utilization": 0.0,
                    "tx_utilization": 0.0,
                }
            )
            self._last_snapshot["history"] = list(self._history)
            return self._last_snapshot

    def select_interface(self, interface_name: str) -> dict[str, Any]:
        interface_name = interface_name.strip()
        if not interface_name:
            raise ValueError("Interface name cannot be empty.")
        if self.get_interface_info(interface_name) is None:
            available = ", ".join(interface["name"] for interface in self.discover_interfaces()) or "none"
            raise ValueError(f"Interface '{interface_name}' was not found. Available interfaces: {available}")
        self._selected_interface = interface_name
        return self.reset_statistics()

    def update_statistics(self) -> dict[str, Any]:
        with self._lock:
            timestamp = self._now_iso()
            interface = self._selected_interface
            available_interfaces = self.discover_interfaces()
            info = next((item for item in available_interfaces if item["name"] == interface), None)

            if not interface:
                snapshot = self._build_snapshot(
                    timestamp=timestamp,
                    interface="",
                    status="MONITORING_UNAVAILABLE",
                    message="No interface is configured.",
                )
                self._history.append(
                    {
                        "timestamp": timestamp,
                        "rx_mbps": 0.0,
                        "tx_mbps": 0.0,
                        "rx_utilization": 0.0,
                        "tx_utilization": 0.0,
                    }
                )
                snapshot["history"] = list(self._history)
                self._last_snapshot = snapshot
                return snapshot

            counters = self.read_counters(interface)
            if info is None or counters is None:
                snapshot = self._build_snapshot(
                    timestamp=timestamp,
                    interface=interface,
                    status="MONITORING_UNAVAILABLE",
                    message=f"Interface '{interface}' is unavailable.",
                    info=info,
                )
                self._history.append(
                    {
                        "timestamp": timestamp,
                        "rx_mbps": 0.0,
                        "tx_mbps": 0.0,
                        "rx_utilization": 0.0,
                        "tx_utilization": 0.0,
                    }
                )
                snapshot["history"] = list(self._history)
                self._last_snapshot = snapshot
                return snapshot

            now = perf_counter()
            if self._previous_counters is None or self._previous_perf_time is None:
                self._previous_counters = counters
                self._previous_perf_time = now
                self._monitoring_started_at = now
                self._history.append(
                    {
                        "timestamp": timestamp,
                        "rx_mbps": 0.0,
                        "tx_mbps": 0.0,
                        "rx_utilization": 0.0,
                        "tx_utilization": 0.0,
                    }
                )
                snapshot = self._build_snapshot(
                    timestamp=timestamp,
                    interface=interface,
                    status=info.get("status", "DOWN"),
                    info=info,
                    counters=counters,
                    monitoring_duration_seconds=0.0,
                )
                snapshot["history"] = list(self._history)
                self._last_snapshot = snapshot
                return snapshot

            elapsed = max(1e-6, now - self._previous_perf_time)
            rx_delta = counters.bytes_recv - self._previous_counters.bytes_recv
            tx_delta = counters.bytes_sent - self._previous_counters.bytes_sent

            if rx_delta < 0 or tx_delta < 0:
                self._reset_runtime_state()
                self._previous_counters = counters
                self._previous_perf_time = now
                self._monitoring_started_at = now
                self._history.append(
                    {
                        "timestamp": timestamp,
                        "rx_mbps": 0.0,
                        "tx_mbps": 0.0,
                        "rx_utilization": 0.0,
                        "tx_utilization": 0.0,
                    }
                )
                snapshot = self._build_snapshot(
                    timestamp=timestamp,
                    interface=interface,
                    status=info.get("status", "DOWN"),
                    message="Counter reset detected; statistics restarted.",
                    info=info,
                    counters=counters,
                    monitoring_duration_seconds=0.0,
                )
                snapshot["history"] = list(self._history)
                self._last_snapshot = snapshot
                return snapshot

            rx_mbps = self.calculate_throughput(rx_delta, elapsed)
            tx_mbps = self.calculate_throughput(tx_delta, elapsed)
            self._rx_peak_mbps = max(self._rx_peak_mbps, rx_mbps)
            self._tx_peak_mbps = max(self._tx_peak_mbps, tx_mbps)
            self._total_rx_bytes += max(0, rx_delta)
            self._total_tx_bytes += max(0, tx_delta)
            self._previous_counters = counters
            self._previous_perf_time = now
            monitoring_duration_seconds = 0.0 if self._monitoring_started_at is None else max(0.0, now - self._monitoring_started_at)
            self._history.append(
                {
                    "timestamp": timestamp,
                    "rx_mbps": round(rx_mbps, 3),
                    "tx_mbps": round(tx_mbps, 3),
                    "rx_utilization": round(self.calculate_utilization(rx_mbps, info.get("link_speed_mbps", 0)), 3),
                    "tx_utilization": round(self.calculate_utilization(tx_mbps, info.get("link_speed_mbps", 0)), 3),
                }
            )
            snapshot = self._build_snapshot(
                timestamp=timestamp,
                interface=interface,
                status=info.get("status", "DOWN"),
                info=info,
                counters=counters,
                rx_mbps=rx_mbps,
                tx_mbps=tx_mbps,
                monitoring_duration_seconds=monitoring_duration_seconds,
            )
            snapshot["history"] = list(self._history)
            self._last_snapshot = snapshot
            return snapshot

    def get_status(self, include_history: bool = True) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._last_snapshot)
            if include_history:
                snapshot["history"] = list(self._history)
            else:
                snapshot.pop("history", None)
            snapshot["available_interfaces"] = self.discover_interfaces()
            return snapshot
