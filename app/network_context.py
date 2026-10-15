"""Interface classification and Ethernet / Wi-Fi context snapshots for speed tests."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .process_utils import run_capture

logger = logging.getLogger("ethernet_monitor.network_context")

WIFI_PERMISSION_HINT = (
    "Wi-Fi details unavailable: Windows did not return WLAN data. On Windows 11 enable "
    "Settings > Privacy & security > Location (and 'Let desktop apps access your location')."
)

# Keys are matched case-insensitively against substrings to tolerate localized netsh output.
_WIFI_KEY_MAP: list[tuple[tuple[str, ...], str]] = [
    (("ap bssid", "bssid"), "bssid"),
    (("ssid",), "ssid"),
    (("receive rate", "empfangsrate", "taxa de rece", "velocidad de recep", "débit de réception"), "phy_rx_mbps"),
    (("transmit rate", "bertragungsrate", "taxa de trans", "velocidad de trans", "débit de transmission"), "phy_tx_mbps"),
    (("rssi",), "rssi_dbm"),
    (("signal", "sinal", "señal"), "signal_percent"),
    (("channel", "kanal", "canal"), "channel"),
    (("band", "banda", "bande"), "band"),
    (("radio type", "funktyp", "tipo de r", "type de radio"), "radio_type"),
    (("description", "beschreibung", "descri"), "description"),
    (("state", "status", "estado", "état"), "state"),
    (("profile", "profil", "perfil"), "profile"),
    (("name", "nome", "nombre", "nom"), "name"),
]

_LINE_RE = re.compile(r"^\s*([^:]+?)\s*:\s(.*)$")


def _decode(raw: bytes) -> str:
    for encoding in ("oem", "utf-8") if sys.platform == "win32" else ("utf-8",):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _to_float(value: str) -> float | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", value or "")
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _match_key(key: str) -> str | None:
    lowered = key.strip().lower()
    for needles, field in _WIFI_KEY_MAP:
        for needle in needles:
            if field == "ssid" and lowered != "ssid":
                break
            if field == "name" and lowered != needle:
                continue
            if needle in lowered:
                return field
    return None


def parse_netsh_wlan_interfaces(text: str) -> dict[str, dict[str, Any]]:
    """Parse `netsh wlan show interfaces` output into {interface_name: details}."""
    interfaces: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        field = _match_key(key)
        if field is None:
            continue
        if field == "name":
            current = {"name": value}
            interfaces[value] = current
            continue
        if current is None or current.get(field) is not None:
            # First occurrence wins (e.g. trailing "Hosted network status" must not override "State").
            continue
        if field in ("phy_rx_mbps", "phy_tx_mbps"):
            current[field] = _to_float(value)
        elif field in ("signal_percent", "rssi_dbm", "channel"):
            number = _to_float(value)
            current[field] = int(number) if number is not None else None
        else:
            current[field] = value

    for details in interfaces.values():
        if details.get("rssi_dbm") is None and details.get("signal_percent") is not None:
            # Common Windows approximation: quality% = 2 * (dBm + 100).
            details["rssi_dbm"] = int(details["signal_percent"] / 2 - 100)
            details["rssi_estimated"] = True
        else:
            details.setdefault("rssi_estimated", False)
    return interfaces


def _netsh_path() -> str | None:
    if sys.platform != "win32":
        return None
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "netsh.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("netsh")


async def query_wifi_interfaces() -> dict[str, Any]:
    """Return {"available": bool, "reason": str|None, "interfaces": {...}}. Never raises."""
    netsh = _netsh_path()
    if not netsh:
        return {"available": False, "reason": "Wi-Fi details are only available on Windows (netsh).", "interfaces": {}}
    try:
        _, stdout, _ = await run_capture([netsh, "wlan", "show", "interfaces"], timeout_seconds=8)
    except (asyncio.TimeoutError, OSError) as exc:
        logger.warning("netsh wlan query failed: %s", type(exc).__name__)
        return {"available": False, "reason": "Wi-Fi query timed out or failed.", "interfaces": {}}
    interfaces = parse_netsh_wlan_interfaces(_decode(stdout))
    has_details = any(item.get("ssid") or item.get("phy_rx_mbps") for item in interfaces.values())
    if not interfaces or not has_details:
        return {"available": False, "reason": WIFI_PERMISSION_HINT, "interfaces": interfaces}
    return {"available": True, "reason": None, "interfaces": interfaces}


def classify_interface(name: str, wifi_names: set[str] | None = None) -> str:
    if wifi_names and name in wifi_names:
        return "wifi"
    lowered = name.lower()
    if "loopback" in lowered:
        return "loopback"
    if any(token in lowered for token in ("wi-fi", "wifi", "wlan", "wireless")):
        return "wifi"
    if "bluetooth" in lowered:
        return "bluetooth"
    if "ethernet" in lowered or lowered.startswith(("eth", "en", "lan")):
        return "ethernet"
    return "unknown"


def _duplex_label(stat: Any) -> str:
    duplex = getattr(stat, "duplex", None)
    if duplex == getattr(psutil, "NIC_DUPLEX_FULL", object()):
        return "FULL"
    if duplex == getattr(psutil, "NIC_DUPLEX_HALF", object()):
        return "HALF"
    return "UNKNOWN"


async def capture_context(monitor: Any, interface_name: str, wifi_query: Any = None) -> dict[str, Any]:
    """Snapshot of interface state; Wi-Fi details added when the interface is wireless."""

    def _read_local() -> tuple[dict[str, Any], Any, Any]:
        return (
            monitor.get_interface_info(interface_name) or {},
            psutil.net_if_stats().get(interface_name),
            monitor.read_counters(interface_name),
        )

    # psutil adapter queries can take ~100 ms on Windows; keep them off the event loop.
    info, stat, counters = await asyncio.to_thread(_read_local)
    wifi_result = await (wifi_query or query_wifi_interfaces)()
    return _build_context(interface_name, info, stat, counters, wifi_result)


def _build_context(interface_name: str, info: dict[str, Any], stat: Any, counters: Any, wifi_result: dict[str, Any]) -> dict[str, Any]:
    wifi_interfaces = wifi_result.get("interfaces", {})
    interface_type = classify_interface(interface_name, set(wifi_interfaces))

    context: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "interface": interface_name,
        "interface_type": interface_type,
        "present": bool(info),
        "status": info.get("status", "MISSING"),
        "ipv4_address": info.get("ipv4_address", ""),
        "mac_address": info.get("mac_address", ""),
        "link_speed_mbps": info.get("link_speed_mbps", 0),
        "link_speed_label": info.get("link_speed_label", "Unknown"),
        "duplex": _duplex_label(stat) if stat else "UNKNOWN",
        "mtu": getattr(stat, "mtu", 0) if stat else 0,
        "rx_errors": counters.errin if counters else None,
        "tx_errors": counters.errout if counters else None,
        "rx_drops": counters.dropin if counters else None,
        "tx_drops": counters.dropout if counters else None,
        "wifi": None,
    }
    if interface_type == "wifi":
        details = wifi_interfaces.get(interface_name) or {}
        context["wifi"] = {
            "details_available": bool(details.get("ssid") or details.get("phy_rx_mbps")),
            "reason": None if details else wifi_result.get("reason"),
            "ssid": details.get("ssid"),
            "bssid": details.get("bssid"),
            "state": details.get("state"),
            "radio_type": details.get("radio_type"),
            "band": details.get("band"),
            "channel": details.get("channel"),
            "signal_percent": details.get("signal_percent"),
            "rssi_dbm": details.get("rssi_dbm"),
            "rssi_estimated": details.get("rssi_estimated", False),
            "phy_rx_mbps": details.get("phy_rx_mbps"),
            "phy_tx_mbps": details.get("phy_tx_mbps"),
        }
        if not context["wifi"]["details_available"]:
            context["wifi"]["reason"] = wifi_result.get("reason") or WIFI_PERMISSION_HINT
    return context


async def list_interface_contexts(monitor: Any) -> dict[str, Any]:
    wifi_result = await query_wifi_interfaces()

    def _read_all() -> list[tuple[dict[str, Any], Any, Any]]:
        stats = psutil.net_if_stats()
        return [
            (item, stats.get(item["name"]), monitor.read_counters(item["name"]))
            for item in monitor.discover_interfaces()
        ]

    contexts = [
        _build_context(info["name"], info, stat, counters, wifi_result)
        for info, stat, counters in await asyncio.to_thread(_read_all)
    ]
    return {
        "wifi_details_available": wifi_result.get("available", False),
        "wifi_reason": wifi_result.get("reason"),
        "interfaces": contexts,
    }
