"""Metrics for the machine the cockpit itself runs on (via psutil).

When the cockpit runs in a container, CPU/memory/load reflect the Docker
host (they come from /proc, which isn't namespaced), and disk usage is for
the filesystem backing the data volume.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import time

import psutil

from ..config import DATA_DIR, settings

_STARTED_AT = time.time()


def prime() -> None:
    """psutil.cpu_percent(None) measures since the previous call; prime it at startup."""
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)


def host_metrics() -> dict:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(str(DATA_DIR))
    net = psutil.net_io_counters()
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    return {
        "time": time.time(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "per_cpu": psutil.cpu_percent(interval=None, percpu=True),
            "count": psutil.cpu_count() or 1,
            "load": [round(x, 2) for x in load],
        },
        "memory": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent},
        "disk": {"path": str(DATA_DIR), "total": disk.total, "used": disk.used, "percent": disk.percent},
        "net": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv},
        "uptime": time.time() - psutil.boot_time(),
    }


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> dict:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def about() -> dict:
    db_path = settings.database_url.replace("sqlite:///", "", 1) if settings.database_url.startswith("sqlite") else None
    db_size = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else None
    return {
        "version": settings.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "in_container": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
        "data_dir": str(DATA_DIR),
        "database": settings.database_url if not db_path else db_path,
        "db_size": db_size,
        "process_uptime": time.time() - _STARTED_AT,
        "scheduler": settings.scheduler_enabled,
        "timezone": time.strftime("%Z (UTC%z)"),
    }
