"""Saving settings on the primary and then on each replica, one at a time.

Each server has its own settings file, and saving it restarts WebManager
there, so a change meant for the whole group must never restart every server
at once. When a super admin ticks "also the other servers" on the primary's
Settings tab, this keeps a small plan in the data folder (it outlives the
primary's own restart) and the mesh hub's polling loop works through it:

1. Wait until the primary has saved and restarted (its own control status).
   If that failed and the old settings were put back, stop: nothing is sent.
2. Send the change to the first replica (``/mesh/control``, action
   ``settings``), in replicaN order, and wait until it reports the result in
   its status (``settings_change``), or 10 minutes.
3. Then the next one, and so on. The Settings tab shows how far it got.
"""

import json
import os
import time
from pathlib import Path

from . import server_control

STATE_FILE = "settings-rollout.json"
# A replica that took longer than this to report back is skipped.
REPLICA_TIMEOUT_SECONDS = 600
# Also never send to the next one sooner than this after the last.
MIN_SECONDS_PER_SERVER = 30


def _path(app) -> Path:
    return Path(app.config.get("MESH_STATE_DIR") or app.instance_path) / STATE_FILE


def load(app) -> dict | None:
    try:
        data = json.loads(_path(app).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save(app, state: dict):
    path = _path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def replicas_in_order(app, hub) -> list[str]:
    """The replicas this server sees, in replicaN order."""
    from . import server_aliases
    from .db import get_db

    numbers = {url: number for number, url in server_aliases.load(get_db()).items()}
    urls = [entry["url"] for entry in hub.entries() if entry.get("role") == "replica"]
    return sorted(urls, key=lambda url: (numbers.get(url, 10**6), url))


def start(app, changes: dict, urls: list[str], own_status: dict | None):
    """Plan the rollout. Called after this server's own request is filed."""
    _save(app, {
        "changes": changes,
        "queue": list(urls),
        "current": None,
        "results": [],
        "own_before": (own_status or {}).get("at"),
        "own_done": False,
        "finished": False,
        "started_at": int(time.time()),
    })


def _settings_change(data: dict) -> dict:
    change = data.get("settings_change")
    return change if isinstance(change, dict) else {}


def advance(app, hub):
    """One step, from the hub's polling loop (after each round)."""
    state = load(app)
    if not state or state.get("finished"):
        return
    now = time.time()

    if not state.get("own_done"):
        status = server_control.read_status()
        if not status or status.get("action") != "settings" or status.get("at") == state.get("own_before"):
            return  # this server hasn't finished saving yet
        if status["state"] in server_control.BUSY_STATES:
            return
        if status["state"] == "error":
            state.update(finished=True, aborted=(
                "It didn't work on this server, so the other servers were left as they are."
            ))
            _save(app, state)
            return
        state["own_done"] = True
        state["last_sent_at"] = now - MIN_SECONDS_PER_SERVER

    current = state.get("current")
    if current:
        data = hub.remote_data(current["url"])
        change = _settings_change(data)
        if change.get("state") in ("done", "error") and change.get("at") != current.get("before"):
            state["results"].append({
                "url": current["url"], "ok": change["state"] == "done",
                "message": change.get("message") or "",
            })
            state["current"] = None
        elif now - current["sent_at"] > REPLICA_TIMEOUT_SECONDS:
            state["results"].append({
                "url": current["url"], "ok": False,
                "message": "It didn't report back within 10 minutes; check its System page.",
            })
            state["current"] = None
        else:
            _save(app, state)
            return

    if state["queue"] and now - state.get("last_sent_at", 0) >= MIN_SECONDS_PER_SERVER:
        url = state["queue"].pop(0)
        before = _settings_change(hub.remote_data(url)).get("at")
        accepted, message, body = hub.post_control(url, "settings", {"changes": state["changes"]})
        state["last_sent_at"] = now
        if not accepted:
            state["results"].append({"url": url, "ok": False, "message": message})
        elif body.get("unchanged"):
            state["results"].append({"url": url, "ok": True, "message": message})
        else:
            state["current"] = {"url": url, "sent_at": now, "before": before}
    if not state["queue"] and not state.get("current"):
        state["finished"] = True
        state["finished_at"] = int(now)
    _save(app, state)


def summary(app) -> dict | None:
    """For the Settings tab: where the last rollout got to."""
    state = load(app)
    if not state:
        return None
    return {
        "finished": bool(state.get("finished")),
        "aborted": state.get("aborted"),
        "waiting_here": not state.get("own_done") and not state.get("finished"),
        "current": (state.get("current") or {}).get("url"),
        "queue": state.get("queue") or [],
        "results": state.get("results") or [],
        "keys": sorted(state.get("changes") or {}),
    }
