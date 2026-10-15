from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import CONFIG_PATH, AppConfig, load_config, save_config
from .models import (
    InterfaceSelectionRequest,
    LocalServerStartRequest,
    SpeedTestConfigRequest,
    SpeedTestStartRequest,
)
from .monitor import NetworkMonitor
from .network_context import list_interface_contexts
from .paths import resource_root
from .speedtest import LocalIperfServer, SpeedTestBusyError, SpeedTestError, SpeedTestService
from .speedtest_store import SpeedTestStore

BASE_DIR = resource_root()
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Ethernet Network Monitoring Dashboard", version="1.0.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections)
        stale: list[WebSocket] = []
        for connection in connections:
            try:
                await connection.send_json(payload)
            except Exception:
                stale.append(connection)
        if stale:
            async with self._lock:
                for connection in stale:
                    self._connections.discard(connection)


@app.on_event("startup")
async def startup_event() -> None:
    config = load_config()
    app.state.config = config
    app.state.monitor = NetworkMonitor(config)
    app.state.connections = ConnectionManager()
    app.state.speedtest = SpeedTestService(
        app.state.monitor,
        config,
        SpeedTestStore(),
        broadcast=app.state.connections.broadcast,
    )
    app.state.iperf_server = LocalIperfServer()
    app.state.shutdown_event = asyncio.Event()
    app.state.monitor_task = asyncio.create_task(_monitor_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    app.state.shutdown_event.set()
    await app.state.speedtest.shutdown()
    await app.state.iperf_server.stop()
    monitor_task = getattr(app.state, "monitor_task", None)
    if monitor_task:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task


async def _monitor_loop() -> None:
    await asyncio.sleep(0.1)
    while not app.state.shutdown_event.is_set():
        snapshot = app.state.monitor.update_statistics()
        await app.state.connections.broadcast(snapshot)
        try:
            await asyncio.wait_for(app.state.shutdown_event.wait(), timeout=app.state.monitor.refresh_interval_seconds)
        except asyncio.TimeoutError:
            continue


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/interfaces")
async def get_interfaces() -> JSONResponse:
    return JSONResponse(app.state.monitor.discover_interfaces())


@app.get("/api/interface/{name}")
async def get_interface(name: str) -> JSONResponse:
    info = app.state.monitor.get_interface_info(name)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Interface '{name}' was not found.")
    return JSONResponse(info)


@app.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse(app.state.monitor.get_status(include_history=True))


@app.post("/api/interface")
async def set_interface(request: InterfaceSelectionRequest) -> JSONResponse:
    try:
        snapshot = app.state.monitor.select_interface(request.interface)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    config: AppConfig = app.state.config
    config.interface = request.interface
    save_config(config, CONFIG_PATH)
    return JSONResponse(snapshot)


@app.post("/api/reset-statistics")
async def reset_statistics() -> JSONResponse:
    snapshot = app.state.monitor.reset_statistics()
    return JSONResponse(snapshot)


@app.websocket("/ws/network")
async def network_websocket(websocket: WebSocket) -> None:
    await app.state.connections.connect(websocket)
    try:
        await websocket.send_json(app.state.monitor.get_status(include_history=True))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await app.state.connections.disconnect(websocket)


# ------------------------------------------------------------------------------------------
# Speed test (active test: generates traffic, only on explicit user request)
# ------------------------------------------------------------------------------------------

@app.get("/api/speedtest/capabilities")
async def speedtest_capabilities() -> JSONResponse:
    service: SpeedTestService = app.state.speedtest
    caps = await service.check_iperf(force=True)
    return JSONResponse(service.public_capabilities(caps))


@app.get("/api/speedtest/interfaces")
async def speedtest_interfaces() -> JSONResponse:
    return JSONResponse(await list_interface_contexts(app.state.monitor))


@app.get("/api/speedtest/config")
async def speedtest_get_config() -> JSONResponse:
    config: AppConfig = app.state.config
    return JSONResponse(
        {
            "server": config.speedtest_server,
            "port": config.speedtest_port,
            "efficiency_normal_percent": config.speedtest_efficiency_normal_percent,
            "efficiency_warning_percent": config.speedtest_efficiency_warning_percent,
        }
    )


@app.post("/api/speedtest/config")
async def speedtest_save_config(request: SpeedTestConfigRequest) -> JSONResponse:
    config: AppConfig = app.state.config
    config.speedtest_server = request.server
    config.speedtest_port = request.port
    save_config(config, CONFIG_PATH)
    return JSONResponse({"server": config.speedtest_server, "port": config.speedtest_port, "saved": True})


@app.get("/api/speedtest/status")
async def speedtest_status() -> JSONResponse:
    return JSONResponse(app.state.speedtest.get_test_status())


@app.post("/api/speedtest/start")
async def speedtest_start(request: SpeedTestStartRequest) -> JSONResponse:
    try:
        status = await app.state.speedtest.start_test(request)
    except SpeedTestBusyError as exc:
        raise HTTPException(status_code=409, detail="SPEED TEST ALREADY RUNNING") from exc
    return JSONResponse(status, status_code=202)


@app.post("/api/speedtest/cancel")
async def speedtest_cancel() -> JSONResponse:
    return JSONResponse(await app.state.speedtest.cancel_test())


@app.get("/api/speedtest/results")
async def speedtest_results() -> JSONResponse:
    return JSONResponse({"result": app.state.speedtest.get_last_result()})


@app.get("/api/speedtest/history")
async def speedtest_history(
    limit: int = Query(default=50, ge=1, le=1000),
    hours: float | None = Query(default=None, gt=0, le=24 * 365),
) -> JSONResponse:
    items = await asyncio.to_thread(app.state.speedtest.store.list, limit, hours)
    return JSONResponse({"items": items})


@app.get("/api/speedtest/server")
async def iperf_server_status() -> JSONResponse:
    return JSONResponse(app.state.iperf_server.status())


@app.post("/api/speedtest/server/start")
async def iperf_server_start(request: LocalServerStartRequest) -> JSONResponse:
    local_addresses = {item["ipv4_address"] for item in app.state.monitor.discover_interfaces() if item.get("ipv4_address")}
    if request.bind_address != "0.0.0.0" and request.bind_address not in local_addresses:
        raise HTTPException(status_code=400, detail="Bind address must be 0.0.0.0 or a local interface IPv4 address.")
    caps = await app.state.speedtest.check_iperf(force=True)
    if not caps["iperf3_installed"]:
        raise HTTPException(status_code=400, detail="IPERF3 NOT INSTALLED")
    try:
        status = await app.state.iperf_server.start(caps["executable"], request.bind_address, request.port)
    except SpeedTestError as exc:
        raise HTTPException(status_code=409 if exc.code == "SERVER_ALREADY_RUNNING" else 400, detail=exc.message) from exc
    return JSONResponse(status)


@app.post("/api/speedtest/server/stop")
async def iperf_server_stop() -> JSONResponse:
    return JSONResponse(await app.state.iperf_server.stop())
