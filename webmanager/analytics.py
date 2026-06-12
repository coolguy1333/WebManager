import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


MAX_LOG_BYTES = 8 * 1024 * 1024


def site_analytics(log_path: str | Path, hostname: str | list[str], days: int = 30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    hostnames = {
        value.lower()
        for value in ([hostname] if isinstance(hostname, str) else hostname)
    }
    requests = []
    for record in _recent_records(Path(log_path)):
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


def _recent_records(path: Path):
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - MAX_LOG_BYTES))
            if size > MAX_LOG_BYTES:
                handle.readline()
            lines = handle.readlines()
    except OSError:
        return []

    records = []
    for line in lines:
        try:
            records.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return records
