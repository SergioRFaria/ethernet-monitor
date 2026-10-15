(() => {
    const st = {
        interfaces: [],
        caps: null,
        status: null,
        historyItems: [],
        sortDesc: true,
        chart: null,
        pollTimer: null,
        loaded: false,
    };

    const PHASE_LABELS = {
        IDLE: "READY TO TEST",
        VALIDATING: "VALIDATING",
        LATENCY_TEST: "LATENCY TEST",
        TCP_UPLOAD: "UPLOAD TEST",
        TCP_DOWNLOAD: "DOWNLOAD TEST",
        TCP_BIDIRECTIONAL: "BIDIRECTIONAL TEST",
        UDP_TEST: "UDP TEST",
        COMPLETED: "TEST COMPLETED",
        FAILED: "TEST FAILED",
        CANCELLED: "TEST CANCELLED",
    };
    const ACTIVE = new Set(["VALIDATING", "LATENCY_TEST", "TCP_UPLOAD", "TCP_DOWNLOAD", "TCP_BIDIRECTIONAL", "UDP_TEST"]);

    const $ = (id) => document.getElementById(id);

    function isNum(value) {
        return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
    }

    function fmt(value, digits, unit) {
        if (!isNum(value)) {
            return "—";
        }
        return `${Number(value).toFixed(digits)}${unit ? ` ${unit}` : ""}`;
    }

    function fmtSpeed(mbps) {
        if (!isNum(mbps) || Number(mbps) <= 0) {
            return "Unknown";
        }
        const value = Number(mbps);
        return value >= 1000 ? `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)} Gbps` : `${value} Mbps`;
    }

    function fmtTime(value) {
        if (!value) {
            return "—";
        }
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
    }

    function signed(value, digits, unit) {
        if (!isNum(value)) {
            return "—";
        }
        const n = Number(value);
        return `${n > 0 ? "+" : ""}${n.toFixed(digits)}${unit}`;
    }

    function errorText(payload, fallback) {
        const detail = payload?.detail;
        if (Array.isArray(detail)) {
            return detail.map((item) => {
                const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
                return `${field ? `${field}: ` : ""}${String(item.msg || "invalid value").replace(/^Value error, /, "")}`;
            }).join("; ");
        }
        return detail || payload?.message || fallback;
    }

    async function api(url, options = {}) {
        const response = await fetch(url, {
            headers: { "Content-Type": "application/json" },
            ...options,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(errorText(payload, response.statusText));
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function setPill(element, text, kind) {
        element.textContent = text;
        element.classList.remove("status-pill--good", "status-pill--warn", "status-pill--bad");
        element.classList.add(`status-pill--${kind}`);
    }

    function fillDl(dl, rows) {
        dl.replaceChildren();
        rows.forEach(([label, value]) => {
            const dt = document.createElement("dt");
            dt.textContent = label;
            const dd = document.createElement("dd");
            dd.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
            dl.append(dt, dd);
        });
    }

    function message(text, kind = "") {
        const element = $("st-message");
        element.textContent = text || "";
        element.classList.toggle("st-note--bad", kind === "bad");
    }

    // ---------------- capabilities ----------------
    async function loadCapabilities() {
        try {
            st.caps = await api("/api/speedtest/capabilities");
        } catch (error) {
            console.error(error);
            return;
        }
        const banner = $("st-iperf-banner");
        if (st.caps.iperf3_installed) {
            $("st-iperf-version").textContent = `iPerf3 ${st.caps.iperf3_version}`;
            banner.classList.add("banner--hidden");
        } else {
            $("st-iperf-version").textContent = "IPERF3 NOT INSTALLED";
            banner.textContent = `IPERF3 NOT INSTALLED — TCP/UDP tests are unavailable (QUICK TEST still works). ${st.caps.install_hint || ""}`;
            banner.classList.remove("banner--hidden");
        }
        const bidir = $("st-bidir");
        bidir.disabled = !st.caps.bidirectional_supported;
        if (bidir.disabled) {
            bidir.checked = false;
        }
        $("st-bidir-note").textContent = st.caps.bidirectional_supported
            ? "Adds a simultaneous upload+download run (--bidir)"
            : "Not supported by installed iPerf3 (requires 3.7+); sequential upload/download is used";
    }

    // ---------------- interfaces ----------------
    function selectedContext() {
        const name = $("st-interface").value;
        return st.interfaces.find((item) => item.interface === name) || null;
    }

    function renderContext() {
        const ctx = selectedContext();
        const dl = $("st-context");
        const reason = $("st-wifi-reason");
        if (!ctx) {
            fillDl(dl, []);
            $("st-if-type").textContent = "—";
            return;
        }
        const typeLabel = { wifi: "WI-FI", ethernet: "ETHERNET", loopback: "LOOPBACK", bluetooth: "BLUETOOTH" }[ctx.interface_type] || "UNKNOWN";
        $("st-if-type").textContent = typeLabel;
        const rows = [
            ["Interface type", typeLabel],
            ["IP address", ctx.ipv4_address],
            ["Connection status", ctx.status],
            ["Negotiated link speed", fmtSpeed(ctx.link_speed_mbps)],
        ];
        if (ctx.interface_type === "ethernet") {
            rows.push(["Duplex", ctx.duplex]);
        }
        const wifi = ctx.wifi;
        if (wifi) {
            rows.push(
                ["SSID", wifi.ssid],
                ["BSSID", wifi.bssid],
                ["RSSI", isNum(wifi.rssi_dbm) ? `${wifi.rssi_dbm} dBm${wifi.rssi_estimated ? " (estimated from signal %)" : ""}` : null],
                ["Signal quality", isNum(wifi.signal_percent) ? `${wifi.signal_percent} %` : null],
                ["PHY TX rate", fmt(wifi.phy_tx_mbps, 1, "Mbps")],
                ["PHY RX rate", fmt(wifi.phy_rx_mbps, 1, "Mbps")],
                ["Band", wifi.band],
                ["Channel", wifi.channel],
            );
            reason.hidden = Boolean(wifi.details_available);
            reason.textContent = wifi.details_available ? "" : wifi.reason || "";
        } else {
            reason.hidden = true;
        }
        fillDl(dl, rows);
    }

    async function loadInterfaces() {
        let payload;
        try {
            payload = await api("/api/speedtest/interfaces");
        } catch (error) {
            console.error(error);
            return;
        }
        st.interfaces = payload.interfaces || [];
        const select = $("st-interface");
        const previous = select.value || $("interface-select")?.value || "";
        select.replaceChildren();
        st.interfaces
            .filter((item) => item.interface_type !== "loopback")
            .forEach((item) => {
                const option = document.createElement("option");
                option.value = item.interface;
                option.textContent = `${item.interface} • ${item.status}${item.ipv4_address ? ` • ${item.ipv4_address}` : ""}`;
                select.appendChild(option);
            });
        const names = Array.from(select.options).map((option) => option.value);
        const preferred = names.includes(previous)
            ? previous
            : (st.interfaces.find((item) => item.status === "UP" && item.ipv4_address && item.interface_type !== "loopback") || {}).interface;
        if (preferred) {
            select.value = preferred;
        }
        renderContext();

        const bind = $("st-srv-bind");
        const currentBind = bind.value;
        bind.replaceChildren();
        const addresses = st.interfaces.filter((item) => item.ipv4_address).map((item) => [item.ipv4_address, `${item.ipv4_address} (${item.interface})`]);
        [...addresses, ["0.0.0.0", "0.0.0.0 (all interfaces)"]].forEach(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            bind.appendChild(option);
        });
        if (currentBind) {
            bind.value = currentBind;
        } else {
            const ctx = selectedContext();
            if (ctx?.ipv4_address) {
                bind.value = ctx.ipv4_address;
            }
        }
    }

    // ---------------- config ----------------
    async function loadConfig() {
        try {
            const config = await api("/api/speedtest/config");
            if (config.server && !$("st-server").value) {
                $("st-server").value = config.server;
            }
            if (config.port) {
                $("st-port").value = config.port;
            }
        } catch (error) {
            console.error(error);
        }
    }

    async function saveServer() {
        try {
            await api("/api/speedtest/config", {
                method: "POST",
                body: JSON.stringify({ server: $("st-server").value.trim(), port: Number($("st-port").value) }),
            });
            message("Server address saved to configuration.");
        } catch (error) {
            message(`Unable to save server: ${error.message}`, "bad");
        }
    }

    // ---------------- test control ----------------
    function udpBandwidth() {
        const choice = $("st-udp-bw").value;
        return choice === "custom" ? Number($("st-udp-custom").value) : Number(choice);
    }

    async function startTest() {
        const server = $("st-server").value.trim();
        if (!server) {
            message("Enter the iPerf3 server address.", "bad");
            return;
        }
        const body = {
            interface: $("st-interface").value,
            server,
            port: Number($("st-port").value),
            mode: $("st-mode").value,
            duration_seconds: Number($("st-duration").value),
            parallel_streams: Number($("st-streams").value),
            udp_bandwidth_mbps: udpBandwidth(),
            bidirectional: $("st-bidir").checked,
        };
        try {
            const status = await api("/api/speedtest/start", { method: "POST", body: JSON.stringify(body) });
            renderStatus(status);
            startPolling();
        } catch (error) {
            if (error.status === 409) {
                message("SPEED TEST ALREADY RUNNING", "bad");
            } else {
                message(`Unable to start test: ${error.message}`, "bad");
            }
        }
    }

    async function cancelTest() {
        $("st-cancel").disabled = true;
        message("Cancelling…");
        try {
            renderStatus(await api("/api/speedtest/cancel", { method: "POST", body: "{}" }));
        } catch (error) {
            message(`Unable to cancel: ${error.message}`, "bad");
        }
    }

    function startPolling() {
        if (st.pollTimer) {
            return;
        }
        // Fallback if the WebSocket is disconnected; WebSocket updates are used when available.
        st.pollTimer = window.setInterval(async () => {
            try {
                const status = await api("/api/speedtest/status");
                renderStatus(status);
                if (!status.running) {
                    stopPolling();
                    await loadResult();
                    await loadHistory();
                }
            } catch (error) {
                console.error(error);
            }
        }, 2000);
    }

    function stopPolling() {
        if (st.pollTimer) {
            window.clearInterval(st.pollTimer);
            st.pollTimer = null;
        }
    }

    function renderStatus(status) {
        if (!status) {
            return;
        }
        st.status = status;
        const state = status.status || "IDLE";
        const running = ACTIVE.has(state);
        const label = PHASE_LABELS[state] || state;
        const kind = state === "FAILED" ? "bad" : running || state === "CANCELLED" ? "warn" : "good";
        setPill($("st-state"), label, kind);
        $("st-phase-label").textContent = label;
        const phasePct = Number(status.progress_percent || 0);
        const overallPct = Number(status.overall_progress_percent || 0);
        $("st-phase-percent").textContent = `${phasePct.toFixed(0)} %`;
        $("st-overall-percent").textContent = `${overallPct.toFixed(0)} %`;
        $("st-phase-bar").style.width = `${Math.min(100, phasePct)}%`;
        $("st-overall-bar").style.width = `${Math.min(100, overallPct)}%`;

        const phases = $("st-phases");
        phases.replaceChildren();
        const list = Array.isArray(status.phases) ? status.phases : [];
        const currentIndex = list.indexOf(state);
        list.forEach((phase, index) => {
            const chip = document.createElement("span");
            chip.className = "st-phase";
            if (state === "COMPLETED" || (currentIndex >= 0 && index < currentIndex)) {
                chip.classList.add("st-phase--done");
            } else if (index === currentIndex) {
                chip.classList.add("st-phase--active");
            }
            chip.textContent = PHASE_LABELS[phase] || phase;
            phases.appendChild(chip);
        });

        $("st-start").disabled = running;
        $("st-cancel").disabled = !running;
        if (state === "FAILED" && status.error) {
            message(`${status.error.code}: ${status.error.message}`, "bad");
        } else if (running) {
            message(`${label} — ${Number(status.elapsed_seconds || 0).toFixed(1)} s elapsed in phase`);
        } else if (state === "CANCELLED") {
            message("Test cancelled. The iPerf3 process was terminated.");
        } else if (state === "COMPLETED") {
            message("TEST COMPLETED");
        }
        if (running) {
            startPolling();
        }
    }

    // ---------------- results ----------------
    function healthClass(health) {
        return { NORMAL: "normal", WARNING: "warn", INVESTIGATE: "bad" }[health] || "";
    }

    function setEffCard(valueId, subId, percent, reference, health, direction) {
        const value = $(valueId);
        value.textContent = isNum(percent) ? `${Number(percent).toFixed(1)} %` : "—";
        value.classList.remove("st-eff--normal", "st-eff--warn", "st-eff--bad");
        const cls = healthClass(health);
        if (cls) {
            value.classList.add(`st-eff--${cls}`);
        }
        $(subId).textContent = isNum(reference)
            ? `${direction} vs ${fmt(reference, 0, "Mbps")}${health ? ` — ${health}` : ""}`
            : `${direction} — reference rate unavailable`;
    }

    function renderResult(result) {
        if (!result) {
            return;
        }
        const summary = result.summary || {};
        const statusKind = result.status === "COMPLETED" ? "" : " (incomplete)";
        $("st-result-status").textContent = `${result.status}${statusKind} • ${fmtTime(result.timestamp)}`;
        $("st-r-download").textContent = fmt(summary.download_mbps, 1, "Mbps");
        $("st-r-upload").textContent = fmt(summary.upload_mbps, 1, "Mbps");
        $("st-r-latency").textContent = fmt(summary.latency_ms, 2, "ms");
        $("st-r-jitter").textContent = fmt(summary.jitter_ms, 2, "ms");
        $("st-r-loss").textContent = fmt(summary.packet_loss_percent, 1, "%");
        $("st-r-download-sub").textContent = result.tcp_download ? `server → client • peak ${fmt(result.tcp_download.peak_mbps, 1, "Mbps")}` : "server → client";
        $("st-r-upload-sub").textContent = result.tcp_upload ? `client → server • peak ${fmt(result.tcp_upload.peak_mbps, 1, "Mbps")}` : "client → server";
        const latency = result.latency || {};
        $("st-r-latency-sub").textContent = latency.received
            ? `ICMP avg • min ${fmt(latency.latency_min_ms, 2, "")} / max ${fmt(latency.latency_max_ms, 2, "ms")}`
            : "ICMP avg RTT";

        const eff = summary.efficiency || {};
        $("st-eff-label").textContent = eff.label || "THROUGHPUT EFFICIENCY";
        $("st-eff-label-up").textContent = eff.label || "THROUGHPUT EFFICIENCY";
        setEffCard("st-r-eff-down", "st-r-eff-down-sub", eff.download_percent, eff.download_reference_mbps, eff.download_health, "download");
        setEffCard("st-r-eff-up", "st-r-eff-up-sub", eff.upload_percent, eff.upload_reference_mbps, eff.upload_health, "upload");

        const udp = result.udp || {};
        $("st-r-udp").textContent = fmt(udp.throughput_mbps, 1, "Mbps");
        $("st-r-udp-sub").textContent = result.udp
            ? `target ${fmt(udp.target_mbps, 0, "Mbps")} • jitter ${fmt(udp.jitter_ms, 3, "ms")} • loss ${fmt(udp.packet_loss_percent, 2, "%")}`
            : "not run";

        const warnings = $("st-warnings");
        warnings.replaceChildren();
        const items = [...(result.warnings || [])];
        if (result.error) {
            items.unshift(`${result.error.code}: ${result.error.message}`);
        }
        items.forEach((text) => {
            const li = document.createElement("li");
            li.textContent = text;
            warnings.appendChild(li);
        });

        const before = result.context_before || {};
        const after = result.context_after || {};
        const up = result.tcp_upload || {};
        const down = result.tcp_download || {};
        const rows = [
            ["Test mode", String(result.mode || "").toUpperCase()],
            ["Status", result.status],
            ["Timestamp", fmtTime(result.timestamp)],
            ["Test duration", isNum(result.duration_seconds) ? `${result.duration_seconds} s ${result.mode === "quick" ? "(ICMP latency test)" : "per iPerf3 phase"}` : null],
            ["Server address", result.server],
            ["Resolved server IP", result.server_ip],
            ["Server port", result.port],
            ["Selected interface", result.interface],
            ["Interface type", before.interface_type],
            ["Interface link speed", fmtSpeed(before.link_speed_mbps)],
            ["Parallel streams", result.parallel_streams],
            ["iPerf3 version", result.iperf3_version],
            ["TCP upload avg / peak", result.tcp_upload ? `${fmt(up.average_mbps, 1, "")} / ${fmt(up.peak_mbps, 1, "Mbps")}` : null],
            ["TCP download avg / peak", result.tcp_download ? `${fmt(down.average_mbps, 1, "")} / ${fmt(down.peak_mbps, 1, "Mbps")}` : null],
            ["TCP retransmissions (up / down)", result.tcp_upload || result.tcp_download ? `${up.retransmits ?? "n/a"} / ${down.retransmits ?? "n/a"}` : null],
            ["UDP throughput", fmt(udp.throughput_mbps, 2, "Mbps")],
            ["UDP lost / total datagrams", result.udp ? `${udp.lost_datagrams ?? "n/a"} / ${udp.total_datagrams ?? "n/a"}` : null],
            ["UDP packet loss", fmt(udp.packet_loss_percent, 3, "%")],
            ["UDP jitter", fmt(udp.jitter_ms, 3, "ms")],
            ["ICMP probes sent / received", latency.sent ? `${latency.sent} / ${latency.received}` : null],
        ];
        if (result.tcp_bidirectional) {
            rows.push(
                ["Bidir upload (simultaneous)", fmt(result.tcp_bidirectional.upload?.throughput_mbps, 1, "Mbps")],
                ["Bidir download (simultaneous)", fmt(result.tcp_bidirectional.download?.throughput_mbps, 1, "Mbps")],
            );
        }
        if (before.wifi) {
            const wb = before.wifi;
            const wa = after.wifi || {};
            const pair = (a, b, unit = "") => (a === null || a === undefined ? "—" : `${a}${unit}`) + (b !== undefined && b !== null && b !== a ? ` → ${b}${unit}` : "");
            rows.push(
                ["SSID", wb.ssid],
                ["BSSID (before → after)", pair(wb.bssid, wa.bssid)],
                ["RSSI (before → after)", pair(wb.rssi_dbm, wa.rssi_dbm, " dBm")],
                ["Signal (before → after)", pair(wb.signal_percent, wa.signal_percent, " %")],
                ["Band / channel", `${wb.band || "—"} / ${wb.channel ?? "—"}`],
                ["PHY RX (before → after)", pair(wb.phy_rx_mbps, wa.phy_rx_mbps, " Mbps")],
                ["PHY TX (before → after)", pair(wb.phy_tx_mbps, wa.phy_tx_mbps, " Mbps")],
            );
        } else if (before.interface_type) {
            rows.push(
                ["Duplex", before.duplex],
                ["RX errors / drops (before → after)", `${before.rx_errors ?? "—"}/${before.rx_drops ?? "—"} → ${after.rx_errors ?? "—"}/${after.rx_drops ?? "—"}`],
                ["TX errors / drops (before → after)", `${before.tx_errors ?? "—"}/${before.tx_drops ?? "—"} → ${after.tx_errors ?? "—"}/${after.tx_drops ?? "—"}`],
            );
        }
        fillDl($("st-secondary"), rows);
        renderComparison(result.comparison);
    }

    function renderComparison(cmp) {
        const body = $("st-compare-body");
        body.replaceChildren();
        if (!cmp) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 4;
            td.textContent = "No previous completed test on this interface.";
            tr.appendChild(td);
            body.appendChild(tr);
            $("st-compare-note").textContent = "same interface";
            return;
        }
        $("st-compare-note").textContent = `vs ${fmtTime(cmp.previous_timestamp)}`;
        const rows = [
            ["Download", fmt(cmp.download.current, 1, "Mbps"), fmt(cmp.download.previous, 1, "Mbps"), signed(cmp.download.difference_percent, 1, " %")],
            ["Upload", fmt(cmp.upload.current, 1, "Mbps"), fmt(cmp.upload.previous, 1, "Mbps"), signed(cmp.upload.difference_percent, 1, " %")],
            ["Latency", fmt(cmp.latency.current, 2, "ms"), fmt(cmp.latency.previous, 2, "ms"), signed(cmp.latency.difference_ms, 2, " ms")],
            ["Packet loss", fmt(cmp.packet_loss.current, 1, "%"), fmt(cmp.packet_loss.previous, 1, "%"), signed(cmp.packet_loss.difference_percent_points, 1, " pp")],
        ];
        rows.forEach((cells) => {
            const tr = document.createElement("tr");
            cells.forEach((text) => {
                const td = document.createElement("td");
                td.textContent = text;
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
    }

    async function loadResult() {
        try {
            const payload = await api("/api/speedtest/results");
            renderResult(payload.result);
        } catch (error) {
            console.error(error);
        }
    }

    // ---------------- history ----------------
    function ensureChart() {
        if (st.chart || typeof Chart === "undefined") {
            return;
        }
        st.chart = new Chart($("st-history-chart").getContext("2d"), {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    { label: "TCP Download Mbps", data: [], borderColor: "#60a5fa", backgroundColor: "rgba(96,165,250,0.16)", tension: 0.2, spanGaps: true },
                    { label: "TCP Upload Mbps", data: [], borderColor: "#32d583", backgroundColor: "rgba(50,213,131,0.12)", tension: 0.2, spanGaps: true },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: "index", intersect: false },
                plugins: { legend: { labels: { color: "#d8e3f0" } } },
                scales: {
                    x: { ticks: { color: "#8ea2b8", maxRotation: 0, autoSkip: true }, grid: { color: "rgba(148,163,184,0.12)" } },
                    y: { beginAtZero: true, ticks: { color: "#8ea2b8" }, title: { display: true, text: "Mbps", color: "#8ea2b8" }, grid: { color: "rgba(148,163,184,0.12)" } },
                },
            },
        });
    }

    function renderHistoryChart() {
        ensureChart();
        if (!st.chart) {
            return;
        }
        const items = st.historyItems
            .filter((item) => isNum(item.tcp_download_mbps) || isNum(item.tcp_upload_mbps))
            .slice()
            .sort((a, b) => a.id - b.id);
        st.chart.data.labels = items.map((item) => new Date(item.timestamp).toLocaleString());
        st.chart.data.datasets[0].data = items.map((item) => (isNum(item.tcp_download_mbps) ? Number(item.tcp_download_mbps) : null));
        st.chart.data.datasets[1].data = items.map((item) => (isNum(item.tcp_upload_mbps) ? Number(item.tcp_upload_mbps) : null));
        st.chart.update("none");
    }

    function renderHistoryTable() {
        const body = $("st-history-body");
        body.replaceChildren();
        const items = st.historyItems.slice().sort((a, b) => (st.sortDesc ? b.id - a.id : a.id - b.id));
        $("st-sort-time").textContent = `TIME ${st.sortDesc ? "▼" : "▲"}`;
        if (!items.length) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 11;
            td.textContent = "No tests recorded yet.";
            tr.appendChild(td);
            body.appendChild(tr);
            return;
        }
        items.forEach((item) => {
            const tr = document.createElement("tr");
            [
                fmtTime(item.timestamp),
                item.interface,
                String(item.interface_type || "—").toUpperCase(),
                String(item.mode || "—").toUpperCase(),
                fmt(item.tcp_download_mbps, 1, "Mbps"),
                fmt(item.tcp_upload_mbps, 1, "Mbps"),
                fmt(item.latency_ms, 2, "ms"),
                fmt(item.jitter_ms, 2, "ms"),
                fmt(item.packet_loss_percent, 1, "%"),
                `${item.server || "—"}:${item.port || ""}`,
                item.status,
            ].forEach((text) => {
                const td = document.createElement("td");
                td.textContent = text ?? "—";
                tr.appendChild(td);
            });
            if (item.status !== "COMPLETED") {
                tr.classList.add("st-row--muted");
                tr.title = item.error_message || "";
            }
            body.appendChild(tr);
        });
    }

    async function loadHistory() {
        const range = $("st-history-range").value;
        const query = range === "24h" ? "limit=1000&hours=24" : `limit=${encodeURIComponent(range)}`;
        try {
            const payload = await api(`/api/speedtest/history?${query}`);
            st.historyItems = payload.items || [];
        } catch (error) {
            console.error(error);
            st.historyItems = [];
        }
        renderHistoryTable();
        renderHistoryChart();
    }

    // ---------------- local iperf server ----------------
    function renderServer(status) {
        const running = Boolean(status?.running);
        setPill($("st-srv-status"), running ? "RUNNING" : "STOPPED", running ? "good" : "warn");
        $("st-srv-info").textContent = running
            ? `Listening on ${status.listening_address}:${status.port} since ${fmtTime(status.started_at)}. Remote clients: iperf3 -c <this PC's IP> -p ${status.port}`
            : status?.last_error || "";
        $("st-srv-start").disabled = running;
        $("st-srv-stop").disabled = !running;
    }

    async function loadServer() {
        try {
            renderServer(await api("/api/speedtest/server"));
        } catch (error) {
            console.error(error);
        }
    }

    async function startServer() {
        try {
            renderServer(await api("/api/speedtest/server/start", {
                method: "POST",
                body: JSON.stringify({ bind_address: $("st-srv-bind").value, port: Number($("st-srv-port").value) }),
            }));
        } catch (error) {
            $("st-srv-info").textContent = `Unable to start server: ${error.message}`;
        }
    }

    async function stopServer() {
        try {
            renderServer(await api("/api/speedtest/server/stop", { method: "POST", body: "{}" }));
        } catch (error) {
            $("st-srv-info").textContent = `Unable to stop server: ${error.message}`;
        }
    }

    // ---------------- wiring ----------------
    function updateModeControls() {
        const mode = $("st-mode").value;
        const tcp = mode === "tcp" || mode === "full";
        const udp = mode === "udp" || mode === "full";
        $("st-streams").disabled = !tcp;
        $("st-bidir").disabled = !tcp || !st.caps?.bidirectional_supported;
        $("st-udp-bw").disabled = !udp;
        $("st-udp-custom").disabled = !udp;
        $("st-udp-warning").hidden = !udp;
        $("st-udp-custom").hidden = $("st-udp-bw").value !== "custom";
    }

    async function refreshTab() {
        await Promise.all([loadCapabilities(), loadInterfaces(), loadServer()]);
        updateModeControls();
    }

    function handleMessage(msg) {
        if (msg.type === "speedtest_progress") {
            renderStatus(msg);
        } else if (msg.type === "speedtest_complete") {
            renderResult(msg.result);
            loadHistory();
        }
    }

    async function init() {
        if (!$("panel-speedtest")) {
            return;
        }
        $("st-interface").addEventListener("change", renderContext);
        $("st-mode").addEventListener("change", updateModeControls);
        $("st-udp-bw").addEventListener("change", updateModeControls);
        $("st-save-server").addEventListener("click", saveServer);
        $("st-start").addEventListener("click", startTest);
        $("st-cancel").addEventListener("click", cancelTest);
        $("st-history-range").addEventListener("change", loadHistory);
        $("st-sort-time").addEventListener("click", () => {
            st.sortDesc = !st.sortDesc;
            renderHistoryTable();
        });
        $("st-srv-start").addEventListener("click", startServer);
        $("st-srv-stop").addEventListener("click", stopServer);
        $("tab-speedtest").addEventListener("click", () => {
            refreshTab();
            window.requestAnimationFrame(() => st.chart?.resize());
        });

        await loadConfig();
        await refreshTab();
        try {
            renderStatus(await api("/api/speedtest/status"));
        } catch (error) {
            console.error(error);
        }
        await loadResult();
        await loadHistory();
    }

    window.SpeedTest = { handleMessage };
    document.addEventListener("DOMContentLoaded", () => {
        init().catch((error) => console.error(error));
    });
})();
