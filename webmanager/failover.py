"""Is this group of servers in a failover situation right now?

Three situations get a large warning on every page (base.html) and a
notification (the watchdog), because each needs a person to look:

- ``promoted``: this server took over as the primary. It stays until a super
  admin confirms they have checked things (an "acknowledged" file).
- ``primary_down``: this is a replica and its primary hasn't answered for a
  while, so it serves sites but can't save changes.
- ``two_primaries``: another server in the list also says it is the primary
  (an old primary came back after its replica was promoted), so changes made
  on one never reach the other.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .replication import promoted_marker

# How long a replica's primary may be silent before it counts as down
# (settings are copied every 15 seconds, so this is several missed rounds).
PRIMARY_DOWN_AFTER_SECONDS = 90


def acknowledged_marker(app) -> Path:
    return promoted_marker(app).with_name("promoted-acknowledged")


def _when(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write_promoted_marker(app, previous_primary: str, by: str, config_synced_at=None, data_synced_at=None):
    """Record that this server took over (what it took over from, and how
    fresh its copy was). Raises OSError."""
    marker = promoted_marker(app)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "from": previous_primary,
                "by": by,
                "at": int(time.time()),
                "config_synced_at": int(config_synced_at) if config_synced_at else None,
                "data_synced_at": int(data_synced_at) if data_synced_at else None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    acknowledged_marker(app).unlink(missing_ok=True)


def promotion(app) -> dict | None:
    """Details of this server's takeover, or None if it never took over."""
    marker = promoted_marker(app)
    try:
        details = marker.stat()
        text = marker.read_text(encoding="utf-8")[:4096]
    except OSError:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        # Written by an older version: "Promoted from replica of <url>".
        words = text.strip().split()
        data = {"from": words[-1] if words else None}
    acknowledged = acknowledged_marker(app)
    try:
        acknowledged_at = acknowledged.stat().st_mtime
    except OSError:
        acknowledged_at = None
    at = data.get("at") if isinstance(data.get("at"), (int, float)) else details.st_mtime
    return {
        "from": str(data.get("from") or app.config.get("PROMOTED_FROM") or "") or None,
        "by": data.get("by") if data.get("by") in {"system page", "webmanager-promote"} else None,
        "at": _when(at),
        "config_synced_at": _when(data.get("config_synced_at")) if isinstance(data.get("config_synced_at"), int) else None,
        "data_synced_at": _when(data.get("data_synced_at")) if isinstance(data.get("data_synced_at"), int) else None,
        "acknowledged": acknowledged_at is not None and acknowledged_at >= details.st_mtime,
        "key": f"{int(details.st_mtime)}",
    }


def acknowledge(app, who: str):
    """A super admin has checked things after the takeover. Raises OSError."""
    path = acknowledged_marker(app)
    path.write_text(json.dumps({"by": who, "at": int(time.time())}) + "\n", encoding="utf-8")


def state(app) -> dict | None:
    """The failover situation to warn about, or None. Cheap: no network."""
    manager = app.extensions.get("replication_manager")
    if manager is not None and manager.is_replica:
        last_ok = manager.last_sync_at or getattr(manager, "started_at", None)
        if manager.last_error and last_ok and time.time() - last_ok > PRIMARY_DOWN_AFTER_SECONDS:
            return {
                "kind": "primary_down",
                "primary": manager.primary_url,
                "since": _when(manager.last_sync_at),
                "error": manager.last_error,
                "key": f"{int(manager.last_sync_at or 0)}",
            }
        return None
    hub = app.extensions.get("mesh_hub")
    if hub is not None:
        others = [
            entry for entry in hub.entries()
            if entry["reachable"] and entry.get("role") == "primary"
        ]
        # Only a group with replicas can have two primaries; servers that
        # merely watch each other (--peers) are each their own primary.
        promoted = promotion(app)
        if others and (promoted or any(entry.get("promoted_at") for entry in others)):
            other = others[0]
            mine = promoted_marker(app)
            try:
                my_promoted_at = int(mine.stat().st_mtime)
            except OSError:
                my_promoted_at = None
            newer = other.get("promoted_at") and (not my_promoted_at or other["promoted_at"] > my_promoted_at)
            return {
                "kind": "two_primaries",
                "other": other["url"],
                "other_name": other["hostname"],
                "this_is_newer": not newer,
                "key": f"{other['url']}:{other.get('promoted_at')}:{my_promoted_at}",
            }
    promoted = promotion(app)
    if promoted and not promoted["acknowledged"]:
        return {"kind": "promoted", **promoted}
    return None
