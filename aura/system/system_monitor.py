"""Background system monitor.

Runs psutil polling on its own QThread and emits an immutable `SystemStats`
snapshot, so the GUI thread never blocks on a system call. Missing psutil or a
platform that refuses a particular metric yields `None` for that field rather
than an exception.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QMutex, QMutexLocker, QThread, QWaitCondition, Signal

from config import MONITOR_INTERVAL_S
from system.system_info import app_uptime_seconds, format_bytes, format_duration

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SystemStats:
    """One poll of the host. Any field may be None if unavailable."""

    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used: float | None = None
    memory_total: float | None = None
    disk_percent: float | None = None
    disk_used: float | None = None
    disk_total: float | None = None
    net_online: bool | None = None
    net_sent_rate: float | None = None
    net_recv_rate: float | None = None
    battery_percent: float | None = None
    battery_plugged: bool | None = None
    battery_secs_left: int | None = None
    app_uptime: float = 0.0
    error: str | None = None

    # -- display helpers ----------------------------------------------------
    @property
    def memory_text(self) -> str:
        if self.memory_percent is None:
            return "N/A"
        if self.memory_used is None or self.memory_total is None:
            return f"{self.memory_percent:.0f}%"
        return (
            f"{self.memory_percent:.0f}%  "
            f"{format_bytes(self.memory_used)}/{format_bytes(self.memory_total)}"
        )

    @property
    def disk_text(self) -> str:
        if self.disk_percent is None:
            return "N/A"
        if self.disk_used is None or self.disk_total is None:
            return f"{self.disk_percent:.0f}%"
        return f"{self.disk_percent:.0f}%  {format_bytes(self.disk_total)} total"

    @property
    def battery_text(self) -> str:
        if self.battery_percent is None:
            return "AC POWER"
        state = "CHARGING" if self.battery_plugged else "BATTERY"
        if self.battery_secs_left and self.battery_secs_left > 0 and not self.battery_plugged:
            return f"{self.battery_percent:.0f}% · {format_duration(self.battery_secs_left)}"
        return f"{self.battery_percent:.0f}% · {state}"

    @property
    def network_text(self) -> str:
        if self.net_online is None:
            return "UNKNOWN"
        if not self.net_online:
            return "OFFLINE"
        if self.net_recv_rate is None:
            return "ONLINE"
        return f"ONLINE  ↓{format_bytes(self.net_recv_rate)}/s"

    def summary_for_assistant(self) -> str:
        """One-line plain-text digest the assistant can read back to the user."""
        parts = []
        if self.cpu_percent is not None:
            parts.append(f"CPU at {self.cpu_percent:.0f} percent")
        if self.memory_percent is not None:
            parts.append(f"memory at {self.memory_percent:.0f} percent")
        if self.disk_percent is not None:
            parts.append(f"disk at {self.disk_percent:.0f} percent")
        if self.battery_percent is not None:
            state = "charging" if self.battery_plugged else "on battery"
            parts.append(f"battery {self.battery_percent:.0f} percent {state}")
        if self.net_online is not None:
            parts.append("network online" if self.net_online else "network offline")
        if not parts:
            return "System metrics are unavailable on this machine."
        return "All monitored systems nominal: " + ", ".join(parts) + "."


class SystemMonitor(QThread):
    """Polls the host on a worker thread and emits `stats_ready`."""

    stats_ready = Signal(object)   # SystemStats
    failed = Signal(str)

    def __init__(self, interval: float = MONITOR_INTERVAL_S, parent=None) -> None:
        super().__init__(parent)
        self._interval = max(0.25, interval)
        self._running = False
        self._mutex = QMutex()
        self._wait = QWaitCondition()
        self._last_net: tuple[float, float, float] | None = None
        self._warned = False
        self.latest = SystemStats(error=None if psutil else "psutil not installed")

    def stop(self) -> None:
        with QMutexLocker(self._mutex):
            self._running = False
            self._wait.wakeAll()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._running = True
        if psutil is None:
            self.failed.emit(
                "psutil is not installed — system metrics disabled. "
                "Install it with: pip install psutil"
            )
            stats = SystemStats(error="psutil not installed")
            self.latest = stats
            self.stats_ready.emit(stats)
            return

        # Prime the non-blocking CPU sampler; the first call always reads 0.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        while True:
            with QMutexLocker(self._mutex):
                if not self._running:
                    break
            stats = self._sample()
            self.latest = stats
            self.stats_ready.emit(stats)
            with QMutexLocker(self._mutex):
                if not self._running:
                    break
                self._wait.wait(self._mutex, int(self._interval * 1000))

    # -- sampling -----------------------------------------------------------
    def _sample(self) -> SystemStats:
        cpu = mem_pct = mem_used = mem_total = None
        disk_pct = disk_used = disk_total = None
        net_online = sent_rate = recv_rate = None
        batt_pct = batt_secs = None
        batt_plugged = None
        error = None

        try:
            cpu = float(psutil.cpu_percent(interval=None))
        except Exception as exc:
            error = f"cpu: {exc}"

        try:
            vm = psutil.virtual_memory()
            mem_pct, mem_used, mem_total = float(vm.percent), float(vm.used), float(vm.total)
        except Exception as exc:
            error = error or f"memory: {exc}"

        try:
            du = psutil.disk_usage("/")
            disk_pct, disk_used, disk_total = float(du.percent), float(du.used), float(du.total)
        except Exception as exc:
            error = error or f"disk: {exc}"

        try:
            counters = psutil.net_io_counters()
            now = time.monotonic()
            if counters is not None:
                if self._last_net is not None:
                    prev_t, prev_sent, prev_recv = self._last_net
                    span = max(1e-6, now - prev_t)
                    sent_rate = max(0.0, (counters.bytes_sent - prev_sent) / span)
                    recv_rate = max(0.0, (counters.bytes_recv - prev_recv) / span)
                self._last_net = (now, counters.bytes_sent, counters.bytes_recv)
            net_online = self._interfaces_up()
        except Exception as exc:
            error = error or f"network: {exc}"

        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                batt_pct = float(battery.percent)
                batt_plugged = bool(battery.power_plugged)
                secs = battery.secsleft
                # psutil uses sentinels for "unknown" / "plugged in".
                if isinstance(secs, int) and secs >= 0:
                    batt_secs = secs
        except Exception:
            # Desktops and VMs legitimately have no battery — not an error.
            pass

        return SystemStats(
            cpu_percent=cpu,
            memory_percent=mem_pct,
            memory_used=mem_used,
            memory_total=mem_total,
            disk_percent=disk_pct,
            disk_used=disk_used,
            disk_total=disk_total,
            net_online=net_online,
            net_sent_rate=sent_rate,
            net_recv_rate=recv_rate,
            battery_percent=batt_pct,
            battery_plugged=batt_plugged,
            battery_secs_left=batt_secs,
            app_uptime=app_uptime_seconds(),
            error=error,
        )

    @staticmethod
    def _interfaces_up() -> bool | None:
        """True if any non-loopback interface is up. Cheap, and never blocks."""
        try:
            stats = psutil.net_if_stats()
        except Exception:
            return None
        for name, info in stats.items():
            if name.startswith("lo"):
                continue
            if getattr(info, "isup", False):
                return True
        return False
