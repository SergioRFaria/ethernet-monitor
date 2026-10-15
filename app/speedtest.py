"""Active network speed test built on iPerf3 (client mode) plus low-rate ICMP latency probing.

Security notes:
- iperf3 is always started with an argument array via asyncio.create_subprocess_exec (no shell).
- Only validated values (IPv4 address, int port/duration/streams, numeric bandwidth) reach the command.
- The executable comes from PATH, config.json or the app bundle, never from the browser.
"""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import re
import shutil
import socket
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable

from .latency import default_probe, run_icmp_latency
from .models import SpeedTestStartRequest, validate_ipv4_bind_address, validate_server_address
from .network_context import capture_context
from .paths import resource_root
from .process_utils import run_capture, spawn, stop_process
from .speedtest_store import SpeedTestStore

logger = logging.getLogger("ethernet_monitor.speedtest")

ALLOWED_DURATIONS = (5, 10, 20, 30)
ALLOWED_STREAMS = (1, 2, 4, 8)
LATENCY_PHASE_SECONDS = 5
LATENCY_INTERVAL_SECONDS = 0.25
IPERF_TIMEOUT_MARGIN_SECONDS = 20
SERVER_CHECK_TIMEOUT_SECONDS = 3.0
INSTALL_HINT = (
    "Install iPerf3 and make sure iperf3.exe is on PATH (or set \"iperf3_path\" in config.json). "
    "Windows: 'winget install iperf3' or download a Windows build from https://iperf.fr/iperf-download.php."
)


class SpeedTestState(str, Enum):
    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    LATENCY_TEST = "LATENCY_TEST"
    TCP_UPLOAD = "TCP_UPLOAD"
    TCP_DOWNLOAD = "TCP_DOWNLOAD"
    TCP_BIDIRECTIONAL = "TCP_BIDIRECTIONAL"
    UDP_TEST = "UDP_TEST"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_STATES = {
    SpeedTestState.VALIDATING,
    SpeedTestState.LATENCY_TEST,
    SpeedTestState.TCP_UPLOAD,
    SpeedTestState.TCP_DOWNLOAD,
    SpeedTestState.TCP_BIDIRECTIONAL,
    SpeedTestState.UDP_TEST,
}


class SpeedTestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SpeedTestBusyError(Exception):
    pass


class _UserCancelled(Exception):
    pass


# Ordered: more specific patterns first.
_IPERF_ERROR_PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    (("server is busy",), "SERVER_BUSY", "The iPerf3 server is busy running another test. Try again later."),
    (("connection refused",), "PORT_CLOSED", "Connection refused: no iPerf3 server is listening on that port, or the port is blocked."),
    (("no route to host", "network is unreachable", "host is unreachable"), "NO_ROUTE", "No route to host: check the interface, subnet and gateway."),
    (("name or service not known", "nodename nor servname", "no address associated", "unknown host", "getaddrinfo"), "DNS_FAILURE", "DNS resolution failed for the server name."),
    (("control socket has closed", "connection reset", "unable to receive results", "broken pipe", "server has terminated"), "SERVER_TERMINATED", "The iPerf3 server closed the connection unexpectedly."),
    (("cannot assign requested address", "unable to bind", "bind"), "BIND_FAILED", "Could not bind to the selected interface address."),
    (("timed out", "timeout"), "SERVER_UNREACHABLE", "IPERF SERVER UNREACHABLE (connection timed out)."),
    (("unable to connect",), "SERVER_UNREACHABLE", "IPERF SERVER UNREACHABLE."),
]


def _sanitize(text: str, limit: int = 200) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "").strip()
    return cleaned[:limit]


