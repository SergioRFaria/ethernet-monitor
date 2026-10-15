@echo off
setlocal
cd /d "%~dp0"
echo Starting Ethernet Network Monitoring Dashboard...
echo Open http://127.0.0.1:8000 in your browser.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
