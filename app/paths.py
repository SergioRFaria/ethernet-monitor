"""Resolve bundled read-only assets separately from writable application data."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """Return the directory containing templates/static, including in a PyInstaller bundle."""
    bundled_root = getattr(sys, "_MEIPASS", None)
    return Path(bundled_root) if bundled_root else PROJECT_ROOT


def user_data_root() -> Path:
    """Frozen builds keep user data in LocalAppData; source runs keep it in the project folder."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "EthernetMonitor"
    return PROJECT_ROOT


def config_path() -> Path:
    return user_data_root() / "config.json"


def speedtest_database_path() -> Path:
    return user_data_root() / "data" / "speedtest_history.db"