def map_iperf_error(message: str) -> SpeedTestError:
    lowered = (message or "").lower()
    for needles, code, friendly in _IPERF_ERROR_PATTERNS:
        if any(needle in lowered for needle in needles):
            return SpeedTestError(code, friendly)
    return SpeedTestError("IPERF_ERROR", f"iPerf3 reported an error: {_sanitize(message) or 'unknown error'}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------------
# iperf3 discovery / command construction
# --------------------------------------------------------------------------------------------

def resolve_iperf_executable(configured_path: str = "") -> tuple[str | None, str]:
    """Return (path, source); only files literally named iperf3/iperf3.exe are accepted."""
    if configured_path:
        candidate = Path(configured_path)
        if candidate.is_file() and candidate.name.lower() in ("iperf3", "iperf3.exe"):
            return str(candidate.resolve()), "config"
        logger.warning("Configured iperf3_path is invalid; falling back to PATH lookup.")
    bundled = resource_root() / "vendor" / "iperf3" / "iperf3.exe"
    if bundled.is_file():
        return str(bundled.resolve()), "bundled"
    found = shutil.which("iperf3")
    return (found, "PATH") if found else (None, "none")


def parse_iperf_version(text: str) -> tuple[str | None, tuple[int, ...] | None]:
    match = re.search(r"iperf\s+(\d+)\.(\d+)(?:\.(\d+))?", text or "", re.IGNORECASE)
    if not match:
        return None, None
    parts = tuple(int(part) for part in match.groups() if part is not None)
    return ".".join(str(part) for part in parts), parts


def build_iperf_command(
    executable: str,
    *,
    server: str,
    port: int,
    duration: int,
    parallel_streams: int = 1,
    reverse: bool = False,
    udp: bool = False,
    udp_bandwidth_mbps: float | None = None,
    bidirectional: bool = False,
    bind_address: str | None = None,
    connect_timeout_ms: int | None = None,
) -> list[str]:
    """Builds an iperf3 client argument list from validated values only."""
    server = validate_server_address(server)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid port.")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration not in ALLOWED_DURATIONS:
        raise ValueError("Invalid duration.")
    if isinstance(parallel_streams, bool) or not isinstance(parallel_streams, int) or parallel_streams not in ALLOWED_STREAMS:
        raise ValueError("Invalid parallel stream count.")
    if reverse and bidirectional:
        raise ValueError("Reverse and bidirectional modes are mutually exclusive.")
    if udp and (reverse or bidirectional):
        raise ValueError("UDP test runs client to server only.")

    args = [executable, "-c", server, "-p", str(port), "-t", str(duration), "-J"]
    if udp:
        if udp_bandwidth_mbps is None or not 1.0 <= float(udp_bandwidth_mbps) <= 10000.0:
            raise ValueError("Invalid UDP bandwidth.")
        args += ["-u", "-b", f"{int(round(float(udp_bandwidth_mbps) * 1000))}K", "-P", "1"]
    else:
        args += ["-P", str(parallel_streams)]
    if reverse:
        args.append("-R")
    if bidirectional:
        args.append("--bidir")
    if bind_address:
        args += ["-B", validate_ipv4_bind_address(bind_address)]
    if connect_timeout_ms:
        args += ["--connect-timeout", str(int(connect_timeout_ms))]
    return args


# --------------------------------------------------------------------------------------------
# JSON parsing
# --------------------------------------------------------------------------------------------

def parse_iperf_json(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 returned malformed JSON output.") from exc
    if not isinstance(data, dict):
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 returned unexpected JSON output.")
    if data.get("error"):
        raise map_iperf_error(str(data["error"]))
    return data


def _mbps(value: Any) -> float | None:
    try:
        return round(float(value) / 1_000_000, 3)
    except (TypeError, ValueError):
        return None


def _interval_peaks(data: dict[str, Any], key: str) -> float | None:
    values = []
    for interval in data.get("intervals") or []:
        summary = (interval or {}).get(key) or {}
        if summary.get("omitted"):
            continue
        mbps = _mbps(summary.get("bits_per_second"))
        if mbps is not None:
            values.append(mbps)
    return max(values) if values else None


def _tcp_direction(sent: dict[str, Any], received: dict[str, Any], peak: float | None) -> dict[str, Any]:
    receiver_mbps = _mbps(received.get("bits_per_second"))
    sender_mbps = _mbps(sent.get("bits_per_second"))
    throughput = receiver_mbps if receiver_mbps is not None else sender_mbps
    if throughput is None:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 JSON did not contain TCP throughput.")
    retransmits = sent.get("retransmits")
    return {
        "throughput_mbps": throughput,
        "average_mbps": throughput,
        "peak_mbps": peak if peak is not None else throughput,
        "sender_mbps": sender_mbps,
        "receiver_mbps": receiver_mbps,
        "bytes_transferred": received.get("bytes") or sent.get("bytes"),
        "measured_seconds": round(float(received.get("seconds") or sent.get("seconds") or 0.0), 3),
        "retransmits": int(retransmits) if isinstance(retransmits, (int, float)) else None,
    }


def extract_tcp_result(data: dict[str, Any]) -> dict[str, Any]:
    """Receiver-side throughput is reported (goodput at the receiving end)."""
    end = data.get("end") or {}
    sent = end.get("sum_sent") or {}
    received = end.get("sum_received") or {}
    if not sent and not received:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 JSON did not contain TCP summary data.")
    test_start = (data.get("start") or {}).get("test_start") or {}
    result = _tcp_direction(sent, received, _interval_peaks(data, "sum"))
    result["reverse"] = bool(test_start.get("reverse"))
    result["streams"] = test_start.get("num_streams")
    result["direction"] = "SERVER_TO_CLIENT" if result["reverse"] else "CLIENT_TO_SERVER"
    return result


def extract_bidir_result(data: dict[str, Any]) -> dict[str, Any]:
    end = data.get("end") or {}
    if "sum_sent_bidir_reverse" not in end and "sum_received_bidir_reverse" not in end:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 JSON did not contain bidirectional results.")
    upload = _tcp_direction(end.get("sum_sent") or {}, end.get("sum_received") or {}, _interval_peaks(data, "sum"))
    download = _tcp_direction(
        end.get("sum_sent_bidir_reverse") or {},
        end.get("sum_received_bidir_reverse") or {},
        _interval_peaks(data, "sum_bidir_reverse"),
    )
    return {"upload": upload, "download": download}


def extract_udp_result(data: dict[str, Any], target_mbps: float | None = None) -> dict[str, Any]:
    end = data.get("end") or {}
    received = end.get("sum_received") or {}
    summary = end.get("sum") or {}
    stats = received if received.get("packets") else summary
    if not stats:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 JSON did not contain UDP summary data.")

    total = stats.get("packets")
    lost = stats.get("lost_packets")
    loss_percent = stats.get("lost_percent")
    if loss_percent is None and isinstance(total, (int, float)) and total:
        loss_percent = (lost or 0) / total * 100.0
    jitter = stats.get("jitter_ms", summary.get("jitter_ms"))

    if received.get("bits_per_second") is not None:
        throughput = _mbps(received.get("bits_per_second"))
    else:
        # Older iperf3 only reports sender rate; approximate delivered rate from the loss ratio.
        sender = _mbps(summary.get("bits_per_second"))
        throughput = round(sender * (1 - (loss_percent or 0) / 100.0), 3) if sender is not None else None
    if throughput is None:
        raise SpeedTestError("MALFORMED_OUTPUT", "iPerf3 JSON did not contain UDP throughput.")

    return {
        "throughput_mbps": throughput,
        "sender_mbps": _mbps(summary.get("bits_per_second")),
        "target_mbps": target_mbps,
        "jitter_ms": round(float(jitter), 3) if jitter is not None else None,
        "lost_datagrams": int(lost) if lost is not None else None,
        "total_datagrams": int(total) if total is not None else None,
        "packet_loss_percent": round(float(loss_percent), 3) if loss_percent is not None else None,
        "direction": "CLIENT_TO_SERVER",
    }


# --------------------------------------------------------------------------------------------
# Summary / efficiency / comparison
# --------------------------------------------------------------------------------------------

def calculate_efficiency(measured_mbps: float | None, reference_mbps: float | None) -> float | None:
    if measured_mbps is None or not reference_mbps or reference_mbps <= 0:
        return None
    return round(measured_mbps / reference_mbps * 100.0, 1)


def classify_efficiency(value: float | None, normal_percent: float, warning_percent: float) -> str | None:
    if value is None:
        return None
    if value >= normal_percent:
        return "NORMAL"
    if value >= warning_percent:
        return "WARNING"
    return "INVESTIGATE"


def calculate_summary(result: dict[str, Any], normal_percent: float = 85.0, warning_percent: float = 60.0) -> dict[str, Any]:
    context = result.get("context_before") or {}
    latency = result.get("latency") or {}
    udp = result.get("udp") or {}
    download = (result.get("tcp_download") or {}).get("throughput_mbps")
    upload = (result.get("tcp_upload") or {}).get("throughput_mbps")
    interface_type = context.get("interface_type", "unknown")
    link_speed = context.get("link_speed_mbps") or 0

    if interface_type == "wifi":
        wifi = context.get("wifi") or {}
        label = "PHY THROUGHPUT EFFICIENCY"
        down_ref, up_ref = wifi.get("phy_rx_mbps"), wifi.get("phy_tx_mbps")
        basis = "wifi_phy_rate"
        if not down_ref and not up_ref and link_speed:
            # Windows reports the current Wi-Fi PHY rate as the adapter link speed.
            down_ref = up_ref = link_speed
            basis = "adapter_link_speed"
    else:
        label = "LINK THROUGHPUT EFFICIENCY"
        down_ref = up_ref = link_speed or None
        basis = "negotiated_link_speed"

    down_eff = calculate_efficiency(download, down_ref)
    up_eff = calculate_efficiency(upload, up_ref)
    icmp_ok = bool(latency.get("received"))
    return {
        "download_mbps": download,
        "upload_mbps": upload,
        "latency_ms": latency.get("latency_avg_ms") if icmp_ok else None,
        "jitter_ms": latency.get("jitter_ms") if icmp_ok else None,
        "packet_loss_percent": latency.get("packet_loss_percent") if icmp_ok else None,
        "latency_source": "ICMP echo",
        "udp_mbps": udp.get("throughput_mbps"),
        "udp_jitter_ms": udp.get("jitter_ms"),
        "udp_packet_loss_percent": udp.get("packet_loss_percent"),
        "interface_type": interface_type,
        "link_speed_mbps": link_speed,
        "efficiency": {
            "label": label,
            "basis": basis,
            "download_reference_mbps": down_ref,
            "upload_reference_mbps": up_ref,
            "download_percent": down_eff,
            "upload_percent": up_eff,
            "download_health": classify_efficiency(down_eff, normal_percent, warning_percent),
            "upload_health": classify_efficiency(up_eff, normal_percent, warning_percent),
        },
        "thresholds": {"normal_percent": normal_percent, "warning_percent": warning_percent},
    }


def _pct_diff(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100.0, 1)


def _diff(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 3)


def compare_results(current_row: dict[str, Any], previous_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not previous_row:
        return None
    return {
        "previous_id": previous_row.get("id"),
        "previous_timestamp": previous_row.get("timestamp"),
        "download": {
            "current": current_row.get("tcp_download_mbps"),
            "previous": previous_row.get("tcp_download_mbps"),
            "difference_percent": _pct_diff(current_row.get("tcp_download_mbps"), previous_row.get("tcp_download_mbps")),
        },
        "upload": {
            "current": current_row.get("tcp_upload_mbps"),
            "previous": previous_row.get("tcp_upload_mbps"),
            "difference_percent": _pct_diff(current_row.get("tcp_upload_mbps"), previous_row.get("tcp_upload_mbps")),
        },
        "latency": {
            "current": current_row.get("latency_ms"),
            "previous": previous_row.get("latency_ms"),
            "difference_ms": _diff(current_row.get("latency_ms"), previous_row.get("latency_ms")),
        },
        "packet_loss": {
            "current": current_row.get("packet_loss_percent"),
            "previous": previous_row.get("packet_loss_percent"),
            "difference_percent_points": _diff(current_row.get("packet_loss_percent"), previous_row.get("packet_loss_percent")),
        },
    }


# --------------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------------

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class SpeedTestService:
    def __init__(
        self,
        monitor: Any,
        config: Any,
        store: SpeedTestStore,
        broadcast: Broadcast | None = None,
        *,
        latency_probe: Any = None,
        wifi_query: Any = None,
    ) -> None:
        self.monitor = monitor
        self.config = config
        self.store = store
        self._broadcast = broadcast
        self._latency_probe = latency_probe
        self._wifi_query = wifi_query
        self._state = SpeedTestState.IDLE
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._process: Any = None
        self._cancel_event = asyncio.Event()
        self._capabilities: dict[str, Any] | None = None
        self._last_result: dict[str, Any] | None = None
        self._request: SpeedTestStartRequest | None = None
        self._plan: list[tuple[SpeedTestState, float]] = []
        self._completed_seconds = 0.0
        self._phase_started = 0.0
        self._phase_expected = 0.0
        self._test_started = 0.0
        self._test_finished = 0.0
        self._message = "READY TO TEST"
        self._error: dict[str, str] | None = None

    # ---------------- capabilities ----------------
    async def check_iperf(self, force: bool = False) -> dict[str, Any]:
        if self._capabilities is not None and not force:
            return self._capabilities
        executable, source = resolve_iperf_executable(getattr(self.config, "iperf3_path", ""))
        caps: dict[str, Any] = {
            "iperf3_installed": False,
            "iperf3_version": None,
            "iperf3_source": source,
            "tcp_supported": False,
            "udp_supported": False,
            "bidirectional_supported": False,
            "icmp_latency_supported": (self._latency_probe or default_probe()) is not None,
            "install_hint": INSTALL_HINT,
            "executable": executable,
        }
        if executable:
            try:
                _, stdout, stderr = await run_capture([executable, "--version"], timeout_seconds=5)
                version, parts = parse_iperf_version((stdout + b"\n" + stderr).decode("utf-8", errors="replace"))
            except (asyncio.TimeoutError, OSError):
                version, parts = None, None
            if parts and parts[0] >= 3:
                caps.update(
                    iperf3_installed=True,
                    iperf3_version=version,
                    tcp_supported=True,
                    udp_supported=True,
                    bidirectional_supported=parts >= (3, 7),
                    version_tuple=list(parts),
                )
        self._capabilities = caps
        return caps

    def public_capabilities(self, caps: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in caps.items() if key not in ("executable", "version_tuple")}

    # ---------------- server validation ----------------
    async def check_server(self, server: str, port: int, bind_ip: str | None, probe_port: bool = True) -> str:
        server = validate_server_address(server)
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(server, port, family=socket.AF_INET, type=socket.SOCK_STREAM),
                timeout=SERVER_CHECK_TIMEOUT_SECONDS,
            )
        except (socket.gaierror, asyncio.TimeoutError) as exc:
            raise SpeedTestError("DNS_FAILURE", f"DNS resolution failed for '{server}'.") from exc
        if not infos:
            raise SpeedTestError("DNS_FAILURE", f"DNS resolution returned no IPv4 address for '{server}'.")
        server_ip = infos[0][4][0]
        if not probe_port:
            return server_ip

        local_addr = (bind_ip, 0) if bind_ip and not ipaddress.ip_address(server_ip).is_loopback else None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server_ip, port, local_addr=local_addr),
                timeout=SERVER_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise SpeedTestError("SERVER_UNREACHABLE", f"IPERF SERVER UNREACHABLE: no response from {server_ip}:{port} (host down or firewall).") from exc
        except ConnectionRefusedError as exc:
            raise SpeedTestError("PORT_CLOSED", f"IPERF SERVER UNREACHABLE: connection refused on {server_ip}:{port} (server not running or port blocked).") from exc
        except OSError as exc:
            code = getattr(exc, "winerror", None) or exc.errno
            if code in (10051, 10065, errno.ENETUNREACH, errno.EHOSTUNREACH):
                raise SpeedTestError("NO_ROUTE", f"No route to host {server_ip}.") from exc
            if code in (10049, errno.EADDRNOTAVAIL):
                raise SpeedTestError("BIND_FAILED", "Could not bind to the selected interface address.") from exc
            raise SpeedTestError("SERVER_UNREACHABLE", f"IPERF SERVER UNREACHABLE: {server_ip}:{port}.") from exc
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (asyncio.TimeoutError, OSError):
            pass
        return server_ip

    # ---------------- phases ----------------
    async def run_latency_test(self, destination_ip: str, source_ip: str | None, seconds: int) -> dict[str, Any]:
        count = max(1, int(seconds / LATENCY_INTERVAL_SECONDS))
        return await run_icmp_latency(
            destination_ip,
            source_ip,
            count=count,
            interval_seconds=LATENCY_INTERVAL_SECONDS,
            cancel_event=self._cancel_event,
            probe=self._latency_probe,
        )

    async def _run_iperf(self, args: list[str], expected_seconds: float, phase: str) -> dict[str, Any]:
        # Log only sanitized metadata, never raw user strings.
        logger.info("iperf3 phase=%s args=%d timeout=%ss", phase, len(args), expected_seconds + IPERF_TIMEOUT_MARGIN_SECONDS)
        if self._cancel_event.is_set():
            raise _UserCancelled()
        process = await spawn(args)
        self._process = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=expected_seconds + IPERF_TIMEOUT_MARGIN_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await stop_process(process)
            raise SpeedTestError("IPERF_TIMEOUT", "iPerf3 did not finish in time and was terminated.") from exc
        finally:
            if process.returncode is None:
                await stop_process(process)
            self._process = None
        if self._cancel_event.is_set():
            raise _UserCancelled()
        if not (stdout or b"").strip():
            raise map_iperf_error((stderr or b"").decode("utf-8", errors="replace") or "no output")
        return parse_iperf_json(stdout)

    def _iperf_args(self, req: SpeedTestStartRequest, server_ip: str, bind_ip: str | None, **kwargs: Any) -> list[str]:
        caps = self._capabilities or {}
        version = tuple(caps.get("version_tuple") or ())
        return build_iperf_command(
            caps["executable"],
            server=server_ip,
            port=req.port,
            duration=req.duration_seconds,
            parallel_streams=req.parallel_streams,
            bind_address=bind_ip,
            connect_timeout_ms=3000 if version >= (3, 7) else None,
            **kwargs,
        )

    async def run_tcp_upload(self, req: SpeedTestStartRequest, server_ip: str, bind_ip: str | None) -> dict[str, Any]:
        data = await self._run_iperf(self._iperf_args(req, server_ip, bind_ip), req.duration_seconds, "tcp_upload")
        return {**extract_tcp_result(data), "label": "TCP UPLOAD"}

    async def run_tcp_download(self, req: SpeedTestStartRequest, server_ip: str, bind_ip: str | None) -> dict[str, Any]:
        data = await self._run_iperf(self._iperf_args(req, server_ip, bind_ip, reverse=True), req.duration_seconds, "tcp_download")
        return {**extract_tcp_result(data), "label": "TCP DOWNLOAD"}

    async def run_tcp_bidirectional(self, req: SpeedTestStartRequest, server_ip: str, bind_ip: str | None) -> dict[str, Any]:
        data = await self._run_iperf(self._iperf_args(req, server_ip, bind_ip, bidirectional=True), req.duration_seconds, "tcp_bidir")
        return extract_bidir_result(data)

    async def run_udp_test(self, req: SpeedTestStartRequest, server_ip: str, bind_ip: str | None) -> dict[str, Any]:
        args = self._iperf_args(req, server_ip, bind_ip, udp=True, udp_bandwidth_mbps=req.udp_bandwidth_mbps)
        data = await self._run_iperf(args, req.duration_seconds, "udp")
        return extract_udp_result(data, target_mbps=req.udp_bandwidth_mbps)

    def calculate_summary(self, result: dict[str, Any]) -> dict[str, Any]:
        return calculate_summary(
            result,
            float(getattr(self.config, "speedtest_efficiency_normal_percent", 85.0)),
            float(getattr(self.config, "speedtest_efficiency_warning_percent", 60.0)),
        )

    # ---------------- state / progress ----------------
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _progress_payload(self) -> dict[str, Any]:
        now = perf_counter()
        active = self._state in ACTIVE_STATES
        phase_elapsed = max(0.0, now - self._phase_started) if active else 0.0
        if self._state == SpeedTestState.COMPLETED:
            phase_progress = overall = 100.0
        elif active:
            phase_progress = min(99.0, phase_elapsed / self._phase_expected * 100.0) if self._phase_expected else 0.0
            total_expected = sum(seconds for _, seconds in self._plan) or 1.0
            overall = min(99.0, (self._completed_seconds + min(phase_elapsed, self._phase_expected)) / total_expected * 100.0)
        else:
            phase_progress = overall = 0.0
        return {
            "type": "speedtest_progress",
            "status": self._state.value,
            "running": active,
            "message": self._message,
            "progress_percent": round(phase_progress, 1),
            "overall_progress_percent": round(overall, 1),
            "elapsed_seconds": round(phase_elapsed, 1),
            "total_elapsed_seconds": round((self._test_finished or now) - self._test_started, 1) if self._test_started else 0.0,
            "phases": [state.value for state, _ in self._plan],
            "error": self._error,
        }

    def get_test_status(self) -> dict[str, Any]:
        payload = self._progress_payload()
        payload.pop("type", None)
        payload["request"] = self._request.model_dump() if self._request else None
        payload["last_result_id"] = (self._last_result or {}).get("id")
        return payload

    async def _emit(self, payload: dict[str, Any]) -> None:
        if not self._broadcast:
            return
        try:
            await self._broadcast(payload)
        except Exception:  # noqa: BLE001 - broadcasting must never break a test
            logger.debug("speedtest broadcast failed", exc_info=True)

    async def _enter_phase(self, state: SpeedTestState, message: str) -> None:
        if self._state in ACTIVE_STATES:
            self._completed_seconds += self._phase_expected
        self._state = state
        self._message = message
        self._phase_started = perf_counter()
        self._phase_expected = next((seconds for phase, seconds in self._plan if phase == state), 1.0)
        await self._emit(self._progress_payload())

    async def _progress_loop(self) -> None:
        while self._state in ACTIVE_STATES:
            await asyncio.sleep(0.5)
            if self._state in ACTIVE_STATES:
                await self._emit(self._progress_payload())

    # ---------------- lifecycle ----------------
    async def start_test(self, req: SpeedTestStartRequest) -> dict[str, Any]:
        async with self._lock:
            if self.is_running:
                raise SpeedTestBusyError("SPEED TEST ALREADY RUNNING")
            self._cancel_event = asyncio.Event()
            self._request = req
            self._error = None
            self._completed_seconds = 0.0
            self._test_started = perf_counter()
            self._test_finished = 0.0
            self._plan = self._build_plan(req)
            self._state = SpeedTestState.IDLE
            await self._enter_phase(SpeedTestState.VALIDATING, "VALIDATING")
            self._task = asyncio.create_task(self._run(req))
        return self.get_test_status()

    @staticmethod
    def _build_plan(req: SpeedTestStartRequest) -> list[tuple[SpeedTestState, float]]:
        duration = float(req.duration_seconds)
        latency_seconds = float(req.duration_seconds if req.mode == "quick" else LATENCY_PHASE_SECONDS)
        plan: list[tuple[SpeedTestState, float]] = [(SpeedTestState.VALIDATING, 2.0), (SpeedTestState.LATENCY_TEST, latency_seconds)]
        if req.mode in ("tcp", "full"):
            plan += [(SpeedTestState.TCP_UPLOAD, duration + 1), (SpeedTestState.TCP_DOWNLOAD, duration + 1)]
            if req.bidirectional:
                plan.append((SpeedTestState.TCP_BIDIRECTIONAL, duration + 1))
        if req.mode in ("udp", "full"):
            plan.append((SpeedTestState.UDP_TEST, duration + 1))
        return plan

    async def _interface_info(self, name: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.monitor.get_interface_info, name)

    async def _check_interface_up(self, name: str) -> None:
        info = await self._interface_info(name)
        if info is None or info.get("status") != "UP":
            raise SpeedTestError("INTERFACE_DISCONNECTED", f"Interface '{name}' disconnected during the test.")

    async def _run(self, req: SpeedTestStartRequest) -> None:
        result: dict[str, Any] = {
            "id": None,
            "timestamp": _now_iso(),
            "completed_at": None,
            "mode": req.mode,
            "interface": req.interface,
            "server": req.server,
            "server_ip": None,
            "port": req.port,
            "duration_seconds": req.duration_seconds,
            "parallel_streams": req.parallel_streams,
            "udp_bandwidth_mbps": req.udp_bandwidth_mbps if req.mode in ("udp", "full") else None,
            "bidirectional": req.bidirectional,
            "status": "RUNNING",
            "error": None,
            "warnings": [],
            "iperf3_version": None,
            "context_before": None,
            "context_after": None,
            "latency": None,
            "tcp_upload": None,
            "tcp_download": None,
            "tcp_bidirectional": None,
            "udp": None,
            "summary": None,
            "comparison": None,
        }
        progress_task = asyncio.create_task(self._progress_loop())
        final_state = SpeedTestState.FAILED
        try:
            # ---- 1. validation ----
            info = await self._interface_info(req.interface)
            if info is None:
                raise SpeedTestError("INTERFACE_NOT_FOUND", f"Interface '{req.interface}' was not found.")
            if info.get("status") != "UP":
                raise SpeedTestError("INTERFACE_DOWN", f"Interface '{req.interface}' is not UP.")
            bind_ip = info.get("ipv4_address") or None
            if not bind_ip:
                raise SpeedTestError("INTERFACE_NO_IPV4", f"Interface '{req.interface}' has no IPv4 address.")
            result["context_before"] = await capture_context(self.monitor, req.interface, self._wifi_query)

            needs_iperf = req.mode != "quick"
            if needs_iperf:
                caps = await self.check_iperf(force=True)
                if not caps["iperf3_installed"]:
                    raise SpeedTestError("IPERF3_NOT_INSTALLED", "IPERF3 NOT INSTALLED. " + INSTALL_HINT)
                if req.bidirectional and not caps["bidirectional_supported"]:
                    raise SpeedTestError("BIDIR_UNSUPPORTED", "The installed iPerf3 version does not support --bidir (requires 3.7+).")
                result["iperf3_version"] = caps["iperf3_version"]
            server_ip = await self.check_server(req.server, req.port, bind_ip, probe_port=needs_iperf)
            result["server_ip"] = server_ip
            source_ip = None if ipaddress.ip_address(server_ip).is_loopback else bind_ip

            # ---- 2. phases ----
            for state, _ in self._plan[1:]:
                if self._cancel_event.is_set():
                    raise _UserCancelled()
                await self._check_interface_up(req.interface)
                if state == SpeedTestState.LATENCY_TEST:
                    await self._enter_phase(state, "LATENCY TEST")
                    seconds = int(next(s for p, s in self._plan if p == state))
                    latency = await self.run_latency_test(server_ip, source_ip, seconds)
                    result["latency"] = latency
                    if latency.get("error"):
                        result["warnings"].append(latency["error"])
                    elif not latency.get("received"):
                        if req.mode == "quick":
                            raise SpeedTestError("SERVER_UNREACHABLE", "No ICMP echo replies from the server (host down or ICMP blocked by firewall).")
                        result["warnings"].append("No ICMP echo replies: ICMP may be blocked by the server firewall; latency/jitter/loss unavailable.")
                elif state == SpeedTestState.TCP_UPLOAD:
                    await self._enter_phase(state, "UPLOAD TEST")
                    result["tcp_upload"] = await self.run_tcp_upload(req, server_ip, source_ip)
                elif state == SpeedTestState.TCP_DOWNLOAD:
                    await self._enter_phase(state, "DOWNLOAD TEST")
                    result["tcp_download"] = await self.run_tcp_download(req, server_ip, source_ip)
                elif state == SpeedTestState.TCP_BIDIRECTIONAL:
                    await self._enter_phase(state, "BIDIRECTIONAL TEST")
                    result["tcp_bidirectional"] = await self.run_tcp_bidirectional(req, server_ip, source_ip)
                elif state == SpeedTestState.UDP_TEST:
                    await self._enter_phase(state, "UDP TEST")
                    result["udp"] = await self.run_udp_test(req, server_ip, source_ip)

            if self._cancel_event.is_set():
                raise _UserCancelled()
            final_state = SpeedTestState.COMPLETED
        except _UserCancelled:
            final_state = SpeedTestState.CANCELLED
            result["error"] = {"code": "CANCELLED", "message": "Test cancelled by user."}
        except asyncio.CancelledError:
            final_state = SpeedTestState.CANCELLED
            result["error"] = {"code": "CANCELLED", "message": "Test cancelled."}
        except SpeedTestError as exc:
            info = await self._interface_info(req.interface)
            if exc.code not in ("INTERFACE_NOT_FOUND", "INTERFACE_DOWN") and (info is None or info.get("status") != "UP"):
                exc = SpeedTestError("INTERFACE_DISCONNECTED", f"Interface '{req.interface}' disconnected during the test.")
            result["error"] = {"code": exc.code, "message": exc.message}
        except Exception:  # noqa: BLE001 - never leak stack traces to the browser
            logger.exception("Unexpected speed test failure")
            result["error"] = {"code": "INTERNAL_ERROR", "message": "Unexpected internal error during the speed test."}
        finally:
            if self._process is not None:
                await stop_process(self._process)
                self._process = None
            await self._finalize(result, final_state, req)
            progress_task.cancel()

    async def _finalize(self, result: dict[str, Any], final_state: SpeedTestState, req: SpeedTestStartRequest) -> None:
        try:
            if result.get("context_before"):
                result["context_after"] = await capture_context(self.monitor, req.interface, self._wifi_query)
                self._add_context_warnings(result)
        except Exception:  # noqa: BLE001
            logger.debug("context_after capture failed", exc_info=True)
        result["status"] = final_state.value
        result["completed_at"] = _now_iso()
        result["summary"] = self.calculate_summary(result)
        try:
            result["id"] = await asyncio.to_thread(self.store.save, result)
            if final_state == SpeedTestState.COMPLETED:
                previous = await asyncio.to_thread(self.store.previous_completed, req.interface, result["id"])
                result["comparison"] = compare_results(SpeedTestStore._row_from_result(result), previous)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist speed test result")
            result["warnings"].append("Result could not be saved to history.")

        self._last_result = result
        self._error = result["error"]
        self._test_finished = perf_counter()
        self._state = final_state
        self._message = {
            SpeedTestState.COMPLETED: "TEST COMPLETED",
            SpeedTestState.CANCELLED: "TEST CANCELLED",
        }.get(final_state, (result["error"] or {}).get("message", "TEST FAILED"))
        await self._emit(self._progress_payload())
        await self._emit({"type": "speedtest_complete", "result": result})

    @staticmethod
    def _add_context_warnings(result: dict[str, Any]) -> None:
        before = result.get("context_before") or {}
        after = result.get("context_after") or {}
        if after.get("status") != "UP":
            result["warnings"].append("Interface was not UP after the test.")
        wifi_before, wifi_after = before.get("wifi") or {}, after.get("wifi") or {}
        if wifi_before.get("bssid") and wifi_after.get("bssid") and wifi_before["bssid"] != wifi_after["bssid"]:
            result["warnings"].append("Wi-Fi BSSID changed during the test (roaming).")
        for key in ("rx_errors", "tx_errors", "rx_drops", "tx_drops"):
            b, a = before.get(key), after.get(key)
            if isinstance(a, int) and isinstance(b, int) and a > b:
                result["warnings"].append(f"{key.replace('_', ' ').upper()} increased by {a - b} during the test.")

    async def cancel_test(self) -> dict[str, Any]:
        if not self.is_running:
            return self.get_test_status()
        self._cancel_event.set()
        self._message = "CANCELLING"
        if self._process is not None:
            await stop_process(self._process)
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=6)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        return self.get_test_status()

    def get_last_result(self) -> dict[str, Any] | None:
        if self._last_result is not None:
            return self._last_result
        latest = self.store.latest()
        if latest and latest.get("details"):
            return latest["details"]
        return None

    async def shutdown(self) -> None:
        if self.is_running:
            await self.cancel_test()


