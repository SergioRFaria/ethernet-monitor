@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
set "BUILD_MODE=--onedir"
if /i "%~1"=="--onefile" set "BUILD_MODE=--onefile"

%PYTHON% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is missing. Install build dependencies first:
    echo   %PYTHON% -m pip install -r requirements-build.txt
    exit /b 1
)

set "IPERF_ARGS="
if exist "vendor\iperf3\iperf3.exe" (
    set IPERF_ARGS=--add-binary "vendor\iperf3\iperf3.exe;vendor\iperf3"
    echo Bundling vendor\iperf3\iperf3.exe
) else (
    echo iPerf3 will not be bundled. TCP/UDP tests require iperf3.exe on PATH or in config.json.
)

echo Building portable Windows folder...
%PYTHON% -m PyInstaller --noconfirm --clean %BUILD_MODE% --console --name EthernetMonitor --add-data "templates;templates" --add-data "static;static" --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all websockets %IPERF_ARGS% standalone.py
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
if /i "%BUILD_MODE%"=="--onefile" (
    echo Build complete: dist\EthernetMonitor.exe
) else (
    echo Build complete: dist\EthernetMonitor\EthernetMonitor.exe
)
if /i "%BUILD_MODE%"=="--onedir" echo Copy the entire dist\EthernetMonitor folder to the target PC.
echo User settings/history are stored in %%LOCALAPPDATA%%\EthernetMonitor.
endlocal
