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


def aggregate_analytics(log_path: str | Path, site_hostnames: dict, days: int = 30):
    """Summarise traffic for several sites in one pass over the access log.

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

    for record in _recent_records(Path(log_path)):
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
