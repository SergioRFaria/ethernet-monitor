from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paths import PROJECT_ROOT, config_path, resource_root, speedtest_database_path, user_data_root
from app.speedtest import resolve_iperf_executable


class PackagingPathTests(unittest.TestCase):
    def test_source_run_keeps_project_data_paths(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(user_data_root(), PROJECT_ROOT)
            self.assertEqual(config_path(), PROJECT_ROOT / "config.json")
            self.assertEqual(speedtest_database_path(), PROJECT_ROOT / "data" / "speedtest_history.db")

    def test_frozen_app_uses_local_appdata_for_writable_files(self) -> None:
        with patch.object(sys, "frozen", True, create=True), patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}):
            root = Path(r"C:\Users\Test\AppData\Local") / "EthernetMonitor"
            self.assertEqual(user_data_root(), root)
            self.assertEqual(config_path(), root / "config.json")
            self.assertEqual(speedtest_database_path(), root / "data" / "speedtest_history.db")

    def test_frozen_assets_use_pyinstaller_meipass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "_MEIPASS", tmp, create=True):
            self.assertEqual(resource_root(), Path(tmp))

    def test_bundled_iperf_is_discovered_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundled = Path(tmp) / "vendor" / "iperf3" / "iperf3.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"test executable placeholder")
            with patch("app.speedtest.resource_root", return_value=Path(tmp)), patch("app.speedtest.shutil.which", return_value=None):
                path, source = resolve_iperf_executable()
            self.assertEqual(Path(path), bundled.resolve())
            self.assertEqual(source, "bundled")


if __name__ == "__main__":
    unittest.main()
