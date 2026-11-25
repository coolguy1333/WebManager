"""Keeps an eye on the hosting once a minute so nobody has to.

- If the web server in front of the sites has died, start it again.
- Ask every running site for its home page; after two misses in a row call it
  down (and try to bring an app's container back), and say so when it recovers.
- Keep a per-day tally of those checks: the "uptime" figure on each site's page.
- Send a notification (see notifications.py) for those events, for source and
  WebManager updates that failed or are waiting, and for a nearly full disk.

What has already been announced is remembered in a small file in the data
folder, so a restart doesn't repeat old news.
"""

import json
import shutil
import socket
import threading
import time
from pathlib import Path

from . import activity, failover, health, update_status
from .db import get_db
from .domains import site_hostnames

INTERVAL_SECONDS = 60
FAILURES_BEFORE_DOWN = 2
UPTIME_KEEP_DAYS = 90
DISK_WARN_PERCENT = 10
DISK_REMIND_SECONDS = 24 * 3600


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


class Watchdog:
    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        self._sites: dict[int, dict] = {}  # site id -> {"fails": n, "down": bool, "name": str}
        self._state_path = Path(
            app.config.get("WATCHDOG_STATE_FILE") or Path(app.instance_path) / "watchdog-state.json"
        )
        self._state = self._load_state()
        self._primed = False
        self.last_run = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="webmanager-watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if self._stop.wait(45):  # let the sites finish starting first
            return
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the watchdog must outlive its own bugs
                self.app.logger.exception("Watchdog round failed")
            if self._stop.wait(INTERVAL_SECONDS):
                return

    # -- remembered announcements ---------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        try:
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            pass

    def _notify(self, title, text, level="info"):
        notifier = self.app.extensions.get("notifier")
        if notifier is not None:
            notifier.send(title, text, level)

    @property
    def _is_replica(self) -> bool:
        manager = self.app.extensions.get("replication_manager")
        return bool(manager is not None and manager.is_replica)

    # -- one round -------------------------------------------------------------
    def run_once(self):
        with self.app.app_context():
            database = get_db()
            runtime = self.app.extensions["runtime_manager"]
            if runtime.nginx_binary:
                self._watch_sites(database, runtime)
            if not self._is_replica:
                self._watch_sources(database)
            self._watch_program_update()
            self._watch_disk()
            self._watch_failover()
        self._primed = True
        self._save_state()
        self.last_run = time.time()

    def _watch_sites(self, database, runtime):
        gateway_port = self.app.config["SITE_GATEWAY_PORT"]
        rows = database.execute("SELECT * FROM sites WHERE status = 'running'").fetchall()
        sites = [(row, site_hostnames(database, row)) for row in rows]
        sites = [(row, names) for row, names in sites if names]
        if not sites:
            self._sites.clear()
            return
        if not _port_open(gateway_port):
            # The managed Nginx is gone (killed, crashed, out of memory).
            self.app.logger.warning("Site web server is not answering; starting it again.")
            runtime.restore_sites(include_apps=not self._is_replica)
            runtime.restore_gateway()
            time.sleep(1.5)
            back = _port_open(gateway_port)
            activity.record_system(
                self.app, "Restarted the site web server",
                "It had stopped answering." + ("" if back else " It is still down."), ok=back,
            )
            self._notify(
                "Site web server restarted" if back else "Site web server is down",
                "The web server in front of your sites had stopped. "
                + ("WebManager started it again." if back else "Starting it again did not work; run sudo webmanager-doctor on the server."),
                "warning" if back else "error",
            )
            if not back:
                return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        tallies = []
        seen = set()
        for row, names in sites:
            seen.add(row["id"])
            code = health.probe_site(gateway_port, names[0])
            answered = code is not None and code < 500
            tallies.append((row["id"], day, 0 if answered else 1))
            state = self._sites.setdefault(row["id"], {"fails": 0, "down": False})
            state["name"] = row["name"]
            if answered:
                if state["down"]:
                    activity.record_system(self.app, "Site is answering again", row["name"])
                    self._notify(f"{row['name']} is back", f"{names[0]} is answering again.", "ok")
                state.update(fails=0, down=False)
                continue
            state["fails"] += 1
            if state["fails"] >= FAILURES_BEFORE_DOWN and not state["down"]:
                state["down"] = True
                problem = "no answer" if code is None else f"error {code}"
                activity.record_system(self.app, "Site stopped answering", f"{row['name']} ({problem})", ok=False)
                self._notify(
                    f"{row['name']} is down",
                    f"{names[0]} is not answering ({problem}). Open its page in WebManager for the reason.",
                    "error",
                )
                if row["kind"] == "app" and not self._is_replica:
                    # Starts the container again if it is gone; a no-op if it runs.
                    runtime.start_app_async(row["id"])
        for site_id in list(self._sites):
            if site_id not in seen:
                del self._sites[site_id]
        if not self._is_replica:
            database.executemany(
                """
                INSERT INTO site_uptime (site_id, day, checks, failures) VALUES (?, ?, 1, ?)
                ON CONFLICT(site_id, day) DO UPDATE
                SET checks = checks + 1, failures = failures + excluded.failures
                """,
                tallies,
            )
            database.execute(
                "DELETE FROM site_uptime WHERE day < date('now', ?)", (f"-{UPTIME_KEEP_DAYS} days",)
            )
            database.commit()

    def _watch_sources(self, database):
        current = {}
        for row in database.execute(
            "SELECT id, name, update_state, update_error, pending_commit FROM repositories"
        ).fetchall():
            if row["update_state"] == "failed":
                current[f"{row['id']}:failed:{row['update_error']}"] = (
                    f"Update check failed for {row['name']}", row["update_error"] or "No details were recorded.", "error",
                )
            elif row["pending_commit"]:
                current[f"{row['id']}:pending:{row['pending_commit']}"] = (
                    f"An update for {row['name']} is waiting",
                    f"Revision {row['pending_commit'][:12]} passed its checks. Approve it under Sources.", "info",
                )
        known = set(self._state.get("sources", []))
        if self._primed or "sources" in self._state:
            for key, (title, text, level) in current.items():
                if key not in known:
                    self._notify(title, text, level)
        self._state["sources"] = sorted(current)

    def _watch_program_update(self):
        status = update_status.read_update_status()
        state = status.get("state")
        installed = status.get("installed_commit")
        key = f"{state}:{status.get('available_commit')}" if state == "error" else ""
        if key and key != self._state.get("update_error"):
            if "update_error" in self._state:
                self._notify("WebManager update failed", status.get("message") or "See System in WebManager.", "error")
        self._state["update_error"] = key
        previous = self._state.get("installed")
        if installed and previous and installed != previous:
            activity.record_system(self.app, "WebManager was updated", f"Now running {installed[:12]}.")
            self._notify("WebManager was updated", f"Now running {installed[:12]}.", "ok")
        if installed:
            self._state["installed"] = installed

    def _watch_failover(self):
        """Tell people when the group fails over (same as the page warning)."""
        situation = failover.state(self.app)
        current = f"{situation['kind']}:{situation['key']}" if situation else ""
        previous = self._state.get("failover", "")
        self._state["failover"] = current
        if current == previous or not current and not previous:
            return
        host = socket.gethostname()
        if not situation:
            if previous.startswith("primary_down:"):
                self._notify("The primary is answering again", f"{host} is syncing from it again.", "ok")
            return
        if situation["kind"] == "promoted":
            self._notify(
                f"Failed over: {host} is now the primary",
                f"It took over from {situation['from'] or 'the previous primary'}. Open WebManager on {host}: "
                "the warning there lists what to check (keep the old primary off, DNS, apps, recent changes).",
                "error",
            )
        elif situation["kind"] == "primary_down":
            self._notify(
                "The primary server isn't answering",
                f"{host} can't reach {situation['primary']}: {situation['error']} It keeps serving sites, "
                "but changes can't be saved. If the primary is gone for good, promote this server.",
                "error",
            )
        else:
            self._notify(
                "Two servers are acting as the primary",
                f"{host} and {situation['other_name']} ({situation['other']}) both accept changes. "
                "Join one to the other as a replica.",
                "error",
            )

    def _watch_disk(self):
        try:
            usage = shutil.disk_usage(self.app.instance_path)
        except OSError:
            return
        free_percent = 100 * usage.free / usage.total if usage.total else 100
        now = time.time()
        if free_percent >= DISK_WARN_PERCENT:
            self._state.pop("disk_warned_at", None)
            return
        if now - float(self._state.get("disk_warned_at", 0)) < DISK_REMIND_SECONDS:
            return
        self._state["disk_warned_at"] = now
        self._notify(
            "Disk is almost full",
            f"Only {free_percent:.0f}% ({usage.free // (1024 * 1024)} MB) is free where WebManager keeps its data. "
            "Updates, backups and deploys will start failing.",
            "warning",
        )


def uptime_summary(database, site_id: int, days: int = 30):
    """{"percent", "checks", "failures", "days"} for a site, or None before the
    first check."""
    row = database.execute(
        """
        SELECT COALESCE(SUM(checks), 0) AS checks, COALESCE(SUM(failures), 0) AS failures,
               COUNT(*) AS days
        FROM site_uptime WHERE site_id = ? AND day >= date('now', ?)
        """,
        (site_id, f"-{days} days"),
    ).fetchone()
    if not row["checks"]:
        return None
    return {
        "percent": round(100 * (row["checks"] - row["failures"]) / row["checks"], 2),
        "checks": row["checks"],
        "failures": row["failures"],
        "days": row["days"],
    }
