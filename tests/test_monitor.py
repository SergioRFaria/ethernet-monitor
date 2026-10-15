from __future__ import annotations

from dataclasses import dataclass
import socket
import unittest
from unittest.mock import patch

import psutil

from app.config import AppConfig
from app.monitor import NetworkMonitor


@dataclass
class DummyStat:
    isup: bool = True
    speed: int = 1000


@dataclass
class DummyAddr:
    family: int
    address: str


@dataclass
class DummyCounter:
    bytes_recv: int
    bytes_sent: int
    packets_recv: int = 0
    packets_sent: int = 0
    errin: int = 0
    errout: int = 0
    dropin: int = 0
    dropout: int = 0


class NetworkMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats_patch = patch("psutil.net_if_stats", return_value={"Ethernet": DummyStat(isup=True, speed=1000)})
        self.addrs_patch = patch(
            "psutil.net_if_addrs",
            return_value={
                "Ethernet": [
                    DummyAddr(socket.AF_INET, "192.168.100.20"),
                    DummyAddr(getattr(psutil, "AF_LINK", 17), "AA:BB:CC:DD:EE:FF"),
                ]
            },
        )
        self.stats_patch.start()
        self.addrs_patch.start()
        self.addCleanup(self.stats_patch.stop)
        self.addCleanup(self.addrs_patch.stop)
        self.monitor = NetworkMonitor(AppConfig(interface="Ethernet", refresh_interval_ms=1000, history_seconds=300))

    def test_discover_interfaces_includes_ipv4_and_mac(self) -> None:
        interfaces = self.monitor.discover_interfaces()
        self.assertEqual(
            interfaces,
            [
                {
                    "name": "Ethernet",
                    "ipv4_address": "192.168.100.20",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "state": "UP",
                    "status": "UP",
                    "link_speed_mbps": 1000,
                    "link_speed_label": "1 Gbps",
                }
            ],
        )

    def test_throughput_calculation_uses_byte_deltas(self) -> None:
        counters = [
            {"Ethernet": DummyCounter(bytes_recv=0, bytes_sent=0, packets_recv=10, packets_sent=20)},
            {"Ethernet": DummyCounter(bytes_recv=5_000_000, bytes_sent=2_500_000, packets_recv=12, packets_sent=22)},
        ]

        with patch("psutil.net_io_counters", side_effect=lambda pernic=True: counters.pop(0)), patch(
            "app.monitor.perf_counter", side_effect=[100.0, 101.0]
        ):
            first = self.monitor.update_statistics()
            second = self.monitor.update_statistics()

        self.assertEqual(first["rx_mbps"], 0.0)
        self.assertEqual(first["tx_mbps"], 0.0)
        self.assertAlmostEqual(second["rx_mbps"], 40.0, places=2)
        self.assertAlmostEqual(second["tx_mbps"], 20.0, places=2)
        self.assertAlmostEqual(second["rx_peak_mbps"], 40.0, places=2)
        self.assertAlmostEqual(second["tx_peak_mbps"], 20.0, places=2)
        self.assertAlmostEqual(second["rx_utilization"], 4.0, places=2)
        self.assertAlmostEqual(second["tx_utilization"], 2.0, places=2)
        self.assertEqual(second["total_rx_bytes"], 5_000_000)
        self.assertEqual(second["total_tx_bytes"], 2_500_000)
        self.assertAlmostEqual(second["rx_average_mbps"], 40.0, places=2)
        self.assertAlmostEqual(second["tx_average_mbps"], 20.0, places=2)

    def test_counter_reset_restarts_statistics(self) -> None:
        counters = [
            {"Ethernet": DummyCounter(bytes_recv=10_000, bytes_sent=10_000)},
            {"Ethernet": DummyCounter(bytes_recv=5_000, bytes_sent=4_000)},
        ]

        with patch("psutil.net_io_counters", side_effect=lambda pernic=True: counters.pop(0)), patch(
            "app.monitor.perf_counter", side_effect=[200.0, 201.0]
        ):
            self.monitor.update_statistics()
            reset = self.monitor.update_statistics()

        self.assertEqual(reset["rx_mbps"], 0.0)
        self.assertEqual(reset["tx_mbps"], 0.0)
        self.assertEqual(reset["rx_peak_mbps"], 0.0)
        self.assertEqual(reset["tx_peak_mbps"], 0.0)
        self.assertEqual(reset["total_rx_bytes"], 0)
        self.assertEqual(reset["total_tx_bytes"], 0)
        self.assertIn("Counter reset detected", reset["message"] or "")


if __name__ == "__main__":
    unittest.main()
