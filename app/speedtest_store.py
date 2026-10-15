"""SQLite persistence for speed-test results."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import speedtest_database_path

DEFAULT_DB_PATH = speedtest_database_path()

_COLUMNS: list[tuple[str, str]] = [
    ("timestamp", "TEXT NOT NULL"),
    ("interface", "TEXT"),
    ("interface_type", "TEXT"),
    ("server", "TEXT"),
    ("port", "INTEGER"),
    ("mode", "TEXT"),
    ("tcp_download_mbps", "REAL"),
    ("tcp_upload_mbps", "REAL"),
    ("tcp_download_peak_mbps", "REAL"),
    ("tcp_upload_peak_mbps", "REAL"),
    ("tcp_retransmits", "INTEGER"),
    ("udp_mbps", "REAL"),
    ("udp_jitter_ms", "REAL"),
    ("udp_lost_datagrams", "INTEGER"),
    ("udp_total_datagrams", "INTEGER"),
    ("udp_packet_loss_percent", "REAL"),
    ("latency_ms", "REAL"),
    ("jitter_ms", "REAL"),
    ("packet_loss_percent", "REAL"),
    ("link_speed_mbps", "INTEGER"),
    ("wifi_ssid", "TEXT"),
    ("wifi_bssid", "TEXT"),
    ("wifi_rssi", "INTEGER"),
    ("wifi_channel", "INTEGER"),
    ("wifi_band", "TEXT"),
    ("wifi_phy_rx_mbps", "REAL"),
    ("wifi_phy_tx_mbps", "REAL"),
    ("duration_seconds", "INTEGER"),
    ("parallel_streams", "INTEGER"),
    ("status", "TEXT"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("details_json", "TEXT"),
]


class SpeedTestStore:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            columns = ", ".join(f"{name} {kind}" for name, kind in _COLUMNS)
            conn.execute(f"CREATE TABLE IF NOT EXISTS speed_test_results (id INTEGER PRIMARY KEY AUTOINCREMENT, {columns})")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_speedtest_timestamp ON speed_test_results(timestamp)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    @staticmethod
    def _row_from_result(result: dict[str, Any]) -> dict[str, Any]:
        context = result.get("context_before") or {}
        wifi = context.get("wifi") or {}
        tcp_up = result.get("tcp_upload") or {}
        tcp_down = result.get("tcp_download") or {}
        udp = result.get("udp") or {}
        latency = result.get("latency") or {}
        error = result.get("error") or {}
        return {
            "timestamp": result.get("timestamp"),
            "interface": result.get("interface"),
            "interface_type": context.get("interface_type"),
            "server": result.get("server"),
            "port": result.get("port"),
            "mode": result.get("mode"),
            "tcp_download_mbps": tcp_down.get("throughput_mbps"),
            "tcp_upload_mbps": tcp_up.get("throughput_mbps"),
            "tcp_download_peak_mbps": tcp_down.get("peak_mbps"),
            "tcp_upload_peak_mbps": tcp_up.get("peak_mbps"),
            "tcp_retransmits": tcp_up.get("retransmits"),
            "udp_mbps": udp.get("throughput_mbps"),
            "udp_jitter_ms": udp.get("jitter_ms"),
            "udp_lost_datagrams": udp.get("lost_datagrams"),
            "udp_total_datagrams": udp.get("total_datagrams"),
            "udp_packet_loss_percent": udp.get("packet_loss_percent"),
            "latency_ms": latency.get("latency_avg_ms"),
            "jitter_ms": latency.get("jitter_ms"),
            "packet_loss_percent": latency.get("packet_loss_percent") if latency.get("received") else None,
            "link_speed_mbps": context.get("link_speed_mbps"),
            "wifi_ssid": wifi.get("ssid"),
            "wifi_bssid": wifi.get("bssid"),
            "wifi_rssi": wifi.get("rssi_dbm"),
            "wifi_channel": wifi.get("channel"),
            "wifi_band": wifi.get("band"),
            "wifi_phy_rx_mbps": wifi.get("phy_rx_mbps"),
            "wifi_phy_tx_mbps": wifi.get("phy_tx_mbps"),
            "duration_seconds": result.get("duration_seconds"),
            "parallel_streams": result.get("parallel_streams"),
            "status": result.get("status"),
            "error_code": error.get("code"),
            "error_message": error.get("message"),
            "details_json": json.dumps(result, default=str),
        }

    def save(self, result: dict[str, Any]) -> int:
        row = self._row_from_result(result)
        names = list(row)
        placeholders = ", ".join("?" for _ in names)
        with self._session() as conn:
            cursor = conn.execute(
                f"INSERT INTO speed_test_results ({', '.join(names)}) VALUES ({placeholders})",
                [row[name] for name in names],
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _to_dict(row: sqlite3.Row, include_details: bool = False) -> dict[str, Any]:
        data = dict(row)
        details = data.pop("details_json", None)
        if include_details and details:
            try:
                data["details"] = json.loads(details)
            except json.JSONDecodeError:
                data["details"] = None
        return data

    def list(self, limit: int = 50, since_hours: float | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        query = "SELECT * FROM speed_test_results"
        params: list[Any] = []
        if since_hours is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=float(since_hours))
            query += " WHERE timestamp >= ?"
            params.append(since.isoformat(timespec="seconds").replace("+00:00", "Z"))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._session() as conn:
            return [self._to_dict(row) for row in conn.execute(query, params).fetchall()]

    def latest(self) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM speed_test_results ORDER BY id DESC LIMIT 1").fetchone()
        return self._to_dict(row, include_details=True) if row else None

    def previous_completed(self, interface: str, before_id: int | None) -> dict[str, Any] | None:
        query = "SELECT * FROM speed_test_results WHERE status = 'COMPLETED' AND interface = ?"
        params: list[Any] = [interface]
        if before_id is not None:
            query += " AND id < ?"
            params.append(before_id)
        query += " ORDER BY id DESC LIMIT 1"
        with self._session() as conn:
            row = conn.execute(query, params).fetchone()
        return self._to_dict(row) if row else None
