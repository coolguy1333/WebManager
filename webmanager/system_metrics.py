"""Lightweight host resource metrics without extra dependencies.

Linux values come from /proc. Anything unavailable on the current platform is
returned as None so the UI can show "n/a" instead of failing.
"""

import os
import platform
import shutil
import socket
import threading
import time
from pathlib import Path


_size_cache = {}
_size_lock = threading.Lock()
SIZE_CACHE_SECONDS = 120
SIZE_WALK_LIMIT = 200_000


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _cpu_times():
    text = _read("/proc/stat")
    if not text:
        return None
    parts = text.splitlines()[0].split()[1:]
    values = [int(value) for value in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(interval=0.2):
    first = _cpu_times()
    if first is None:
        return None
    time.sleep(interval)
    second = _cpu_times()
    total = second[0] - first[0]
    idle = second[1] - first[1]
    if total <= 0:
        return 0.0
    return round(100 * (total - idle) / total, 1)


def memory():
    text = _read("/proc/meminfo")
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        try:
            values[key] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    if not total or available is None:
        return None
    swap_total = values.get("SwapTotal", 0)
    swap_used = swap_total - values.get("SwapFree", 0)
    return {
        "total": total,
        "used": total - available,
        "percent": round(100 * (total - available) / total, 1),
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": round(100 * swap_used / swap_total, 1) if swap_total else 0.0,
    }


def disk(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(100 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


def uptime_seconds():
    text = _read("/proc/uptime")
    if not text:
        return None
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def process_memory():
    text = _read("/proc/self/status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def network_totals():
    text = _read("/proc/net/dev")
    if not text:
        return None
    received = sent = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) >= 9:
            received += int(fields[0])
            sent += int(fields[8])
    return {"received": received, "sent": sent}


def directory_size(path):
    """Total size of a directory tree, cached briefly because walking is slow."""
    path = Path(path)
    now = time.monotonic()
    with _size_lock:
        cached = _size_cache.get(path)
        if cached and now - cached[0] < SIZE_CACHE_SECONDS:
            return cached[1]
    total = 0
    seen = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                seen += 1
                if seen > SIZE_WALK_LIMIT:
                    break
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
            if seen > SIZE_WALK_LIMIT:
                break
    except OSError:
        return None
    with _size_lock:
        _size_cache[path] = (now, total)
    return total


def load_average():
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def collect(app):
    config = app.config
    data_dir = Path(app.instance_path)
    cpu_count = os.cpu_count() or 1
    load = load_average()
    return {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_count": cpu_count,
        "cpu_percent": cpu_percent(),
        "load": load,
        "load_percent": round(100 * load[0] / cpu_count, 1) if load else None,
        "memory": memory(),
        "disk": disk(data_dir),
        "uptime": uptime_seconds(),
        "process_memory": process_memory(),
        "network": network_totals(),
        "storage": {
            "repositories": directory_size(config["REPOSITORY_ROOT"]),
            "logs": directory_size(config["LOG_ROOT"]),
            "nginx": directory_size(config["NGINX_ROOT"]),
            "database": _file_size(config["DATABASE"]),
        },
        "sampled_at": int(time.time()),
    }


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None
