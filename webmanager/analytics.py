import copy
import gzip
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


# The most log read for one report, across today's log and the rotated ones.
MAX_LOG_BYTES = 32 * 1024 * 1024
_ROTATED_SUFFIX_RE = re.compile(r"\.(\d+)(?:\.gz)?")
_cache: dict = {}
_cache_lock = threading.Lock()


def site_analytics(log_path: str | Path, hostname: str | list[str], days: int = 30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    hostnames = {
        value.lower()
        for value in ([hostname] if isinstance(hostname, str) else hostname)
    }
    requests = []
    for record in _recent_records(Path(log_path), hostnames, cutoff):
        if str(record.get("host", "")).lower() not in hostnames:
            continue
        try:
            recorded_at = datetime.fromisoformat(record["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        if recorded_at < cutoff:
            continue
        record["_time"] = recorded_at
        requests.append(record)

    visitors = {
        str(record.get("client", "")).split(",", 1)[0].strip()
        for record in requests
        if record.get("client")
    }
    paths = Counter()
    statuses = Counter()
    daily = Counter()
    transferred = 0
    for record in requests:
        path = urlsplit(str(record.get("uri", "/"))).path or "/"
        paths[path] += 1
        statuses[str(record.get("status", "unknown"))] += 1
        daily[record["_time"].date().isoformat()] += 1
        try:
            transferred += int(record.get("bytes", 0))
        except (TypeError, ValueError):
            pass

    daily_rows = sorted(daily.items())
    return {
        "days": days,
        "requests": len(requests),
        "visitors": len(visitors),
        "bytes": transferred,
        "top_paths": paths.most_common(8),
        "statuses": sorted(statuses.items()),
        "daily": daily_rows,
        "daily_max": max((count for _, count in daily_rows), default=1),
    }


def _log_files(path: Path) -> list[Path]:
    """Today's log, then the rotated ones newest first (access.log.1,
    access.log.2.gz, ...). logrotate starts a new log every day, so reading
    only the current file would make a "last 30 days" report show hours."""
    rotated = []
    try:
        for candidate in path.parent.glob(path.name + ".*"):
            match = _ROTATED_SUFFIX_RE.fullmatch(candidate.name[len(path.name):])
            if match and candidate.is_file():
                rotated.append((int(match.group(1)), candidate))
    except OSError:
        pass
    return ([path] if path.is_file() else []) + [file for _, file in sorted(rotated)]


def _read_lines(file: Path, budget: int) -> list[bytes]:
    """Up to ``budget`` bytes of whole lines (the end of a plain file, the
    start of a compressed one)."""
    try:
        if file.suffix == ".gz":
            with gzip.open(file, "rb") as handle:
                data = handle.read(budget + 1)
            lines = data.splitlines()
            if len(data) > budget:
                lines = lines[:-1]  # the last line was cut
            return lines
        with file.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - budget)
            handle.seek(start)
            if start:
                handle.readline()
            return handle.readlines()
    except (OSError, EOFError, gzip.BadGzipFile):
        return []


def _recent_records(path: Path, hostnames=None, since: datetime | None = None):
    """Parsed JSON lines from the current and rotated logs, newest first, up
    to MAX_LOG_BYTES. When ``hostnames`` is given, lines that can't be about
    one of them are skipped without being parsed, which is most of a log on a
    server that hosts several sites. Rotated logs last written before
    ``since`` hold nothing newer and are not read."""
    wanted = [name.lower().encode("utf-8") for name in hostnames] if hostnames else None
    records = []
    budget = MAX_LOG_BYTES
    for index, file in enumerate(_log_files(path)):
        if budget <= 0:
            break
        if index and since is not None:
            try:
                if file.stat().st_mtime < since.timestamp():
                    break  # this one and every older one is before the window
            except OSError:
                continue
        lines = _read_lines(file, budget)
        budget -= sum(len(line) + 1 for line in lines)
        for line in lines:
            if wanted is not None:
                lowered = line.lower()
                if not any(name in lowered for name in wanted):
                    continue
            try:
                records.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    return records


def _signature(path: Path) -> tuple:
    parts = []
    for file in _log_files(path):
        try:
            details = file.stat()
        except OSError:
            continue
        parts.append((file.name, details.st_mtime_ns, details.st_size))
    return tuple(parts)


def aggregate_analytics(log_path: str | Path, site_hostnames: dict, days: int = 30):
    """Summarise traffic for several sites (see ``_aggregate``). The answer is
    kept for a minute while the logs are unchanged, so reloading or opening
    several pages doesn't re-read them each time."""
    path = Path(log_path)
    key = (
        _signature(path),
        tuple(sorted((site_id, tuple(sorted(names))) for site_id, names in site_hostnames.items())),
        days,
        int(time.time() // 60),
    )
    with _cache_lock:
        cached = _cache.get(key)
    if cached is None:
        cached = _aggregate(path, site_hostnames, days)
        with _cache_lock:
            if len(_cache) >= 16:
                _cache.clear()
            _cache[key] = cached
    return copy.deepcopy(cached)


def _aggregate(log_path: Path, site_hostnames: dict, days: int):
    """Summarise traffic for several sites in one pass over the access logs.

    ``site_hostnames`` maps a site id to the hostnames it answers on. Returns
    overall totals plus a per-site breakdown and a zero-filled daily series so
    charts always cover the whole period.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    host_to_site = {}
    for site_id, hostnames in site_hostnames.items():
        for hostname in hostnames:
            host_to_site[hostname.lower()] = site_id

    day_keys = [
        (now - timedelta(days=offset)).date().isoformat()
        for offset in range(days - 1, -1, -1)
    ]
    daily = {day: 0 for day in day_keys}
    daily_by_site = {site_id: {day: 0 for day in day_keys} for site_id in site_hostnames}
    per_site = {
        site_id: {"requests": 0, "bytes": 0, "visitors": set(), "errors": 0}
        for site_id in site_hostnames
    }
    visitors = set()
    paths = Counter()
    statuses = Counter()
    transferred = 0
    requests = 0

    for record in _recent_records(Path(log_path), host_to_site, cutoff):
        site_id = host_to_site.get(str(record.get("host", "")).lower())
        if site_id is None:
            continue
        try:
            recorded_at = datetime.fromisoformat(record["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        if recorded_at < cutoff:
            continue
        day = recorded_at.astimezone(timezone.utc).date().isoformat()
        client = str(record.get("client", "")).split(",", 1)[0].strip()
        try:
            size = int(record.get("bytes", 0))
        except (TypeError, ValueError):
            size = 0
        status = str(record.get("status", "unknown"))

        requests += 1
        transferred += size
        if day in daily:
            daily[day] += 1
            daily_by_site[site_id][day] += 1
        if client:
            visitors.add(client)
            per_site[site_id]["visitors"].add(client)
        paths[urlsplit(str(record.get("uri", "/"))).path or "/"] += 1
        statuses[status] += 1
        per_site[site_id]["requests"] += 1
        per_site[site_id]["bytes"] += size
        if status[:1] in {"4", "5"}:
            per_site[site_id]["errors"] += 1

    status_groups = Counter()
    for status, count in statuses.items():
        status_groups[f"{status[:1]}xx" if status[:1].isdigit() else "other"] += count

    return {
        "days": days,
        "requests": requests,
        "visitors": len(visitors),
        "bytes": transferred,
        "errors": status_groups.get("4xx", 0) + status_groups.get("5xx", 0),
        "daily": [(day, daily[day]) for day in day_keys],
        "daily_max": max(daily.values(), default=0),
        "daily_by_site": {
            site_id: [(day, series[day]) for day in day_keys]
            for site_id, series in daily_by_site.items()
        },
        "status_groups": sorted(status_groups.items()),
        "top_paths": paths.most_common(10),
        "per_site": {
            site_id: {
                "requests": values["requests"],
                "bytes": values["bytes"],
                "visitors": len(values["visitors"]),
                "errors": values["errors"],
            }
            for site_id, values in per_site.items()
        },
    }
