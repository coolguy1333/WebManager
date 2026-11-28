"""Traffic reports from the site gateway's access log.

Reading every log on every page load is what made Analytics slow: the logs
change with every visit to any site, so a cached answer was never reused, and
a busy server's 30-day report re-read tens of megabytes each time (while a
32 MB cap could still cover less than a day). Instead the logs are read once
into running totals per hostname and day, and after that only the lines added
since. A log file is recognised by its first line, which stays the same when
logrotate copies it to access.log.1 (copytruncate) and later compresses it to
access.log.2.gz, so nothing is counted twice and a finished file is never read
again. A background thread keeps the totals current (keep_index_fresh).
"""

import gzip
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reports go back at most this far (the longest period on the Analytics page).
MAX_DAYS = 90
# The most log read when the index is first built (newest first); after that
# only new lines are read.
MAX_LOG_BYTES = 512 * 1024 * 1024
# Top pages are kept per hostname and day; beyond this many the rarest go.
MAX_PATHS_PER_BUCKET = 400
# The System page's health check visits each site; that is not traffic.
HEALTH_PROBE_AGENT = "webmanager-health"
_ROTATED_SUFFIX_RE = re.compile(r"\.(\d+)(?:\.gz)?")
_indexes: dict = {}
_indexes_lock = threading.Lock()


def _log_files(path: Path) -> list[Path]:
    """Today's log, then the rotated ones newest first (access.log.1,
    access.log.2.gz, ...)."""
    rotated = []
    try:
        for candidate in path.parent.glob(path.name + ".*"):
            match = _ROTATED_SUFFIX_RE.fullmatch(candidate.name[len(path.name):])
            if match and candidate.is_file():
                rotated.append((int(match.group(1)), candidate))
    except OSError:
        pass
    return ([path] if path.is_file() else []) + [file for _, file in sorted(rotated)]


class _Bucket:
    """One hostname's traffic on one day."""

    __slots__ = ("requests", "bytes", "errors", "statuses", "paths", "visitors")

    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.errors = 0
        self.statuses = Counter()
        self.paths = Counter()
        self.visitors = set()


def _identity(file: Path) -> bytes | None:
    """A log file's first line, or None while it has no complete line."""
    try:
        opener = gzip.open if file.suffix == ".gz" else open
        with opener(file, "rb") as handle:
            line = handle.readline(4096)
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return line if line.endswith(b"\n") else None


def _day_of(stamp) -> str | None:
    """The UTC day (YYYY-MM-DD) of an ISO 8601 time from the log."""
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    if stamp.endswith(("+00:00", "Z")):
        return stamp[:10]
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def _day_keys(days: int) -> list[str]:
    now = datetime.now(timezone.utc)
    return [(now - timedelta(days=offset)).date().isoformat() for offset in range(days - 1, -1, -1)]


class TrafficIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.buckets: dict[tuple[str, str], _Bucket] = {}
        # first line -> {"offset": bytes read so far, "complete": a rotated file read to its end}
        self.files: dict[bytes, dict] = {}
        self.bytes_read = 0  # how much the last refresh read (tests check it)
        self.files_read: list[Path] = []

    # -- reading ----------------------------------------------------------
    def refresh(self):
        with self.lock:
            self._refresh()

    def _refresh(self):
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=MAX_DAYS)
        cutoff_day = window_start.date().isoformat()
        budget = MAX_LOG_BYTES
        self.bytes_read = 0
        self.files_read = []
        seen = set()
        for position, file in enumerate(_log_files(self.path)):
            if budget <= 0:
                break
            if position:
                try:
                    if file.stat().st_mtime < window_start.timestamp():
                        break  # this one and every older one is before the window
                except OSError:
                    continue
            identity = _identity(file)
            if identity is None:
                continue
            seen.add(identity)
            state = self.files.get(identity)
            if state is not None and state["complete"]:
                continue
            self.files_read.append(file)
            consumed, end = self._read(file, state["offset"] if state else None, budget, cutoff_day)
            budget -= consumed
            self.bytes_read += consumed
            self.files[identity] = {"offset": end, "complete": position > 0}
        for identity in [identity for identity in self.files if identity not in seen]:
            del self.files[identity]  # rotated away for good
        for key in [key for key in self.buckets if key[1] < cutoff_day]:
            del self.buckets[key]

    def _read(self, file: Path, offset, budget: int, cutoff_day: str) -> tuple[int, int]:
        """Read whole new lines from ``offset`` (None: never read before, so
        the newest ``budget`` bytes). Returns (bytes read, new offset)."""
        try:
            if file.suffix == ".gz":
                with gzip.open(file, "rb") as handle:
                    skipped = 0
                    while offset and skipped < offset:
                        chunk = handle.read(min(1 << 20, offset - skipped))
                        if not chunk:
                            break
                        skipped += len(chunk)
                    data = handle.read(budget)
                start = offset or 0
            else:
                with file.open("rb") as handle:
                    size = handle.seek(0, 2)
                    start = offset if offset is not None else max(0, size - budget)
                    if start > size:
                        start = 0  # truncated in place since the last read
                    handle.seek(start)
                    data = handle.read(min(budget, size - start))
                if offset is None and start:
                    cut = data.find(b"\n")  # begin at a whole line
                    start += cut + 1
                    data = data[cut + 1:] if cut >= 0 else b""
        except (OSError, EOFError, gzip.BadGzipFile):
            return 0, offset or 0
        last = data.rfind(b"\n")
        if last < 0:
            return 0, start
        data = data[: last + 1]
        for line in data.splitlines():
            self._ingest(line, cutoff_day)
        return len(data), start + len(data)

    def _ingest(self, line: bytes, cutoff_day: str):
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(record, dict) or record.get("agent") == HEALTH_PROBE_AGENT:
            return
        host = str(record.get("host", "")).lower()
        day = _day_of(record.get("time"))
        if not host or day is None or day < cutoff_day:
            return
        bucket = self.buckets.get((host, day))
        if bucket is None:
            bucket = self.buckets[(host, day)] = _Bucket()
        status = str(record.get("status", "unknown"))
        try:
            size = int(record.get("bytes", 0))
        except (TypeError, ValueError):
            size = 0
        bucket.requests += 1
        bucket.bytes += size
        bucket.statuses[status] += 1
        if status[:1] in {"4", "5"}:
            bucket.errors += 1
        path = str(record.get("uri", "/")).split("?", 1)[0].split("#", 1)[0] or "/"
        bucket.paths[path] += 1
        if len(bucket.paths) > MAX_PATHS_PER_BUCKET:
            bucket.paths = Counter(dict(bucket.paths.most_common(MAX_PATHS_PER_BUCKET // 2)))
        client = str(record.get("client", "")).split(",", 1)[0].strip()
        if client:
            bucket.visitors.add(client)

    # -- reports ----------------------------------------------------------
    def report(self, site_hostnames: dict, days: int) -> dict:
        """Overall totals, a per-site breakdown and zero-filled daily series
        for ``site_hostnames`` (site id -> the hostnames it answers on)."""
        day_keys = _day_keys(days)
        host_to_site = {
            hostname.lower(): site_id for site_id, hostnames in site_hostnames.items() for hostname in hostnames
        }
        daily = dict.fromkeys(day_keys, 0)
        daily_by_site = {site_id: dict.fromkeys(day_keys, 0) for site_id in site_hostnames}
        per_site = {
            site_id: {"requests": 0, "bytes": 0, "visitors": set(), "errors": 0} for site_id in site_hostnames
        }
        visitors, paths, statuses = set(), Counter(), Counter()
        requests = transferred = 0
        with self.lock:
            for (host, day), bucket in self.buckets.items():
                site_id = host_to_site.get(host)
                if site_id is None or day not in daily:
                    continue
                requests += bucket.requests
                transferred += bucket.bytes
                daily[day] += bucket.requests
                daily_by_site[site_id][day] += bucket.requests
                visitors |= bucket.visitors
                paths.update(bucket.paths)
                statuses.update(bucket.statuses)
                site = per_site[site_id]
                site["requests"] += bucket.requests
                site["bytes"] += bucket.bytes
                site["errors"] += bucket.errors
                site["visitors"] |= bucket.visitors
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
                site_id: [(day, series[day]) for day in day_keys] for site_id, series in daily_by_site.items()
            },
            "status_groups": sorted(status_groups.items()),
            "statuses": sorted(statuses.items()),
            "top_paths": paths.most_common(10),
            "per_site": {
                site_id: {
                    "requests": values["requests"], "bytes": values["bytes"],
                    "visitors": len(values["visitors"]), "errors": values["errors"],
                }
                for site_id, values in per_site.items()
            },
        }


def _index_for(path) -> TrafficIndex:
    key = str(Path(path))
    with _indexes_lock:
        index = _indexes.get(key)
        if index is None:
            index = _indexes[key] = TrafficIndex(Path(path))
        return index


def aggregate_analytics(log_path, site_hostnames: dict, days: int = 30) -> dict:
    """Traffic for several sites over the last ``days`` days. Only log lines
    added since the last report are read."""
    index = _index_for(log_path)
    index.refresh()
    return index.report(site_hostnames, days)


def site_analytics(log_path, hostname, days: int = 30) -> dict:
    """One site's traffic: totals, top pages, exact status codes, and the
    days that had any."""
    hostnames = [hostname] if isinstance(hostname, str) else list(hostname)
    data = aggregate_analytics(log_path, {0: hostnames}, days)
    daily_rows = [(day, count) for day, count in data["daily"] if count]
    return {
        "days": days,
        "requests": data["requests"],
        "visitors": data["visitors"],
        "bytes": data["bytes"],
        "top_paths": data["top_paths"][:8],
        "statuses": data["statuses"],
        "daily": daily_rows,
        "daily_max": max((count for _, count in daily_rows), default=1),
    }


def keep_index_fresh(app, log_path, every_seconds: int = 60):
    """Build the index at start-up and keep it current in the background, so
    even the first visit to Analytics after a restart is quick."""
    def run():
        index = _index_for(log_path)
        while True:
            try:
                index.refresh()
            except Exception:  # never let the reader stop over one bad file
                app.logger.exception("Could not read the traffic logs")
            time.sleep(every_seconds)

    thread = threading.Thread(target=run, name="webmanager-traffic-index", daemon=True)
    thread.start()
    return thread
