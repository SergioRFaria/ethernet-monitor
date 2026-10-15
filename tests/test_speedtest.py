from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import main as app_main
from app import process_utils
from app.config import AppConfig
from app.latency import summarize_rtts
from app.models import SpeedTestStartRequest, validate_server_address
from app.monitor import CounterSnapshot
from app.network_context import classify_interface, parse_netsh_wlan_interfaces
from app.speedtest import (
    SpeedTestBusyError,
    SpeedTestError,
    SpeedTestService,
    SpeedTestState,
    build_iperf_command,
    calculate_efficiency,
    calculate_summary,
    classify_efficiency,
    compare_results,
    extract_bidir_result,
    extract_tcp_result,
    extract_udp_result,
    map_iperf_error,
    parse_iperf_json,
    parse_iperf_version,
    resolve_iperf_executable,
)
from app.speedtest_store import SpeedTestStore

APP_DIR = Path(__file__).resolve().parents[1] / "app"
IPERF = r"C:\tools\iperf3.exe"

TCP_UPLOAD_JSON = {
    "start": {"test_start": {"protocol": "TCP", "num_streams": 4, "reverse": 0}},
    "intervals": [
        {"sum": {"bits_per_second": 930e6, "omitted": False}},
        {"sum": {"bits_per_second": 951.5e6, "omitted": False}},
        {"sum": {"bits_per_second": 990e6, "omitted": True}},
    ],
    "end": {
        "sum_sent": {"bits_per_second": 940e6, "retransmits": 12, "bytes": 1175000000, "seconds": 10.0},
        "sum_received": {"bits_per_second": 936.8e6, "bytes": 1171000000, "seconds": 10.0},
    },
}
TCP_DOWNLOAD_JSON = {
    "start": {"test_start": {"protocol": "TCP", "num_streams": 4, "reverse": 1}},
    "intervals": [{"sum": {"bits_per_second": 945e6}}, {"sum": {"bits_per_second": 938e6}}],
    "end": {
        "sum_sent": {"bits_per_second": 943e6, "bytes": 1178000000, "seconds": 10.0},
        "sum_received": {"bits_per_second": 941.2e6, "bytes": 1176500000, "seconds": 10.0},
    },
}
UDP_JSON = {
    "start": {"test_start": {"protocol": "UDP", "num_streams": 1}},
    "intervals": [{"sum": {"bits_per_second": 100e6}}],
    "end": {
        "sum": {"bits_per_second": 100e6, "jitter_ms": 0.0312, "lost_packets": 12, "packets": 86000, "lost_percent": 0.013953},
        "sum_received": {"bits_per_second": 99.98e6, "jitter_ms": 0.0312, "lost_packets": 12, "packets": 86000, "lost_percent": 0.013953},
    },
}
UDP_LEGACY_JSON = {"end": {"sum": {"bits_per_second": 50e6, "jitter_ms": 0.5, "lost_packets": 100, "packets": 1000, "lost_percent": 10.0}}}
BIDIR_JSON = {
    "intervals": [{"sum": {"bits_per_second": 600e6}, "sum_bidir_reverse": {"bits_per_second": 650e6}}],
    "end": {
        "sum_sent": {"bits_per_second": 580e6}, "sum_received": {"bits_per_second": 571e6},
        "sum_sent_bidir_reverse": {"bits_per_second": 660e6}, "sum_received_bidir_reverse": {"bits_per_second": 648e6},
    },
}


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", hang: bool = False) -> None:
        self.stdout, self.stderr, self.hang = stdout, stderr, hang
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await self._done.wait()
            return b"", b""
        self.returncode = 0
        return self.stdout, self.stderr

    async def wait(self) -> int:
        if self.hang:
            await self._done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()


class FakeMonitor:
    def __init__(self, name: str = "Ethernet", status: str = "UP", link: int = 1000) -> None:
        self.info = {
            "name": name, "ipv4_address": "192.168.1.20", "mac_address": "AA:BB:CC:DD:EE:FF",
            "state": status, "status": status, "link_speed_mbps": link, "link_speed_label": "1 Gbps",
        }

    def get_interface_info(self, name: str):
        return dict(self.info) if name == self.info["name"] else None

    def read_counters(self, name: str):
        return CounterSnapshot(0, 0, 0, 0, 0, 0, 0, 0)

    def discover_interfaces(self):
        return [dict(self.info)]