class LocalIperfServer:
    """Optional local iperf3 server (-s). Never started automatically."""

    def __init__(self) -> None:
        self._process: Any = None
        self._bind_address: str | None = None
        self._port: int | None = None
        self._started_at: str | None = None
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def status(self) -> dict[str, Any]:
        running = self.running
        if not running and self._process is not None:
            self._last_error = self._last_error or "iPerf3 server process exited."
            self._process = None
        return {
            "running": running,
            "status": "RUNNING" if running else "STOPPED",
            "listening_address": self._bind_address if running else None,
            "port": self._port if running else None,
            "started_at": self._started_at if running else None,
            "last_error": None if running else self._last_error,
        }

    async def start(self, executable: str, bind_address: str, port: int) -> dict[str, Any]:
        bind_address = validate_ipv4_bind_address(bind_address)
        if not 1 <= int(port) <= 65535:
            raise SpeedTestError("INVALID_PORT", "Invalid port.")
        async with self._lock:
            if self.running:
                raise SpeedTestError("SERVER_ALREADY_RUNNING", "Local iPerf3 server is already running.")
            args = [executable, "-s", "-B", bind_address, "-p", str(int(port))]
            logger.info("Starting local iperf3 server port=%s", port)
            # Server output is discarded so a long-running process can never block on a full pipe.
            self._process = await spawn(args, capture_output=False)
            await asyncio.sleep(0.8)
            if self._process.returncode is not None:
                self._process = None
                self._last_error = "iPerf3 server exited immediately (port in use or bind address unavailable)."
                raise SpeedTestError("SERVER_START_FAILED", self._last_error)
            self._bind_address, self._port, self._started_at, self._last_error = bind_address, int(port), _now_iso(), None
        return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            if self._process is not None:
                await stop_process(self._process)
            self._process = None
            self._last_error = None
        return self.status()
