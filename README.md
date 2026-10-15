# Ethernet Network Monitoring Dashboard

A local Windows dashboard for monitoring one Ethernet adapter in real time. It reads actual adapter counters from `psutil`, calculates throughput from counter deltas, and streams live telemetry to a web UI with FastAPI and WebSockets.

## Requirements

- Windows 10/11
- Python 3.11 or newer
- An Ethernet adapter visible to Windows
- Internet access only if you keep the Chart.js CDN reference in the HTML template

## Python installation

Install Python 3.11+ and confirm it is available:

```bash
python --version
```

## Virtual environment creation

From the project folder:

```bash
python -m venv .venv
.venv\Scripts\activate
```

## Dependency installation

```bash
pip install -r requirements.txt
```

## Starting the dashboard

Use either Uvicorn directly:

```bash
uvicorn app.main:app --reload
```

Or start the Windows launcher:

```bash
run.bat
```

Open:

```text
http://127.0.0.1:8000
```

## Selecting the Ethernet interface

The dashboard discovers adapters with:

- `GET /api/interfaces`

Use the interface selector in the UI or call:

- `POST /api/interface`

Example body:

```json
{
  "interface": "Ethernet"
}
```

If the configured interface does not exist, the dashboard shows the available interfaces and continues running.

## Configuration

The application reads `config.json` from the project root:

```json
{
  "interface": "Ethernet",
  "refresh_interval_ms": 1000,
  "history_seconds": 300
}
```

- `interface`: selected adapter name
- `refresh_interval_ms`: sampling interval
- `history_seconds`: rolling chart history window

## API overview