async def no_wifi():
    return {"available": False, "reason": "n/a", "interfaces": {}}


CAPS = {
    "iperf3_installed": True, "iperf3_version": "3.17.1", "tcp_supported": True, "udp_supported": True,
    "bidirectional_supported": True, "executable": IPERF, "version_tuple": [3, 17, 1],
}


def request(**overrides) -> SpeedTestStartRequest:
    payload = {"interface": "Ethernet", "server": "192.168.1.100", "port": 5201, "mode": "full",
               "duration_seconds": 5, "parallel_streams": 4, "udp_bandwidth_mbps": 100}
    payload.update(overrides)
    return SpeedTestStartRequest(**payload)


# ------------------------------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_valid_server_addresses(self) -> None:
        self.assertEqual(validate_server_address(" 192.168.1.100 "), "192.168.1.100")
        self.assertEqual(validate_server_address("iperf-server.lab.local"), "iperf-server.lab.local")
        self.assertEqual(validate_server_address("localhost"), "localhost")

    def test_invalid_and_injection_server_addresses(self) -> None:
        for bad in [
            "", "   ", "-c", "--help", "1.2.3.4 -R", "1.2.3.4;calc", "1.2.3.4 & calc", "a|b", "$(whoami)",
            "`calc`", "x\nB", "host_name", "999.1.1.1", "1.2.3", "0.0.0.0", "224.0.0.1", "255.255.255.255",
            "::1", "fe80::1", "a" * 254, "server/../../etc", "\"quoted\"",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_server_address(bad)

    def test_request_model_rejects_invalid_values(self) -> None:
        for override in [
            {"port": 0}, {"port": 70000}, {"port": "5201; calc"}, {"duration_seconds": 7}, {"parallel_streams": 3},
            {"udp_bandwidth_mbps": 0}, {"udp_bandwidth_mbps": 20000}, {"mode": "shell"},
            {"server": "10.0.0.1 --logfile C:\\x"}, {"interface": ""},
        ]:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                request(**override)

    def test_request_model_has_no_free_form_iperf_flags(self) -> None:
        with self.assertRaises(ValidationError):
            SpeedTestStartRequest(interface="Ethernet", server="1.1.1.1", extra_args="-R")  # type: ignore[call-arg]
        self.assertNotIn("extra_args", request().model_dump())

    def test_config_defaults_do_not_crash_on_missing_keys(self) -> None:
        config = AppConfig.from_mapping({"interface": "Wi-Fi"})
        self.assertEqual(config.speedtest_port, 5201)
        self.assertEqual(config.speedtest_server, "")
        self.assertEqual(config.speedtest_efficiency_normal_percent, 85.0)


class CommandTests(unittest.TestCase):
    def test_tcp_upload_command(self) -> None:
        args = build_iperf_command(IPERF, server="192.168.1.100", port=5201, duration=10, parallel_streams=4)
        self.assertEqual(args, [IPERF, "-c", "192.168.1.100", "-p", "5201", "-t", "10", "-J", "-P", "4"])

    def test_tcp_download_uses_reverse_mode(self) -> None:
        args = build_iperf_command(IPERF, server="192.168.1.100", port=5201, duration=10, parallel_streams=4, reverse=True)
        self.assertIn("-R", args)
        self.assertIn("-J", args)

    def test_udp_command(self) -> None:
        args = build_iperf_command(IPERF, server="192.168.1.100", port=5201, duration=10, udp=True, udp_bandwidth_mbps=100)
        self.assertEqual(args[-5:], ["-u", "-b", "100000K", "-P", "1"])

    def test_bind_bidir_and_connect_timeout(self) -> None:
        args = build_iperf_command(IPERF, server="10.0.0.5", port=5201, duration=5, bidirectional=True,
                                   bind_address="10.0.0.2", connect_timeout_ms=3000)
        self.assertIn("--bidir", args)
        self.assertEqual(args[args.index("-B") + 1], "10.0.0.2")
        self.assertEqual(args[args.index("--connect-timeout") + 1], "3000")

    def test_command_is_list_of_plain_tokens(self) -> None:
        args = build_iperf_command(IPERF, server="iperf.lab", port=5201, duration=30, parallel_streams=8)
        self.assertIsInstance(args, list)
        for token in args[1:]:
            self.assertRegex(token, r"^[A-Za-z0-9.\-]+$")

    def test_command_rejects_injection_and_bad_values(self) -> None:
        bad_calls = [
            dict(server="1.2.3.4; calc", port=5201, duration=10),
            dict(server="-R", port=5201, duration=10),
            dict(server="1.2.3.4", port="5201", duration=10),
            dict(server="1.2.3.4", port=True, duration=10),
            dict(server="1.2.3.4", port=0, duration=10),
            dict(server="1.2.3.4", port=5201, duration="10"),
            dict(server="1.2.3.4", port=5201, duration=3600),
            dict(server="1.2.3.4", port=5201, duration=10, parallel_streams=128),
            dict(server="1.2.3.4", port=5201, duration=10, reverse=True, bidirectional=True),
            dict(server="1.2.3.4", port=5201, duration=10, udp=True, udp_bandwidth_mbps=0),
            dict(server="1.2.3.4", port=5201, duration=10, bind_address="1.2.3.4 -s"),
        ]
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_iperf_command(IPERF, **kwargs)


class ParsingTests(unittest.TestCase):
    def test_tcp_upload_extraction(self) -> None:
        result = extract_tcp_result(parse_iperf_json(json.dumps(TCP_UPLOAD_JSON).encode()))
        self.assertEqual(result["throughput_mbps"], 936.8)
        self.assertEqual(result["sender_mbps"], 940.0)
        self.assertEqual(result["peak_mbps"], 951.5)  # omitted interval ignored
        self.assertEqual(result["retransmits"], 12)
        self.assertEqual(result["direction"], "CLIENT_TO_SERVER")

    def test_tcp_download_extraction(self) -> None:
        result = extract_tcp_result(parse_iperf_json(json.dumps(TCP_DOWNLOAD_JSON)))
        self.assertEqual(result["throughput_mbps"], 941.2)
        self.assertEqual(result["peak_mbps"], 945.0)
        self.assertIsNone(result["retransmits"])
        self.assertEqual(result["direction"], "SERVER_TO_CLIENT")

    def test_udp_extraction_jitter_and_loss(self) -> None:
        result = extract_udp_result(UDP_JSON, target_mbps=100)
        self.assertEqual(result["throughput_mbps"], 99.98)
        self.assertEqual(result["jitter_ms"], 0.031)
        self.assertEqual(result["lost_datagrams"], 12)
        self.assertEqual(result["total_datagrams"], 86000)
        self.assertAlmostEqual(result["packet_loss_percent"], 0.014, places=3)

    def test_udp_legacy_extraction_approximates_delivered_rate(self) -> None:
        result = extract_udp_result(UDP_LEGACY_JSON)
        self.assertEqual(result["throughput_mbps"], 45.0)
        self.assertEqual(result["packet_loss_percent"], 10.0)

    def test_bidir_extraction(self) -> None:
        result = extract_bidir_result(BIDIR_JSON)
        self.assertEqual(result["upload"]["throughput_mbps"], 571.0)
        self.assertEqual(result["download"]["throughput_mbps"], 648.0)
        self.assertEqual(result["download"]["peak_mbps"], 650.0)

    def test_malformed_json(self) -> None:
        for raw in [b"not json", b"[1,2]", b"{\"end\": "]:
            with self.subTest(raw=raw), self.assertRaises(SpeedTestError) as ctx:
                parse_iperf_json(raw)
            self.assertEqual(ctx.exception.code, "MALFORMED_OUTPUT")
        with self.assertRaises(SpeedTestError):
            extract_tcp_result({"end": {}})

    def test_iperf_error_mapping(self) -> None:
        cases = {
            "unable to connect to server: Connection refused": "PORT_CLOSED",
            "the server is busy running a test. try again later": "SERVER_BUSY",
            "unable to connect to server - server may have stopped running: Connection timed out": "SERVER_UNREACHABLE",
            "unable to connect to server: No route to host": "NO_ROUTE",
            "control socket has closed unexpectedly": "SERVER_TERMINATED",
            "something odd": "IPERF_ERROR",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                self.assertEqual(map_iperf_error(text).code, code)
        with self.assertRaises(SpeedTestError) as ctx:
            parse_iperf_json(json.dumps({"error": "unable to connect to server: Connection refused"}))
        self.assertEqual(ctx.exception.code, "PORT_CLOSED")

    def test_version_parsing(self) -> None:
        self.assertEqual(parse_iperf_version("iperf 3.17.1 (cJSON 1.7.15)")[1], (3, 17, 1))
        self.assertEqual(parse_iperf_version("iperf 3.1.3\nCYGWIN_NT-10.0")[0], "3.1.3")
        self.assertEqual(parse_iperf_version("garbage"), (None, None))


class EfficiencyTests(unittest.TestCase):
    def test_efficiency_calculation(self) -> None:
        self.assertEqual(calculate_efficiency(941, 1000), 94.1)
        self.assertEqual(calculate_efficiency(650, 1200), 54.2)
        self.assertIsNone(calculate_efficiency(500, 0))
        self.assertIsNone(calculate_efficiency(None, 1000))

    def test_configurable_classification(self) -> None:
        self.assertEqual(classify_efficiency(90, 85, 60), "NORMAL")
        self.assertEqual(classify_efficiency(70, 85, 60), "WARNING")
        self.assertEqual(classify_efficiency(50, 85, 60), "INVESTIGATE")
        self.assertEqual(classify_efficiency(70, 65, 50), "NORMAL")
        self.assertIsNone(classify_efficiency(None, 85, 60))

    def test_ethernet_summary_uses_link_speed(self) -> None:
        result = {
            "context_before": {"interface_type": "ethernet", "link_speed_mbps": 1000},
            "tcp_download": {"throughput_mbps": 941.2}, "tcp_upload": {"throughput_mbps": 936.8},
            "latency": {"received": 20, "latency_avg_ms": 1.7, "jitter_ms": 0.3, "packet_loss_percent": 0.0},
        }
        summary = calculate_summary(result)
        self.assertEqual(summary["efficiency"]["label"], "LINK THROUGHPUT EFFICIENCY")
        self.assertEqual(summary["efficiency"]["download_percent"], 94.1)
        self.assertEqual(summary["efficiency"]["download_health"], "NORMAL")
        self.assertEqual(summary["latency_ms"], 1.7)

    def test_wifi_summary_uses_phy_rate(self) -> None:
        result = {
            "context_before": {"interface_type": "wifi", "link_speed_mbps": 1200,
                               "wifi": {"phy_rx_mbps": 1200, "phy_tx_mbps": 1200}},
            "tcp_download": {"throughput_mbps": 650}, "tcp_upload": {"throughput_mbps": 571},
            "latency": {"received": 0, "latency_avg_ms": None},
        }
        summary = calculate_summary(result)
        self.assertEqual(summary["efficiency"]["label"], "PHY THROUGHPUT EFFICIENCY")
        self.assertEqual(summary["efficiency"]["basis"], "wifi_phy_rate")
        self.assertEqual(summary["efficiency"]["download_percent"], 54.2)
        self.assertEqual(summary["efficiency"]["download_health"], "INVESTIGATE")
        self.assertIsNone(summary["packet_loss_percent"])  # no ICMP replies -> not reported as 100 %

    def test_comparison(self) -> None:
        current = {"tcp_download_mbps": 900.0, "tcp_upload_mbps": 800.0, "latency_ms": 2.0, "packet_loss_percent": 0.0}
        previous = {"id": 1, "timestamp": "t", "tcp_download_mbps": 1000.0, "tcp_upload_mbps": 800.0, "latency_ms": 1.5, "packet_loss_percent": 0.5}
        cmp = compare_results(current, previous)
        self.assertEqual(cmp["download"]["difference_percent"], -10.0)
        self.assertEqual(cmp["upload"]["difference_percent"], 0.0)
        self.assertEqual(cmp["latency"]["difference_ms"], 0.5)
        self.assertEqual(cmp["packet_loss"]["difference_percent_points"], -0.5)
        self.assertIsNone(compare_results(current, None))


class LatencyAndWifiTests(unittest.TestCase):
    def test_rtt_summary(self) -> None:
        summary = summarize_rtts(5, [1.0, 2.0, 1.0, 3.0])
        self.assertEqual(summary["packet_loss_percent"], 20.0)
        self.assertEqual(summary["latency_avg_ms"], 1.75)
        self.assertAlmostEqual(summary["jitter_ms"], (1 + 1 + 2) / 3, places=3)
        self.assertIsNone(summarize_rtts(3, [])["jitter_ms"])

    def test_netsh_english_parsing(self) -> None:
        text = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6E AX211 160MHz
    State                  : connected
    SSID                   : Factory-WiFi
    AP BSSID               : aa:bb:cc:dd:ee:ff
    Band                   : 5 GHz
    Channel                : 44
    Radio type             : 802.11ax
    Receive rate (Mbps)    : 1200
    Transmit rate (Mbps)   : 1201.5
    Signal                 : 86%
    Rssi                   : -57
    Profile                : Factory-WiFi

    Hosted network status  : Not available
"""
        wifi = parse_netsh_wlan_interfaces(text)["Wi-Fi"]
        self.assertEqual(wifi["ssid"], "Factory-WiFi")
        self.assertEqual(wifi["bssid"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(wifi["channel"], 44)
        self.assertEqual(wifi["band"], "5 GHz")
        self.assertEqual(wifi["phy_rx_mbps"], 1200.0)
        self.assertEqual(wifi["phy_tx_mbps"], 1201.5)
        self.assertEqual(wifi["rssi_dbm"], -57)
        self.assertFalse(wifi["rssi_estimated"])
        self.assertEqual(wifi["state"], "connected")

    def test_netsh_german_parsing_with_estimated_rssi(self) -> None:
        text = """
    Name                   : WLAN
    Status                 : Verbunden
    SSID                   : Lab
    BSSID                  : 11:22:33:44:55:66
    Kanal                  : 6
    Empfangsrate (MBit/s)  : 286,8
    Übertragungsrate (MBit/s) : 144
    Signal                 : 80%
"""
        wifi = parse_netsh_wlan_interfaces(text)["WLAN"]
        self.assertEqual(wifi["phy_rx_mbps"], 286.8)
        self.assertEqual(wifi["phy_tx_mbps"], 144.0)
        self.assertEqual(wifi["rssi_dbm"], -60)
        self.assertTrue(wifi["rssi_estimated"])

    def test_netsh_permission_denied_output(self) -> None:
        text = "Network shell commands need location permission to access WLAN information.\nFunction WlanQueryInterface returns error 5:"
        self.assertEqual(parse_netsh_wlan_interfaces(text), {})

    def test_interface_classification(self) -> None:
        self.assertEqual(classify_interface("Wi-Fi"), "wifi")
        self.assertEqual(classify_interface("Ethernet 2"), "ethernet")
        self.assertEqual(classify_interface("Loopback Pseudo-Interface 1"), "loopback")
        self.assertEqual(classify_interface("Corp", {"Corp"}), "wifi")


class StoreTests(unittest.TestCase):
    def test_persistence_and_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SpeedTestStore(Path(tmp) / "h.db")
            base = {
                "timestamp": "2026-01-01T00:00:00Z", "interface": "Ethernet", "server": "192.168.1.100", "port": 5201,
                "mode": "full", "status": "COMPLETED", "duration_seconds": 10, "parallel_streams": 4,
                "context_before": {"interface_type": "ethernet", "link_speed_mbps": 1000},
                "tcp_download": {"throughput_mbps": 941.2}, "tcp_upload": {"throughput_mbps": 936.8, "retransmits": 3},
                "udp": {"throughput_mbps": 99.9, "jitter_ms": 0.03, "lost_datagrams": 1, "total_datagrams": 100, "packet_loss_percent": 1.0},
                "latency": {"received": 20, "latency_avg_ms": 1.7, "jitter_ms": 0.3, "packet_loss_percent": 0.0},
            }
            first = store.save(base)
            second = store.save({**base, "timestamp": "2026-01-01T01:00:00Z", "status": "FAILED", "error": {"code": "X", "message": "m"}})
            items = store.list(limit=10)
            self.assertEqual([item["id"] for item in items], [second, first])
            self.assertEqual(items[1]["tcp_download_mbps"], 941.2)
            self.assertEqual(items[1]["udp_lost_datagrams"], 1)
            self.assertEqual(items[0]["error_code"], "X")
            self.assertNotIn("details_json", items[0])
            self.assertEqual(store.previous_completed("Ethernet", second + 1)["id"], first)
            self.assertIsNone(store.previous_completed("Ethernet", first))
            self.assertEqual(store.latest()["details"]["status"], "FAILED")


class NoShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_uses_exec_with_argument_array(self) -> None:
        fake = FakeProcess()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake)) as exec_mock, \
                patch("asyncio.create_subprocess_shell", new=AsyncMock()) as shell_mock:
            result = await process_utils.spawn([IPERF, "-c", "192.168.1.100", "-J"])
        self.assertIs(result, fake)
        exec_mock.assert_awaited_once()
        self.assertEqual(exec_mock.await_args.args, (IPERF, "-c", "192.168.1.100", "-J"))
        self.assertNotIn("shell", exec_mock.await_args.kwargs)
        shell_mock.assert_not_called()

    async def test_spawn_fallback_popen_uses_shell_false(self) -> None:
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=NotImplementedError)), \
                patch("subprocess.Popen") as popen:
            await process_utils.spawn([IPERF, "--version"])
        self.assertEqual(popen.call_args.args[0], [IPERF, "--version"])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    async def test_spawn_rejects_string_commands(self) -> None:
        for bad in ["iperf3 -c 1.2.3.4 & calc", [], [IPERF, 5201], [IPERF, "a\x00b"]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await process_utils.spawn(bad)  # type: ignore[arg-type]

    def test_source_has_no_shell_execution(self) -> None:
        for path in APP_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("shell=True", source)
                self.assertNotIn("create_subprocess_shell", source)
                self.assertNotIn("os.system", source)

    def test_configured_executable_must_be_iperf3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evil = Path(tmp) / "cmd.exe"
            evil.write_text("x")
            with patch("shutil.which", return_value=None):
                self.assertEqual(resolve_iperf_executable(str(evil)), (None, "none"))
            good = Path(tmp) / "iperf3.exe"
            good.write_text("x")
            path, source = resolve_iperf_executable(str(good))
            self.assertEqual(source, "config")
            self.assertTrue(path.endswith("iperf3.exe"))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SpeedTestStore(Path(self.tmp.name) / "h.db")
        self.messages: list[dict] = []

        async def broadcast(payload):
            self.messages.append(payload)

        self.service = SpeedTestService(
            FakeMonitor(), AppConfig(), self.store, broadcast,
            latency_probe=lambda dest, src, timeout: 1.5, wifi_query=no_wifi,
        )
        self.service._capabilities = dict(CAPS)
        self.service.check_iperf = AsyncMock(return_value=dict(CAPS))  # type: ignore[method-assign]
        self.service.check_server = AsyncMock(return_value="192.168.1.100")  # type: ignore[method-assign]
        self.patches = [
            patch("app.speedtest.LATENCY_PHASE_SECONDS", 0.2),
            patch("app.speedtest.LATENCY_INTERVAL_SECONDS", 0.05),
        ]
        for item in self.patches:
            item.start()
        self.spawned: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _fake_spawn(self, hang: bool = False):
        async def fake(args, **kwargs):
            self.spawned.append(list(args))
            if "-u" in args:
                payload = UDP_JSON
            elif "-R" in args:
                payload = TCP_DOWNLOAD_JSON
            else:
                payload = TCP_UPLOAD_JSON
            process = FakeProcess(stdout=json.dumps(payload).encode(), hang=hang)
            self.processes.append(process)
            return process
        return fake

    async def test_full_test_completes_and_persists(self) -> None:
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn()):
            await self.service.start_test(request(mode="full"))
            await self.service._task
        result = self.service.get_last_result()
        self.assertEqual(result["status"], "COMPLETED", result.get("error"))
        self.assertEqual(result["tcp_upload"]["throughput_mbps"], 936.8)
        self.assertEqual(result["tcp_download"]["throughput_mbps"], 941.2)
        self.assertEqual(result["udp"]["lost_datagrams"], 12)
        self.assertEqual(result["latency"]["latency_avg_ms"], 1.5)
        self.assertEqual(result["summary"]["efficiency"]["download_percent"], 94.1)
        self.assertIsNotNone(result["context_before"])
        self.assertIsNotNone(result["context_after"])
        self.assertEqual(len(self.spawned), 3)
        self.assertNotIn("-R", self.spawned[0])
        self.assertIn("-R", self.spawned[1])
        self.assertIn("-u", self.spawned[2])
        for args in self.spawned:
            self.assertEqual(args[0], IPERF)
            self.assertEqual(args[args.index("-B") + 1], "192.168.1.20")
        self.assertEqual(self.store.list()[0]["status"], "COMPLETED")
        self.assertTrue(any(m.get("type") == "speedtest_complete" for m in self.messages))
        self.assertTrue(any(m.get("type") == "speedtest_progress" and m.get("status") == "TCP_DOWNLOAD" for m in self.messages))
        self.assertEqual(self.service.get_test_status()["status"], "COMPLETED")

    async def test_quick_test_does_not_run_iperf(self) -> None:
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn()) as spawn_mock:
            await self.service.start_test(request(mode="quick"))
            await self.service._task
        spawn_mock.assert_not_called()
        self.assertEqual(self.service.get_last_result()["status"], "COMPLETED")

    async def test_concurrent_start_is_rejected(self) -> None:
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn(hang=True)):
            await self.service.start_test(request(mode="tcp"))
            with self.assertRaises(SpeedTestBusyError):
                await self.service.start_test(request(mode="tcp"))
            await self.service.cancel_test()

    async def test_api_returns_409_when_running(self) -> None:
        app_main.app.state.speedtest = self.service
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn(hang=True)):
            await self.service.start_test(request(mode="tcp"))
            with self.assertRaises(HTTPException) as ctx:
                await app_main.speedtest_start(request(mode="tcp"))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail, "SPEED TEST ALREADY RUNNING")
            await self.service.cancel_test()

    async def _wait_for_state(self, state: SpeedTestState) -> None:
        for _ in range(300):
            if self.service._state == state and self.processes:
                return
            await asyncio.sleep(0.01)
        self.fail(f"state {state} not reached")

    async def test_cancellation_terminates_process(self) -> None:
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn(hang=True)):
            await self.service.start_test(request(mode="tcp"))
            await self._wait_for_state(SpeedTestState.TCP_UPLOAD)
            status = await self.service.cancel_test()
        self.assertEqual(status["status"], "CANCELLED")
        self.assertTrue(self.processes[0].terminated)
        self.assertIsNone(self.service._process)
        self.assertEqual(len(self.processes), 1)  # no further phases started
        self.assertEqual(self.store.list()[0]["status"], "CANCELLED")

    async def test_timeout_terminates_hung_iperf(self) -> None:
        with patch("app.speedtest.spawn", side_effect=self._fake_spawn(hang=True)), \
                patch("app.speedtest.IPERF_TIMEOUT_MARGIN_SECONDS", -4.9):
            await self.service.start_test(request(mode="tcp", duration_seconds=5))
            await self.service._task
        result = self.service.get_last_result()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "IPERF_TIMEOUT")
        self.assertTrue(self.processes[0].terminated)

    async def test_iperf_missing(self) -> None:
        self.service.check_iperf = AsyncMock(return_value={**CAPS, "iperf3_installed": False, "executable": None})  # type: ignore[method-assign]
        await self.service.start_test(request(mode="tcp"))
        await self.service._task
        result = self.service.get_last_result()
        self.assertEqual(result["error"]["code"], "IPERF3_NOT_INSTALLED")
        self.assertIn("IPERF3 NOT INSTALLED", result["error"]["message"])

    async def test_interface_down_and_missing(self) -> None:
        self.service.monitor = FakeMonitor(status="DOWN")
        await self.service.start_test(request(mode="tcp"))
        await self.service._task
        self.assertEqual(self.service.get_last_result()["error"]["code"], "INTERFACE_DOWN")
        await self.service.start_test(request(mode="tcp", interface="Nope"))
        await self.service._task
        self.assertEqual(self.service.get_last_result()["error"]["code"], "INTERFACE_NOT_FOUND")

    async def test_iperf_error_json_is_reported_cleanly(self) -> None:
        async def fake(args, **kwargs):
            process = FakeProcess(stdout=json.dumps({"error": "unable to connect to server: Connection refused"}).encode())
            self.processes.append(process)
            return process

        with patch("app.speedtest.spawn", side_effect=fake):
            await self.service.start_test(request(mode="tcp"))
            await self.service._task
        error = self.service.get_last_result()["error"]
        self.assertEqual(error["code"], "PORT_CLOSED")
        self.assertNotIn("Traceback", error["message"])

    async def test_unexpected_exception_is_not_leaked(self) -> None:
        with patch("app.speedtest.spawn", side_effect=RuntimeError("secret internal path C:\\x")):
            await self.service.start_test(request(mode="tcp"))
            await self.service._task
        error = self.service.get_last_result()["error"]
        self.assertEqual(error["code"], "INTERNAL_ERROR")
        self.assertNotIn("secret", error["message"])


class ServerCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = SpeedTestService(FakeMonitor(), AppConfig(), SpeedTestStore(Path(self.tmp.name) / "h.db"))

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_closed_port_is_reported(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(SpeedTestError) as ctx:
            await self.service.check_server("127.0.0.1", port, None)
        self.assertIn(ctx.exception.code, ("PORT_CLOSED", "SERVER_UNREACHABLE"))

    async def test_open_port_is_reachable(self) -> None:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            self.assertEqual(await self.service.check_server("127.0.0.1", port, None), "127.0.0.1")
        finally:
            server.close()
            await server.wait_closed()

    async def test_dns_failure(self) -> None:
        with self.assertRaises(SpeedTestError) as ctx:
            await self.service.check_server("does-not-exist.invalid", 5201, None)
        self.assertEqual(ctx.exception.code, "DNS_FAILURE")

    async def test_iperf_detection_not_installed(self) -> None:
        with patch("app.speedtest.shutil.which", return_value=None):
            caps = await self.service.check_iperf(force=True)
        self.assertFalse(caps["iperf3_installed"])
        self.assertIn("install_hint", self.service.public_capabilities(caps))
        self.assertNotIn("executable", self.service.public_capabilities(caps))

    async def test_iperf_detection_installed(self) -> None:
        with patch("app.speedtest.shutil.which", return_value=IPERF), \
                patch("app.speedtest.run_capture", new=AsyncMock(return_value=(0, b"iperf 3.17.1 (cJSON 1.7.15)\n", b""))) as run:
            caps = await self.service.check_iperf(force=True)
        run.assert_awaited_once_with([IPERF, "--version"], timeout_seconds=5)
        self.assertTrue(caps["iperf3_installed"])
        self.assertEqual(caps["iperf3_version"], "3.17.1")
        self.assertTrue(caps["bidirectional_supported"])
        with patch("app.speedtest.shutil.which", return_value=IPERF), \
                patch("app.speedtest.run_capture", new=AsyncMock(return_value=(0, b"iperf 3.1.3\n", b""))):
            caps = await self.service.check_iperf(force=True)
        self.assertFalse(caps["bidirectional_supported"])


if __name__ == "__main__":
    unittest.main()
