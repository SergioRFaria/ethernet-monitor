(() => {
    const state = {
        socket: null,
        reconnectTimer: null,
        historyLimit: 300,
        throughputChart: null,
        utilizationChart: null,
        latestSnapshot: null,
    };

    const elements = {};

    function $(id) {
        return document.getElementById(id);
    }

    function bindElements() {
        [
            "connection-status",
            "monitoring-status",
            "interface-select",
            "refresh-button",
            "reset-button",
            "unavailable-banner",
            "summary-interface",
            "summary-state",
            "summary-ipv4",
            "summary-mac",
            "summary-link-speed",
            "summary-duration",
            "summary-updated",
            "summary-packet-health",
            "rx-current",
            "rx-peak",
            "rx-average",
            "rx-utilization",
            "rx-total-bytes",
            "rx-packets",
            "rx-errors",
            "rx-drops",
            "tx-current",
            "tx-peak",
            "tx-average",
            "tx-utilization",
            "tx-total-bytes",
            "tx-packets",
            "tx-errors",
            "tx-drops",
            "rx-utilization-class",
            "tx-utilization-class",
        ].forEach((id) => {
            elements[id] = $(id);
        });
    }

    function formatMbps(value) {
        return `${Number(value || 0).toFixed(1)} Mbps`;
    }

    function formatPercent(value) {
        return `${Number(value || 0).toFixed(1)}%`;
    }

    function formatBytes(bytes) {
        const numeric = Number(bytes || 0);
        if (numeric >= 1024 ** 3) {
            return `${(numeric / 1024 ** 3).toFixed(2)} GB`;
        }
        if (numeric >= 1024 ** 2) {
            return `${(numeric / 1024 ** 2).toFixed(2)} MB`;
        }
        if (numeric >= 1024) {
            return `${(numeric / 1024).toFixed(2)} KB`;
        }
        return `${numeric.toFixed(0)} B`;
    }

    function formatDuration(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds || 0)));
        const hours = String(Math.floor(total / 3600)).padStart(2, "0");
        const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
        const secs = String(total % 60).padStart(2, "0");
        return `${hours}:${minutes}:${secs}`;
    }

    function formatTimestamp(value) {
        if (!value) {
            return "—";
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleString();
    }

    function utilizationClass(value) {
        const numeric = Number(value || 0);
        if (numeric > 80) {
            return "bad";
        }
        if (numeric >= 60) {
            return "warn";
        }
        return "normal";
    }

    function setStatusPill(element, value, kind) {
        element.textContent = value;
        element.classList.remove("status-pill--good", "status-pill--warn", "status-pill--bad");
        element.classList.add(`status-pill--${kind}`);
    }

    function setUtilChip(element, value) {
        const kind = utilizationClass(value);
        element.textContent = kind === "normal" ? "Normal" : kind === "warn" ? "Warning" : "High utilization";
        element.classList.remove("util-chip--normal", "util-chip--warn", "util-chip--bad");
        element.classList.add(`util-chip--${kind}`);
    }

    function setUnavailableBanner(snapshot) {
        const banner = elements["unavailable-banner"];
        const unavailable = snapshot?.status === "MONITORING_UNAVAILABLE";
        if (!unavailable) {
            banner.classList.add("banner--hidden");
            banner.textContent = "";
            return;
        }
        const message = snapshot.message || "Monitoring unavailable.";
        const available = Array.isArray(snapshot.available_interfaces) ? snapshot.available_interfaces : [];
        const availableText = available.length
            ? ` Available interfaces: ${available.map((item) => item.name).join(", ")}.`
            : " No active interfaces were detected.";
        banner.textContent = `MONITORING UNAVAILABLE — ${message}${availableText}`;
        banner.classList.remove("banner--hidden");
    }

    function buildThroughputChart() {
        const context = $("throughput-chart").getContext("2d");
        return new Chart(context, {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    {
                        label: "Download / RX",
                        data: [],
                        borderColor: "#60a5fa",
                        backgroundColor: "rgba(96, 165, 250, 0.16)",
                        tension: 0.25,
                        pointRadius: 0,
                        fill: true,
                    },
                    {
                        label: "Upload / TX",
                        data: [],
                        borderColor: "#32d583",
                        backgroundColor: "rgba(50, 213, 131, 0.12)",
                        tension: 0.25,
                        pointRadius: 0,
                        fill: true,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: {
                    mode: "index",
                    intersect: false,
                },
                plugins: {
                    legend: {
                        labels: {
                            color: "#d8e3f0",
                        },
                    },
                    tooltip: {
                        callbacks: {
                            label(context) {
                                return `${context.dataset.label}: ${Number(context.parsed.y || 0).toFixed(2)} Mbps`;
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            color: "#8ea2b8",
                            maxRotation: 0,
                            autoSkip: true,
                        },
                        grid: {
                            color: "rgba(148, 163, 184, 0.12)",
                        },
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            color: "#8ea2b8",
                        },
                        title: {
                            display: true,
                            text: "Mbps",
                            color: "#8ea2b8",
                        },
                        grid: {
                            color: "rgba(148, 163, 184, 0.12)",
                        },
                    },
                },
            },
        });
    }

    function buildUtilizationChart() {
        const context = $("utilization-chart").getContext("2d");
        return new Chart(context, {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    {
                        label: "RX Utilization %",
                        data: [],
                        borderColor: "#fbbf24",
                        backgroundColor: "rgba(251, 191, 36, 0.14)",
                        tension: 0.25,
                        pointRadius: 0,
                        fill: true,
                    },
                    {
                        label: "TX Utilization %",
                        data: [],
                        borderColor: "#f97316",
                        backgroundColor: "rgba(249, 115, 22, 0.12)",
                        tension: 0.25,
                        pointRadius: 0,
                        fill: true,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: {
                    mode: "index",
                    intersect: false,
                },
                plugins: {
                    legend: {
                        labels: {
                            color: "#d8e3f0",
                        },
                    },
                    tooltip: {
                        callbacks: {
                            label(context) {
                                return `${context.dataset.label}: ${Number(context.parsed.y || 0).toFixed(2)}%`;
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            color: "#8ea2b8",
                            maxRotation: 0,
                            autoSkip: true,
                        },
                        grid: {
                            color: "rgba(148, 163, 184, 0.12)",
                        },
                    },
                    y: {
                        beginAtZero: true,
                        suggestedMax: 100,
                        max: 100,
                        ticks: {
                            color: "#8ea2b8",
                            callback(value) {
                                return `${value}%`;
                            },
                        },
                        title: {
                            display: true,
                            text: "% of link capacity",
                            color: "#8ea2b8",
                        },
                        grid: {
                            color: "rgba(148, 163, 184, 0.12)",
                        },
                    },
                },
            },
        });
    }

    function ensureCharts() {
        if (!state.throughputChart) {
            state.throughputChart = buildThroughputChart();
        }
        if (!state.utilizationChart) {
            state.utilizationChart = buildUtilizationChart();
        }
    }

    function seedCharts(history) {
        ensureCharts();
        const items = Array.isArray(history) ? history.slice(-state.historyLimit) : [];
        const labels = items.map((item) => new Date(item.timestamp).toLocaleTimeString());
        const rxValues = items.map((item) => Number(item.rx_mbps || 0));
        const txValues = items.map((item) => Number(item.tx_mbps || 0));
        const rxUtil = items.map((item) => Number(item.rx_utilization || 0));
        const txUtil = items.map((item) => Number(item.tx_utilization || 0));

        state.throughputChart.data.labels = labels;
        state.throughputChart.data.datasets[0].data = rxValues;
        state.throughputChart.data.datasets[1].data = txValues;
        state.throughputChart.update("none");

        state.utilizationChart.data.labels = labels;
        state.utilizationChart.data.datasets[0].data = rxUtil;
        state.utilizationChart.data.datasets[1].data = txUtil;
        state.utilizationChart.update("none");
    }

    function appendHistoryPoint(snapshot) {
        ensureCharts();
        const label = new Date(snapshot.timestamp).toLocaleTimeString();
        const rx = Number(snapshot.rx_mbps || 0);
        const tx = Number(snapshot.tx_mbps || 0);
        const rxUtil = Number(snapshot.rx_utilization || 0);
        const txUtil = Number(snapshot.tx_utilization || 0);

        const charts = [
            state.throughputChart,
            state.utilizationChart,
        ];
        charts.forEach((chart) => {
            chart.data.labels.push(label);
            while (chart.data.labels.length > state.historyLimit) {
                chart.data.labels.shift();
            }
        });

        state.throughputChart.data.datasets[0].data.push(rx);
        state.throughputChart.data.datasets[1].data.push(tx);
        state.utilizationChart.data.datasets[0].data.push(rxUtil);
        state.utilizationChart.data.datasets[1].data.push(txUtil);

        [state.throughputChart, state.utilizationChart].forEach((chart) => {
            chart.data.datasets.forEach((dataset) => {
                while (dataset.data.length > state.historyLimit) {
                    dataset.data.shift();
                }
            });
            chart.update("none");
        });
    }

    function renderSnapshot(snapshot) {
        if (!snapshot) {
            return;
        }
        state.latestSnapshot = snapshot;
        if (typeof snapshot.history_limit === "number") {
            state.historyLimit = Math.max(30, snapshot.history_limit);
        }

        elements["summary-interface"].textContent = snapshot.interface || "—";
        elements["summary-state"].textContent = snapshot.status || "—";
        elements["summary-ipv4"].textContent = snapshot.ipv4_address || "—";
        elements["summary-mac"].textContent = snapshot.mac_address || "—";
        elements["summary-link-speed"].textContent = snapshot.link_speed_label || (snapshot.link_speed_mbps ? `${snapshot.link_speed_mbps} Mbps` : "Unknown");
        elements["summary-duration"].textContent = formatDuration(snapshot.monitoring_duration_seconds);
        elements["summary-updated"].textContent = formatTimestamp(snapshot.timestamp);
        elements["summary-packet-health"].textContent = `RX ${snapshot.rx_packets || 0} / TX ${snapshot.tx_packets || 0}`;

        const stateKind = snapshot.status === "UP" ? "up" : snapshot.status === "DOWN" ? "down" : "unavailable";
        elements["summary-state"].classList.remove("status-text--up", "status-text--down", "status-text--unavailable");
        elements["summary-state"].classList.add(`status-text--${stateKind}`);

        elements["rx-current"].textContent = formatMbps(snapshot.rx_mbps);
        elements["rx-peak"].textContent = formatMbps(snapshot.rx_peak_mbps);
        elements["rx-average"].textContent = formatMbps(snapshot.rx_average_mbps);
        elements["rx-utilization"].textContent = formatPercent(snapshot.rx_utilization);
        elements["rx-total-bytes"].textContent = formatBytes(snapshot.total_rx_bytes);
        elements["rx-packets"].textContent = String(snapshot.rx_packets || 0);
        elements["rx-errors"].textContent = String(snapshot.rx_errors || 0);
        elements["rx-drops"].textContent = String(snapshot.rx_drops || 0);

        elements["tx-current"].textContent = formatMbps(snapshot.tx_mbps);
        elements["tx-peak"].textContent = formatMbps(snapshot.tx_peak_mbps);
        elements["tx-average"].textContent = formatMbps(snapshot.tx_average_mbps);
        elements["tx-utilization"].textContent = formatPercent(snapshot.tx_utilization);
        elements["tx-total-bytes"].textContent = formatBytes(snapshot.total_tx_bytes);
        elements["tx-packets"].textContent = String(snapshot.tx_packets || 0);
        elements["tx-errors"].textContent = String(snapshot.tx_errors || 0);
        elements["tx-drops"].textContent = String(snapshot.tx_drops || 0);

        setStatusPill(
            elements["monitoring-status"],
            snapshot.status === "UP" ? "UP" : snapshot.status === "DOWN" ? "DOWN" : "UNAVAILABLE",
            snapshot.status === "UP" ? "good" : snapshot.status === "DOWN" ? "bad" : "warn"
        );
        setUtilChip(elements["rx-utilization-class"], snapshot.rx_utilization);
        setUtilChip(elements["tx-utilization-class"], snapshot.tx_utilization);
        setUnavailableBanner(snapshot);
    }

    function setConnectionStatus(text, kind) {
        setStatusPill(elements["connection-status"], text, kind);
    }

    function populateInterfaceSelect(interfaces, activeName) {
        const select = elements["interface-select"];
        select.innerHTML = "";
        const items = Array.isArray(interfaces) ? interfaces : [];
        if (!items.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = "No interfaces found";
            select.appendChild(option);
            select.disabled = true;
            return;
        }
        select.disabled = false;
        items.forEach((item) => {
            const option = document.createElement("option");
            option.value = item.name;
            const ip = item.ipv4_address ? ` • ${item.ipv4_address}` : "";
            const speed = item.link_speed_label ? ` • ${item.link_speed_label}` : "";
            option.textContent = `${item.name}${ip}${speed}`;
            if (item.name === activeName) {
                option.selected = true;
            }
            select.appendChild(option);
        });
        if (!Array.from(select.options).some((option) => option.selected)) {
            select.selectedIndex = 0;
        }
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, {
            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {}),
            },
            ...options,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = payload?.detail || payload?.message || response.statusText;
            throw new Error(detail);
        }
        return payload;
    }

    async function loadStatus() {
        const snapshot = await fetchJson("/api/status");
        ensureCharts();
        seedCharts(snapshot.history || []);
        renderSnapshot(snapshot);
    }

    async function loadInterfaces() {
        const snapshot = state.latestSnapshot;
        const activeName = snapshot?.interface || elements["interface-select"].value || "";
        const interfaces = await fetchJson("/api/interfaces");
        populateInterfaceSelect(interfaces, activeName);
        if (snapshot && snapshot.status === "MONITORING_UNAVAILABLE" && activeName && !interfaces.some((item) => item.name === activeName)) {
            elements["interface-select"].value = interfaces[0]?.name || "";
        }
    }

    async function refreshAll() {
        try {
            await loadStatus();
            await loadInterfaces();
            setConnectionStatus("Connected", "good");
        } catch (error) {
            console.error(error);
            setConnectionStatus("Offline", "bad");
        }
    }

    async function changeInterface(name) {
        try {
            const snapshot = await fetchJson("/api/interface", {
                method: "POST",
                body: JSON.stringify({ interface: name }),
            });
            seedCharts(snapshot.history || []);
            renderSnapshot(snapshot);
            setConnectionStatus("Connected", "good");
        } catch (error) {
            alert(`Unable to select interface: ${error.message}`);
            await loadInterfaces();
        }
    }

    async function resetStatistics() {
        try {
            const snapshot = await fetchJson("/api/reset-statistics", {
                method: "POST",
                body: "{}",
            });
            seedCharts(snapshot.history || []);
            renderSnapshot(snapshot);
        } catch (error) {
            alert(`Unable to reset statistics: ${error.message}`);
        }
    }

    function scheduleReconnect() {
        if (state.reconnectTimer) {
            return;
        }
        state.reconnectTimer = window.setTimeout(() => {
            state.reconnectTimer = null;
            connectWebSocket();
        }, 2000);
    }

    function connectWebSocket() {
        if (state.socket && (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING)) {
            return;
        }

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const socket = new WebSocket(`${protocol}//${window.location.host}/ws/network`);
        state.socket = socket;
        setConnectionStatus("Connecting…", "warn");

        socket.onopen = () => {
            setConnectionStatus("Connected", "good");
        };

        socket.onmessage = (event) => {
            try {
                const snapshot = JSON.parse(event.data);
                if (typeof snapshot.type === "string" && snapshot.type.startsWith("speedtest_")) {
                    window.SpeedTest?.handleMessage(snapshot);
                    return;
                }
                renderSnapshot(snapshot);
                if (!state.throughputChart?.data.labels.length && snapshot.history?.length) {
                    seedCharts(snapshot.history);
                } else {
                    appendHistoryPoint(snapshot);
                }
            } catch (error) {
                console.error("Failed to parse WebSocket message", error);
            }
        };

        socket.onerror = () => {
            setConnectionStatus("Offline", "bad");
        };

        socket.onclose = () => {
            setConnectionStatus("Reconnecting…", "warn");
            scheduleReconnect();
        };
    }

    function selectTab(name) {
        document.querySelectorAll(".tab").forEach((tab) => {
            const active = tab.dataset.tab === name;
            tab.classList.toggle("tab--active", active);
            tab.setAttribute("aria-selected", String(active));
            $(tab.getAttribute("aria-controls")).hidden = !active;
        });
    }

    function wireEvents() {
        document.querySelectorAll(".tab").forEach((tab) => {
            tab.addEventListener("click", () => selectTab(tab.dataset.tab));
        });
        elements["refresh-button"].addEventListener("click", refreshAll);
        elements["reset-button"].addEventListener("click", resetStatistics);
        elements["interface-select"].addEventListener("change", (event) => {
            const selected = event.target.value;
            if (selected) {
                changeInterface(selected);
            }
        });
    }

    async function bootstrap() {
        bindElements();
        ensureCharts();
        wireEvents();
        await refreshAll();
        connectWebSocket();
    }

    document.addEventListener("DOMContentLoaded", () => {
        bootstrap().catch((error) => {
            console.error(error);
            setConnectionStatus("Offline", "bad");
        });
    });
})();
