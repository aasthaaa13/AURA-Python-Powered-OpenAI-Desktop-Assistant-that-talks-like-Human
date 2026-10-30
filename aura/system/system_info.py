"""Static host facts and formatting helpers.

Everything here degrades to a placeholder rather than raising: a HUD that cannot
read the battery should still start.
"""

from __future__ import annotations

import platform
import socket
import time
from dataclasses import dataclass

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is optional at runtime
    psutil = None  # type: ignore[assignment]

PSUTIL_AVAILABLE = psutil is not None

_APP_START = time.monotonic()


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    system: str
    release: str
    machine: str
    python: str
    cpu_cores: int | None
    boot_time: float | None

    @property
    def summary(self) -> str:
        return f"{self.system} {self.release} · {self.machine}"


def host_info() -> HostInfo:
    cores = None
    boot = None
    if psutil is not None:
        try:
            cores = psutil.cpu_count(logical=True)
            boot = psutil.boot_time()
        except Exception:
            pass
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    return HostInfo(
        hostname=hostname,
        system=platform.system() or "unknown",
        release=platform.release() or "",
        machine=platform.machine() or "",
        python=platform.python_version(),
        cpu_cores=cores,
        boot_time=boot,
    )


def app_uptime_seconds() -> float:
    return time.monotonic() - _APP_START


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_bytes(value: float | None) -> str:
    if value is None:
        return "--"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} PB"
