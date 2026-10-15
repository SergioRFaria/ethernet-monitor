"""Windows standalone entry point used by PyInstaller."""
from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from app.main import app


HOST = "127.0.0.1"
PORT = int(os.environ.get("ETHERNET_MONITOR_PORT", "8000"))
if not 1 <= PORT <= 65535:
    raise ValueError("ETHERNET_MONITOR_PORT must be between 1 and 65535.")


def _open_dashboard() -> None:
    webbrowser.open_new_tab(f"http://{HOST}:{PORT}/")


def main() -> None:
    threading.Timer(1.5, _open_dashboard).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
