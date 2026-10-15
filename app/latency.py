"""ICMP latency / jitter / loss measurement (low-rate, does not saturate the link)."""
from __future__ import annotations

import asyncio
import socket
import struct
import sys
from statistics import mean
from time import perf_counter
from typing import Any, Callable

ProbeFunc = Callable[[str, str | None, int], "float | None"]


def summarize_rtts(sent: int, rtts_ms: list[float]) -> dict[str, Any]:
    """Jitter = mean absolute difference between consecutive RTTs (RFC 3550 style, unsmoothed)."""
    received = len(rtts_ms)
    loss = ((sent - received) / sent * 100.0) if sent > 0 else 0.0
    jitter = mean(abs(b - a) for a, b in zip(rtts_ms, rtts_ms[1:])) if received >= 2 else (0.0 if received == 1 else None)
    return {
        "sent": sent,
        "received": received,
        "packet_loss_percent": round(loss, 2),
        "latency_min_ms": round(min(rtts_ms), 3) if rtts_ms else None,
        "latency_avg_ms": round(mean(rtts_ms), 3) if rtts_ms else None,
        "latency_max_ms": round(max(rtts_ms), 3) if rtts_ms else None,
        "jitter_ms": round(jitter, 3) if jitter is not None else None,
    }


class WindowsIcmpProbe:
    """ICMP echo through iphlpapi IcmpSendEcho2Ex: no admin rights or ping.exe parsing required."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        lib = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        self._create = lib.IcmpCreateFile
        self._create.restype = wintypes.HANDLE
        self._send = lib.IcmpSendEcho2Ex
        self._send.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ]
        self._send.restype = ctypes.c_ulong
        self._close = lib.IcmpCloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._payload = b"ethernet-monitor-latency-probe00"

    @staticmethod
    def _ip_to_ulong(address: str) -> int:
        return int.from_bytes(socket.inet_aton(address), "little")

    def __call__(self, destination: str, source: str | None, timeout_ms: int) -> float | None:
        ctypes = self._ctypes
        handle = self._create()
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("IcmpCreateFile failed")
        try:
            data = ctypes.create_string_buffer(self._payload, len(self._payload))
            reply = ctypes.create_string_buffer(256 + len(self._payload))
            started = perf_counter()
            count = self._send(
                handle, None, None, None,
                self._ip_to_ulong(source) if source else 0,
                self._ip_to_ulong(destination),
                data, len(self._payload), None, reply, len(reply), timeout_ms,
            )
            elapsed_ms = (perf_counter() - started) * 1000.0
            if count == 0:
                return None
            _, status, rtt_ms = struct.unpack_from("<III", reply.raw, 0)
            if status != 0:
                return None
            # perf_counter gives sub-ms resolution; the API RTT is integer ms only.
            return elapsed_ms if elapsed_ms < rtt_ms + 5 else float(rtt_ms)
        finally:
            self._close(handle)


def default_probe() -> ProbeFunc | None:
    if sys.platform != "win32":
        return None
    try:
        return WindowsIcmpProbe()
    except (OSError, AttributeError):
        return None


async def run_icmp_latency(
    destination: str,
    source: str | None,
    *,
    count: int,
    interval_seconds: float = 0.25,
    timeout_ms: int = 1000,
    cancel_event: asyncio.Event | None = None,
    probe: ProbeFunc | None = None,
) -> dict[str, Any]:
    probe = probe or default_probe()
    if probe is None:
        return {**summarize_rtts(0, []), "method": "icmp", "error": "ICMP latency measurement is not available on this platform."}

    rtts: list[float] = []
    sent = 0
    for index in range(count):
        if cancel_event is not None and cancel_event.is_set():
            break
        started = perf_counter()
        sent += 1
        try:
            rtt = await asyncio.to_thread(probe, destination, source, timeout_ms)
        except OSError:
            rtt = None
        if rtt is not None:
            rtts.append(rtt)
        if index < count - 1:
            await asyncio.sleep(max(0.0, interval_seconds - (perf_counter() - started)))
    return {**summarize_rtts(sent, rtts), "method": "icmp", "error": None}