- `GET /api/interfaces`
- `GET /api/interface/{name}`
- `GET /api/status`
- `POST /api/interface`
- `POST /api/reset-statistics`
- `WS /ws/network`
- Speed test endpoints: see [Speed Test](#speed-test)

## How network speed is calculated

Speed is calculated from counter deltas, not from cumulative byte totals.

For RX:

$$
\text{RX Mbps} = \frac{(\text{current bytes recv} - \text{previous bytes recv}) \times 8}{\text{elapsed seconds} \times 1{,}000{,}000}
$$

For TX:

$$
\text{TX Mbps} = \frac{(\text{current bytes sent} - \text{previous bytes sent}) \times 8}{\text{elapsed seconds} \times 1{,}000{,}000}
$$

The code uses `time.perf_counter()` to measure elapsed time accurately.

## Troubleshooting

### Interface missing

- Check `GET /api/interfaces`
- Update `config.json` or choose a different adapter in the UI

### Cable disconnected

- Status changes to `DOWN`
- Throughput should drop to zero or near zero

### WebSocket reconnects repeatedly

- Refresh the page
- Check that the backend is still running
- Confirm that the browser can access `ws://127.0.0.1:8000/ws/network`

### Invalid configuration file

- Delete or fix `config.json`
- Restart the app

## Windows / psutil limitations

- Link speed can be reported as `0` or `Unknown` depending on the adapter driver.
- Packet error and drop counters depend on Windows driver support and may remain zero.
- Some adapters report the same name as the interface alias shown in Windows.
- `psutil` exposes cumulative counters, so the app must compute throughput from differences between samples.
- Sleep/resume or adapter resets may cause counter jumps; the app restarts statistics when it detects a reset.

## Testing

Run unit tests:

```bash
python -m unittest discover -s tests -v
```

The included tests validate the throughput math, counter reset handling, and interface discovery.
`tests/test_speedtest.py` covers iPerf3 detection, input validation, command generation, JSON parsing
(TCP/UDP/bidirectional), error mapping, timeouts, cancellation, 409 concurrency, persistence,
efficiency calculation and verifies that no command is executed through a shell.

## Speed Test

The **Speed Test** tab is an **ACTIVE** test. The Graphs and Telemetry tabs are **PASSIVE**: they only
read adapter counters. The speed test intentionally generates traffic and only runs when you press
**START TEST**.

### Why iPerf3

iPerf3 measures throughput between two endpoints you control on your own LAN/WLAN. Internet speed-test
services measure the whole path to a remote data centre (ISP, peering, server load), so they cannot
characterise a local Ethernet or Wi-Fi link. iPerf3 is run with `-J`, and only its JSON output is parsed.

### Client / server architecture

```text
[ Dashboard PC = CLIENT ] --(Ethernet / Wi-Fi under test)--> [ Other machine = SERVER: iperf3 -s ]
```

One machine must act as the **SERVER** (`iperf3 -s`). The dashboard PC acts as the **CLIENT**. Put the
server on a wired port as close as possible to the network segment you want to test, and make sure the
server is faster than the link under test (otherwise you measure the server, not the link).

### Installing iPerf3 (dashboard PC and server)

- Windows: `winget install iperf3`, or download a Windows build from <https://iperf.fr/iperf-download.php>
  and put `iperf3.exe` on `PATH`.
- Alternatively set an absolute path in `config.json` (only files named `iperf3.exe` are accepted):

```json
{ "iperf3_path": "C:\\Tools\\iperf3\\iperf3.exe" }
```

- Linux server: `sudo apt install iperf3`.

`GET /api/speedtest/capabilities` reports whether iPerf3 was found. If it is missing the UI shows
**IPERF3 NOT INSTALLED**; TCP/UDP tests are refused (no fake results). QUICK TEST still works because it
only uses ICMP.

### Starting the server

On the server machine:

```bash
iperf3 -s
```

Optional: `iperf3 -s -p 5201 -B <server-ip>` to bind a port/address. Enter the server address and port
(default `5201`) in the Speed Test tab. The address is stored in `config.json` only when you click
**Save server**.

The dashboard can also run a **local** iPerf3 server ("iPerf server" section) so this PC can be the
target for another client. It is never started automatically and binds only to the address you choose.

### Test modes

| Mode  | Phases |
|-------|--------|
| QUICK | ICMP latency / jitter / loss for the selected duration (4 probes/s, does not saturate the link) |
| TCP   | 5 s latency, TCP upload, TCP download (+ optional `--bidir` when iPerf3 >= 3.7) |
| UDP   | 5 s latency, UDP test at the target bandwidth |
| FULL  | validation, latency, TCP upload, TCP download, UDP, consolidated result |

Before every test the backend verifies: interface exists, is UP and has an IPv4 address; server
address/port are valid; iPerf3 is installed; the server name resolves and the TCP port accepts a
connection (otherwise **IPERF SERVER UNREACHABLE**). Traffic is bound to the selected interface's IPv4
address (`-B`).

- **TCP upload** (client -> server): `iperf3 -c SERVER -p PORT -P STREAMS -t DURATION -J`
- **TCP download** (server -> client, reverse mode): same command plus `-R`. This is labelled
  DOWNLOAD and is *not* the same as the local NIC TX counter.
- **UDP**: `iperf3 -c SERVER -u -b <target> -P 1 -t DURATION -J`. Reports achieved throughput, jitter,
  lost and total datagrams and loss %. UDP has no congestion control: aggressive targets intentionally
  congest the link.

Reported TCP throughput is the receiver-side average; peak is the highest 1-second interval.
TCP retransmissions are shown when iPerf3 reports them (often unavailable on Windows senders).

### Latency, jitter and loss

Latency is measured with ICMP echo via the Windows `IcmpSendEcho2Ex` API (no admin rights, no `ping.exe`
parsing), at 4 probes per second. Jitter is the mean absolute difference between consecutive RTTs.
The RTT includes a small application overhead (~0.5 ms). If the server firewall blocks ICMP, latency
fields are left empty with a warning instead of reporting 100 % loss. UDP jitter/loss from iPerf3 are
reported separately.

### Link speed / PHY rate vs measured throughput

- **Ethernet** - `LINK THROUGHPUT EFFICIENCY = measured TCP throughput / negotiated link speed x 100`.
  Ethernet + IP + TCP overhead limits TCP goodput to roughly 94 % of the line rate (about 941 Mbps on
  1 Gbps), so 100 % is not reachable.
- **Wi-Fi** - `PHY THROUGHPUT EFFICIENCY = measured throughput / PHY rate x 100` (download vs PHY RX,
  upload vs PHY TX). The PHY rate is the modulation rate of the radio link; real Wi-Fi throughput is
  typically 50-70 % of it because of half-duplex airtime, contention, ACKs and retries. If `netsh` does
  not return PHY rates, the adapter link speed reported by Windows is used and labelled as such.
- Neither value is "utilization" and neither is a universal standard. Expected results depend on the
  server, CPU, NIC, drivers and topology.

Health thresholds are configurable in `config.json`:

```json
{
  "speedtest_efficiency_normal_percent": 85,
  "speedtest_efficiency_warning_percent": 60
}
```

`>= normal` -> NORMAL, `>= warning` -> WARNING, otherwise INVESTIGATE.

### Context snapshots, history and comparison

At the start and end of each test the dashboard records interface, IP, link speed, duplex,
error/drop counters and, for Wi-Fi, SSID, BSSID, RSSI, signal %, band, channel and PHY RX/TX rates.
Warnings are raised for BSSID changes (roaming), counter increases or the interface going down.

Results (including failed and cancelled tests) are stored in SQLite at `data/speedtest_history.db`,
table `speed_test_results`. The full result JSON, including both context snapshots, is kept in
`details_json`. The tab shows recent tests, a download/upload history chart (last 10 / 50 / 24 h), and a
comparison with the previous completed test on the same interface.

Wi-Fi details come from `netsh wlan show interfaces`. On Windows 11 24H2 and later, this requires
**Settings > Privacy & security > Location** to be on, with desktop apps allowed. Without that, SSID,
BSSID, RSSI and PHY rates are shown as unavailable.

### Network impact

TCP and UDP tests saturate the path for the selected duration and may temporarily affect other users and
applications. Run them deliberately, preferably outside production hours. Only one test can run at a
time (`409 SPEED TEST ALREADY RUNNING`). **CANCEL TEST** terminates the iPerf3 process, and hung processes
are terminated (then killed) after `duration + 20 s`.

### Windows Firewall

- **Server side**: allow inbound **TCP and UDP** on the iPerf3 port (default 5201) for `iperf3.exe`.
  UDP tests need UDP 5201 open as well. Example (elevated PowerShell on the server):

```powershell
New-NetFirewallRule -DisplayName "iPerf3 TCP" -Direction Inbound -Protocol TCP -LocalPort 5201 -Action Allow
New-NetFirewallRule -DisplayName "iPerf3 UDP" -Direction Inbound -Protocol UDP -LocalPort 5201 -Action Allow
```

- To measure latency, allow ICMPv4 Echo Request on the server ("File and Printer Sharing (Echo
  Request - ICMPv4-In)").
- When the local iPerf server feature is used, the same inbound rules are needed on this PC.

### Speed test API

- `GET /api/speedtest/capabilities` - iPerf3 detection, version and supported features
- `GET /api/speedtest/interfaces` - interfaces with type, link speed and Wi-Fi details
- `GET /api/speedtest/config` / `POST /api/speedtest/config` - saved server and port
- `GET /api/speedtest/status` - state (`IDLE`, `VALIDATING`, `LATENCY_TEST`, `TCP_UPLOAD`, `TCP_DOWNLOAD`,
  `TCP_BIDIRECTIONAL`, `UDP_TEST`, `COMPLETED`, `FAILED`, `CANCELLED`) and progress
- `POST /api/speedtest/start` - starts a test (202), or returns 409 if one is already running
- `POST /api/speedtest/cancel`
- `GET /api/speedtest/results` - latest result
- `GET /api/speedtest/history?limit=50&hours=24`
- `GET /api/speedtest/server`, `POST /api/speedtest/server/start`, `POST /api/speedtest/server/stop`

Example start request:

```json
{
  "interface": "Ethernet",
  "server": "192.168.1.100",
  "port": 5201,
  "mode": "full",
  "duration_seconds": 10,
  "parallel_streams": 4,
  "udp_bandwidth_mbps": 100,
  "bidirectional": false
}
```

Progress is pushed over the existing `WS /ws/network` socket as `{"type": "speedtest_progress", ...}` and
`{"type": "speedtest_complete", "result": {...}}`. Monitoring snapshots are unchanged.

### Security

- Requests are validated with Pydantic. Unknown fields are rejected, so arbitrary iPerf3 flags cannot
  be passed.
- The server must be an IPv4 address or an RFC 1123 hostname. The port, duration (5/10/20/30),
  streams (1/2/4/8) and UDP bandwidth (1-10000 Mbps) are range-checked.
- iPerf3 is started with `asyncio.create_subprocess_exec` and an argument array. `shell=True` is never
  used, and the executable comes only from `PATH` or `config.json`.
- The hostname is resolved first, and only the validated IPv4 address is passed to iPerf3.
- Only sanitized metadata is logged. Errors returned to the browser are mapped messages, never stack
  traces.

### Known limitations

- IPv4 only (IPv6 servers are rejected).
- Wi-Fi details depend on `netsh` and Windows Location permission. RSSI is estimated from signal %
  when Windows does not report it.
- ICMP RTT includes a small API overhead and is blocked by many default firewalls.
- iPerf3 JSON output is produced at the end of each phase. Progress is time-based, not live throughput.
- If the dashboard process is killed abruptly (not shut down normally), a running iPerf3 child process may
  have to be ended manually.

## Build a standalone Windows app (target PC does not need Python)

Build on a Windows machine with Python and the project dependencies installed. The receiving PC does
**not** need Python. The default build is a portable folder, which is recommended over a single EXE for
this web app; it includes the Python runtime, backend dependencies, templates and static assets.

From the project folder, run:

```bat
python -m pip install -r requirements-build.txt
build_standalone.bat
```

The executable is `dist\EthernetMonitor\EthernetMonitor.exe`. Copy the **entire** `dist\EthernetMonitor`
folder to the target PC, then double-click the EXE. It starts the local server on `127.0.0.1:8000` and
opens the browser. Leave the console window open while using it; close that window to stop the app.
It binds to `127.0.0.1:8000`; if that port is already taken, set the `ETHERNET_MONITOR_PORT`
environment variable before launching (for example, `set ETHERNET_MONITOR_PORT=8765` in Command Prompt).

The frozen app stores writable settings and history here (not inside the bundled files):

```text
%LOCALAPPDATA%\EthernetMonitor\config.json
%LOCALAPPDATA%\EthernetMonitor\data\speedtest_history.db
```

Source/development runs continue to use `config.json` and `data\speedtest_history.db` in the project
folder. The packaged app is loopback-only; it is not exposed to the LAN. Local iPerf server mode, when
explicitly enabled, has its own selected network bind address.

### Optional: include iPerf3 in the package

iPerf3 is a separate native executable and is not a Python dependency. To make TCP/UDP testing work
without separately installing iPerf3 on the target PC, place a Windows-compatible `iperf3.exe` at:

```text
vendor\iperf3\iperf3.exe
```

Then build as above; `build_standalone.bat` bundles it. The app checks an explicitly configured
`iperf3_path`, then the bundled executable, then `PATH`. If you don't include it, install iPerf3 on the
target machine or point `iperf3_path` in `%LOCALAPPDATA%\EthernetMonitor\config.json` to its location.
The remote iPerf3 **server** is still required for LAN/WLAN tests and must be installed/running on the
other machine (`iperf3 -s`). Check the iPerf3 distribution's license and your organization's software
policy before redistributing its binary.

### Optional: build a single EXE

For a single file, run:

```bat
build_standalone.bat --onefile
```

This bundles and extracts dependencies into a temporary directory at startup, so it launches more
slowly and may trigger stricter antivirus checks. Persistent config and test history remain under
`%LOCALAPPDATA%\EthernetMonitor`. The folder build is easier to troubleshoot and update; for most
deployments, distribute that folder (or zip it) instead of the one-file EXE.
