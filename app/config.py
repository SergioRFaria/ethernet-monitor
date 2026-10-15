from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .paths import config_path

CONFIG_PATH = config_path()


@dataclass(slots=True)
class AppConfig:
    interface: str = "Ethernet"
    refresh_interval_ms: int = 1000
    history_seconds: int = 300
    speedtest_server: str = ""
    speedtest_port: int = 5201
    speedtest_efficiency_normal_percent: float = 85.0
    speedtest_efficiency_warning_percent: float = 60.0
    # Optional absolute path to iperf3.exe; only read from config.json, never from the browser.
    iperf3_path: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "AppConfig":
        payload = data or {}
        # slots=True replaces class-level defaults with descriptors, so read defaults from an instance.
        defaults = cls()
        interface = str(payload.get("interface", defaults.interface))
        try:
            refresh_interval_ms = int(payload.get("refresh_interval_ms", defaults.refresh_interval_ms))
        except (TypeError, ValueError):
            refresh_interval_ms = defaults.refresh_interval_ms
        try:
            history_seconds = int(payload.get("history_seconds", defaults.history_seconds))
        except (TypeError, ValueError):
            history_seconds = defaults.history_seconds
        try:
            speedtest_port = int(payload.get("speedtest_port", defaults.speedtest_port))
        except (TypeError, ValueError):
            speedtest_port = defaults.speedtest_port
        if not 1 <= speedtest_port <= 65535:
            speedtest_port = defaults.speedtest_port
        try:
            normal = float(payload.get("speedtest_efficiency_normal_percent", defaults.speedtest_efficiency_normal_percent))
            warning = float(payload.get("speedtest_efficiency_warning_percent", defaults.speedtest_efficiency_warning_percent))
        except (TypeError, ValueError):
            normal, warning = defaults.speedtest_efficiency_normal_percent, defaults.speedtest_efficiency_warning_percent
        if not 0 < warning <= normal <= 100:
            normal, warning = defaults.speedtest_efficiency_normal_percent, defaults.speedtest_efficiency_warning_percent
        speedtest_server = str(payload.get("speedtest_server", "") or "").strip()[:253]

        return cls(
            interface=interface.strip() or defaults.interface,
            refresh_interval_ms=max(250, refresh_interval_ms),
            history_seconds=max(30, history_seconds),
            speedtest_server=speedtest_server,
            speedtest_port=speedtest_port,
            speedtest_efficiency_normal_percent=normal,
            speedtest_efficiency_warning_percent=warning,
            iperf3_path=str(payload.get("iperf3_path", "") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        return AppConfig()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()

    if not isinstance(payload, dict):
        return AppConfig()

    return AppConfig.from_mapping(payload)


def save_config(config: AppConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
