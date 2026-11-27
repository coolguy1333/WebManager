"""Mesh federation: WebManager servers keeping track of each other.

There is no leader, no shared database, and no distributed deployment -
each server hosts and updates its own sites/apps exactly as it already did.
The mesh only adds visibility: every server independently polls a configured
list of sibling servers' ``/mesh/status`` endpoint and shows the combined
result (who's up, how busy) on the System page. One server being down never
takes the others' visibility into the group down with it.

This mirrors the peer-federation design in coolguy1333/Uptime-Monitor
(``lib/peers.js``): a static, mutually-configured peer list plus a shared
bearer token, polled on a fixed interval, with only aggregate counts
exposed - never site names, hostnames, or repository details.
"""

import atexit
import hashlib
import hmac
import http.client
import ipaddress
import re
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from . import peer_http, system_metrics
from .db import get_db
from . import server_control
from .update_status import (
    COMMIT_RE,
    IN_PROGRESS_STATES,
    auto_install_enabled,
    live_checks_enabled,
    read_update_status,
    request_program_update,
    request_program_update_check,
    run_live_check,
    set_auto_install,
    updater_active,
)

POLL_SECONDS = 20
TIMEOUT_SECONDS = 10
# Asking a server to update waits for its quick check of GitHub (up to 15s).
UPDATE_REQUEST_TIMEOUT_SECONDS = 25
# Its updater starts within seconds of being asked; past this, trust its own state.
UPDATE_PENDING_SECONDS = 90
# The token guards the session-signing key and full database/data snapshots,
# so a short, guessable one is refused rather than trusted.
MIN_TOKEN_LENGTH = 16
MAX_FAILED_ATTEMPTS = 10
FAILED_ATTEMPT_WINDOW_SECONDS = 60
# Bound on tracked failing IPs before a sweep drops expired ones, so a flood
# of one-off bad-token attempts from many source addresses can't grow this
# dict without limit.
MAX_TRACKED_FAILURES = 1000

bp = Blueprint("mesh", __name__)


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


# Sharing can be switched on from the System page instead of by editing
# /etc/webmanager/webmanager.env: the token and the servers added there live in
# the data folder (the web service can't write its own settings file).
def _saved_path(app, name: str) -> Path:
    return Path(app.config.get("MESH_STATE_DIR") or app.instance_path) / name


def load_saved_token(app) -> str:
    try:
        token = _saved_path(app, "peer-token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token if len(token) >= MIN_TOKEN_LENGTH else ""


def save_token(app, token: str):
    path = _saved_path(app, "peer-token")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")


_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")


def normalize_peer_url(value) -> str | None:
    """The bare address (scheme://host[:port]) of a server, or None if it isn't one."""
    text = str(value or "").strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text.rstrip("/"))
        port = parts.port
    except ValueError:  # e.g. a bracketed host that isn't an IPv6 address
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        return None
    hostname = parts.hostname
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return None
        host = f"[{hostname}]"
    elif _HOSTNAME_RE.fullmatch(hostname):
        host = hostname
    else:
        return None  # spaces, underscores, other characters no address has
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def load_saved_peers(app) -> list[str]:
    try:
        data = json.loads(_saved_path(app, "peers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    urls = [normalize_peer_url(item) for item in data] if isinstance(data, list) else []
    return list(dict.fromkeys(url for url in urls if url))


def save_peers(app, urls: list[str]):
    path = _saved_path(app, "peers.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(urls)) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def local_status(app) -> dict:
    """What this server reports about itself to a polling peer. Aggregate
    counts only - no site names, hostnames, or repository details."""
    database = get_db()
    sites = database.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
        FROM sites WHERE kind != 'app'
        """
    ).fetchone()
    apps_enabled = bool(app.config.get("APPS_ENABLED"))
    apps = None
    if apps_enabled:
        row = database.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
            FROM sites WHERE kind = 'app'
            """
        ).fetchone()
        apps = {"total": row["total"], "running": row["running"] or 0}
    metrics = system_metrics.collect_basic(app)
    update = read_update_status()
    installed_commit = update.get("installed_commit")
    from .replication import server_role  # replication imports this module's helpers

    return {
        **server_role(app),
        "hostname": metrics.get("hostname"),
        "version": installed_commit[:12] if installed_commit else None,
        # "current", "available", "testing", "backing_up", "installing",
        # "error"...: lets another server show "Updating…" or "failed".
        "update_state": update.get("state"),
        # How many peers this server sees updating right now (a count: this
        # answer can be public). A primary's updater waits for them before
        # installing by itself (deploy/debian/update.sh).
        "peers_updating": _peers_updating(app),
        "auto_install": auto_install_enabled(),
        **_apps_change(),
        "sites": {"total": sites["total"], "running": sites["running"] or 0},
        "apps_enabled": apps_enabled,
        "apps": apps,
        "cpu_percent": metrics.get("cpu_percent"),
        "memory_percent": (metrics.get("memory") or {}).get("percent"),
        "disk_percent": (metrics.get("disk") or {}).get("percent"),
    }


def _peers_updating(app) -> int:
    hub = app.extensions.get("mesh_hub")
    if hub is None:
        return 0
    return sum(
        1 for entry in hub.entries()
        if entry["reachable"] and entry.get("update_state") in IN_PROGRESS_STATES
    )


def _apps_change() -> dict:
    """A change to app hosting under way here, or one that failed: lets the
    server that asked for it show "Turning on…" or why it didn't work."""
    status = server_control.read_status()
    if not status or status["state"] == "done":
        return {"apps_change": None, "apps_change_message": None}
    return {
        "apps_change": status["state"],
        "apps_change_message": status["message"] if status["state"] == "error" else None,
    }


explain_failure = peer_http.explain_failure


class MeshHub:
    """Polls sibling servers and authenticates their polls of us."""

    def __init__(self, app, urls: list[str], token: str):
        self.app = app
        self.urls = list(dict.fromkeys(url for url in urls if url))
        # Servers added on the System page (removable there); the rest come
        # from WEBMANAGER_PEERS / WEBMANAGER_REPLICA_OF.
        self.saved_urls: set[str] = set()
        self.token = token
        self._token_hash = _hash_token(token) if token else None
        self._remote: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._fails: dict[str, dict] = {}
        # url -> (when an update was asked for, the version it ran then)
        self._update_requested: dict[str, tuple[float, str | None]] = {}
        self._fails_lock = threading.Lock()
        self._named: set[str] = set()  # replicas whose name is already recorded
        self.running = False  # set by start(): False in tests, so nothing polls

    def start(self):
        self.running = True
        if not self.urls or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="webmanager-mesh", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def set_token(self, token: str):
        self.token = token
        self._token_hash = _hash_token(token) if token else None

    def add_saved_url(self, url: str, persist: bool = True):
        """Start tracking a server added on the System page (or that registered itself)."""
        with self._lock:
            if url not in self.urls:
                self.urls.append(url)
            self.saved_urls.add(url)
            if persist:
                save_peers(self.app, list(self.saved_urls))
        if self.running:
            threading.Thread(target=self._poll, args=(url,), daemon=True).start()
            self.start()

    def remove_saved_url(self, url: str) -> bool:
        with self._lock:
            if url not in self.saved_urls:
                return False
            self.saved_urls.discard(url)
            if url in self.urls:
                self.urls.remove(url)
            self._remote.pop(url, None)
            save_peers(self.app, list(self.saved_urls))
        return True

    def probe(self, url: str) -> tuple[bool, str | None, str | None]:
        """Ask a server for its status with our token: (answers, error, hint)."""
        req = urllib.request.Request(f"{url}/mesh/status", headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with peer_http.open_peer(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - admin/peer supplied URL, no redirects
                body = json.loads(response.read(65536).decode("utf-8"))
            if not isinstance(body, dict) or "sites" not in body:
                return False, "It answered, but not like a WebManager server.", None
            return True, None, None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError, OSError) as exc:
            message, hint = explain_failure(exc, TIMEOUT_SECONDS)
            return False, message, hint

    def _post(self, url: str, path: str, payload: dict) -> tuple[bool, str, dict]:
        """POST JSON to another server's token-only endpoint:
        (accepted, message for the admin, its answer)."""
        req = urllib.request.Request(
            f"{url}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        try:
            with peer_http.open_peer(req, timeout=UPDATE_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - listed peer URL
                body = json.loads(response.read(65536).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read(65536).decode("utf-8"))
            except (ValueError, OSError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            if exc.code == 404 and not body.get("error"):
                return False, (
                    "It runs a WebManager too old to be managed from here. "
                    "Update it once on that server (its System page, or git pull && sudo bash setup.sh)."
                ), body
            message, hint = explain_failure(exc, UPDATE_REQUEST_TIMEOUT_SECONDS)
            return False, body.get("error") or hint or message, body
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError, OSError) as exc:
            message, hint = explain_failure(exc, UPDATE_REQUEST_TIMEOUT_SECONDS)
            return False, f"It could not be reached: {hint or message}", {}
        if not isinstance(body, dict):
            return False, "It answered, but not like a WebManager server.", {}
        # Show the new state straight away rather than after the next poll.
        threading.Thread(target=self._poll, args=(url,), daemon=True).start()
        return bool(body.get("ok")), str(body.get("message") or body.get("error") or ""), body

    def request_update(self, url: str, commit: str) -> tuple[bool, str]:
        """Ask another server to install WebManager version ``commit`` (the
        one this server runs). (accepted, message for the admin)."""
        with self._lock:
            version = ((self._remote.get(url) or {}).get("data") or {}).get("version")
        accepted, message, body = self._post(url, "/mesh/update", {"commit": commit})
        if accepted and body.get("state") == "requested":
            with self._lock:
                self._update_requested[url] = (time.time(), version)
        return accepted, message

    def control(self, url: str, action: str) -> tuple[bool, str]:
        """Ask another server to make one of CONTROL_ACTIONS changes."""
        accepted, message, _body = self._post(url, "/mesh/control", {"action": action})
        return accepted, message

    def _update_state(self, url: str, data: dict):
        """What the other server says about its update, or "requested" from
        the moment it was asked until its updater starts (a few seconds) and
        the version or state shows the outcome."""
        state = data.get("update_state")
        requested = self._update_requested.get(url)
        if requested is None:
            return state
        asked_at, version_then = requested
        if (
            time.time() - asked_at > UPDATE_PENDING_SECONDS
            or data.get("version") != version_then
            or state in IN_PROGRESS_STATES
            or state == "error"
        ):
            self._update_requested.pop(url, None)
            return state
        return "requested"

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self):
        # Stagger the first round so a restart doesn't burst-poll every peer
        # at once; subsequent rounds poll all peers back-to-back every 20s.
        for index, url in enumerate(list(self.urls)):
            if self._stop_event.wait(1 + index * 0.5):
                return
            self._poll(url)
        while not self._stop_event.wait(POLL_SECONDS):
            for url in list(self.urls):
                if self._stop_event.is_set():
                    return
                self._poll(url)

    def _poll(self, url):
        req = urllib.request.Request(f"{url}/mesh/status", headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        was_reachable = None
        with self._lock:
            previous = self._remote.get(url)
            if previous is not None:
                was_reachable = previous.get("reachable")
        try:
            with peer_http.open_peer(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured peer URL
                body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Peer returned an unexpected response.")
            now = time.time()
            with self._lock:
                self._remote[url] = {
                    "reachable": True, "error": None, "hint": None,
                    "checked_at": now, "last_ok_at": now, "data": body,
                }
            if body.get("role") == "replica":
                self._name_replica(url)
            if was_reachable is False:
                self.app.logger.info("Mesh peer %s is reachable again.", url)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError, OSError) as exc:
            message, hint = explain_failure(exc, TIMEOUT_SECONDS)
            with self._lock:
                previous = self._remote.get(url, {})
                self._remote[url] = {
                    "reachable": False,
                    "error": message,
                    "hint": hint,
                    "checked_at": time.time(),
                    "last_ok_at": previous.get("last_ok_at"),
                    # Keep the last known data so the panel can still show
                    # "last seen" figures for a peer that's gone offline.
                    "data": previous.get("data"),
                }
            if was_reachable is not False:
                self.app.logger.warning("Mesh peer %s is unreachable: %s (%s)", url, message, hint)

    def _name_replica(self, url):
        """Give a replica its public name (replicaN.<dashboard domain>) the
        first time this primary sees it, and start passing that name to it."""
        if self.app is None or url in self._named:
            return
        manager = self.app.extensions.get("replication_manager")
        if manager is not None and manager.is_replica:
            return  # names are handed out by the primary; this copy is mirrored
        from . import server_aliases

        try:
            with self.app.app_context():
                database = get_db()
                number, new = server_aliases.assign(database, url)
                self._named.add(url)
                if new and server_aliases.base_domain(database):
                    self.app.logger.info("%s is now replica%s.", url, number)
                    runtime = self.app.extensions.get("runtime_manager")
                    if runtime is not None and runtime.nginx_binary:
                        runtime.apply_nginx_configs()
        except Exception:  # noqa: BLE001 - naming must never break polling
            self.app.logger.exception("Could not give %s its replica name", url)

    def _last_seen(self, epoch):
        """"3 minutes ago" for the last successful poll, or None if never."""
        if not epoch:
            return None
        moment = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))
        try:
            return self.app.jinja_env.filters["ago"](moment)
        except Exception:  # never let a label break the panel
            return moment

    def entries(self) -> list[dict]:
        """One entry per configured peer, for the admin panel."""
        with self._lock:
            snapshot = {url: dict(value) for url, value in self._remote.items()}
            urls = list(self.urls)
            saved = set(self.saved_urls)
            update_states = {
                url: self._update_state(url, (snapshot.get(url) or {}).get("data") or {}) for url in urls
            }
        results = []
        for url in urls:
            remote = snapshot.get(url, {})
            data = remote.get("data") or {}
            results.append(
                {
                    "url": url,
                    "removable": url in saved,
                    "hostname": data.get("hostname") or url.replace("https://", "").replace("http://", ""),
                    "reachable": bool(remote.get("reachable")),
                    "error": remote.get("error"),
                    "hint": remote.get("hint"),
                    "checked_at": remote.get("checked_at"),
                    "last_seen": self._last_seen(remote.get("last_ok_at")),
                    "version": data.get("version"),
                    "update_state": update_states.get(url),
                    "role": data.get("role"),
                    "promoted_at": data.get("promoted_at"),
                    "auto_install": data.get("auto_install"),
                    "apps_change": data.get("apps_change"),
                    "apps_change_message": data.get("apps_change_message"),
                    "sites": data.get("sites"),
                    "apps_enabled": data.get("apps_enabled"),
                    "apps": data.get("apps"),
                    "cpu_percent": data.get("cpu_percent"),
                    "memory_percent": data.get("memory_percent"),
                    "disk_percent": data.get("disk_percent"),
                }
            )
        return results

    def authorize_sensitive(self, authorization_header: str, ip: str) -> bool:
        """Like authorize_incoming(), but for endpoints that must never be
        public: the secret key, a full database snapshot, site/app data.
        Refuses outright with no token configured, instead of allowing
        public access the way the plain status endpoint does."""
        if self._token_hash is None or len(self.token) < MIN_TOKEN_LENGTH:
            return False
        return self.authorize_incoming(authorization_header, ip)

    def authorize_incoming(self, authorization_header: str, ip: str) -> bool:
        """Authenticates an inbound GET /mesh/status. With no token configured
        the endpoint is public (like /healthz) and this always returns True.
        Only failed attempts are rate-limited, so a peer with the right token
        is never throttled."""
        if self._token_hash is None:
            return True
        now = time.time()
        with self._fails_lock:
            if len(self._fails) > MAX_TRACKED_FAILURES:
                self._fails = {ip_: a for ip_, a in self._fails.items() if a["reset"] >= now}
            attempt = self._fails.get(ip)
            if attempt and attempt["reset"] > now and attempt["count"] > MAX_FAILED_ATTEMPTS:
                return False
        provided = authorization_header[7:] if authorization_header.startswith("Bearer ") else ""
        ok = hmac.compare_digest(_hash_token(provided), self._token_hash)
        if not ok:
            with self._fails_lock:
                attempt = self._fails.get(ip)
                if not attempt or attempt["reset"] < now:
                    self._fails[ip] = {"count": 1, "reset": now + FAILED_ATTEMPT_WINDOW_SECONDS}
                else:
                    attempt["count"] += 1
        return ok


@bp.get("/mesh/status")
def mesh_status():
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_incoming(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    response = jsonify(local_status(current_app))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/mesh/register")
def mesh_register():
    """A server that was just set up against this one announces its own
    address, so it appears in the Servers panel without anyone typing it in.
    Needs the shared token (the same trust as everything else in the group),
    and the address must answer with that token before it is kept."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_sensitive(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    payload = request.get_json(silent=True)
    url = normalize_peer_url(payload.get("url") if isinstance(payload, dict) else None)
    if url is None:
        return jsonify({"error": "Send {\"url\": \"http://this-server:8080\"}."}), 400
    ok, error, hint = hub.probe(url)
    if not ok:
        return jsonify({"error": f"{url} did not answer: {error}", "hint": hint}), 400
    hub.add_saved_url(url)
    return jsonify({"ok": True, "url": url})


@bp.post("/mesh/update")
def mesh_update():
    """Another server in the group asks this one to install a WebManager
    version: the one it runs itself, so the two match. Token only.

    This only files the same approval the System page's "Install" button
    does. The root updater still installs nothing but the newest version of
    this server's own update branch, after testing it and backing up, and
    rolls back if it fails, so a caller can't pick arbitrary code."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_sensitive(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    payload = request.get_json(silent=True)
    commit = payload.get("commit") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        return jsonify({"error": "Send {\"commit\": \"<40-character commit>\"}."}), 400
    status = read_update_status()
    if status.get("installed_commit") == commit:
        return jsonify({"ok": True, "state": "current", "message": "It already runs that version."})
    if status.get("state") in IN_PROGRESS_STATES:
        return jsonify({"ok": True, "state": status["state"], "message": "It is already installing an update."})
    if live_checks_enabled():
        # The updater only installs the newest version of this server's update
        # branch; say now (not a minute later on its System page) if that isn't it.
        latest = run_live_check(current_app._get_current_object(), wait=True, timeout=15)
        newest = latest.get("available_commit")
        if newest and newest != commit:
            if not latest.get("update_available"):
                return jsonify(
                    {
                        "error": (
                            f"It already runs the newest version of its update branch ({newest[:12]}), "
                            f"not {commit[:12]}. Update this server instead, or make both follow the same "
                            "update branch (WEBMANAGER_UPDATE_BRANCH)."
                        )
                    }
                ), 409
            return jsonify(
                {
                    "error": (
                        f"The newest version on its update branch is {newest[:12]}, not {commit[:12]}. "
                        "Update this server first, or make both follow the same update branch "
                        "(WEBMANAGER_UPDATE_BRANCH)."
                    )
                }
            ), 409
    if updater_active() is False:
        return jsonify(
            {
                "error": (
                    "Its updater service is switched off. On that server run: sudo systemctl enable "
                    "--now webmanager-update.path webmanager-update.timer (or re-run setup.sh)."
                )
            }
        ), 409
    try:
        request_program_update(commit)
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"It could not file the update request: {exc}"}), 500
    current_app.logger.info(
        "WebManager update to %s requested by another server (%s).", commit[:12], request.remote_addr
    )
    return jsonify(
        {
            "ok": True,
            "state": "requested",
            "message": "It is testing, backing up and installing the update now (a minute or two).",
        }
    ), 202


CONTROL_ACTIONS = ("apps_on", "apps_off", "auto_updates_on", "auto_updates_off", "sync_now")


@bp.post("/mesh/control")
def mesh_control():
    """Another server in the group (its super admin, on its Servers list)
    changes a setting of this one, so a replica never needs its own sign-in or
    a shell for these. Token only, and only the CONTROL_ACTIONS below; app
    hosting goes through the same root helper as this server's System page."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_sensitive(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    payload = request.get_json(silent=True)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in CONTROL_ACTIONS:
        return jsonify({"error": f"Unknown action. Use one of: {', '.join(CONTROL_ACTIONS)}."}), 400
    current_app.logger.info("Change '%s' requested by another server (%s).", action, request.remote_addr)

    if action in ("apps_on", "apps_off"):
        enable = action == "apps_on"
        if bool(current_app.config.get("APPS_ENABLED")) == enable:
            return jsonify({"ok": True, "message": f"App hosting is already {'on' if enable else 'off'} there."})
        reason = server_control.blocked_reason()
        if reason:
            return jsonify({"error": reason}), 409
        try:
            server_control.request_app_hosting(enable)
        except OSError as exc:
            return jsonify({"error": f"It could not file the request: {exc}"}), 500
        return jsonify({
            "ok": True,
            "message": (
                "It is turning app hosting on: installing Docker if needed, then restarting "
                "WebManager (a minute or two)." if enable
                else "It is turning app hosting off and restarting WebManager."
            ),
        }), 202

    if action in ("auto_updates_on", "auto_updates_off"):
        enable = action == "auto_updates_on"
        if enable and updater_active() is False:
            return jsonify({"error": "Its updater service is switched off. Re-run setup.sh on that server."}), 409
        try:
            set_auto_install(enable)
            if enable:
                request_program_update_check()
        except OSError as exc:
            return jsonify({"error": f"It could not change the setting: {exc}"}), 500
        return jsonify({
            "ok": True,
            "message": "It now installs new WebManager versions by itself." if enable
            else "It now waits for approval before installing a WebManager version.",
        })

    # sync_now
    manager = current_app.extensions["replication_manager"]
    if not manager.is_replica:
        return jsonify({"error": "It isn't a replica, so it has nothing to sync."}), 409
    if not manager.sync_once():
        return jsonify({"error": f"It could not sync: {manager.last_error}"}), 502
    data_manager = current_app.extensions["data_replication_manager"]
    threading.Thread(target=data_manager.sync_once, name="webmanager-data-sync-now", daemon=True).start()
    return jsonify({"ok": True, "message": "Its settings are synced; its site and app data is syncing now."})


@bp.get("/mesh/join-info")
def mesh_join_info():
    """What a server joining this one as a replica needs to match it: where
    sites live and whether their public addresses are https. Token only."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_sensitive(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    base_domain = current_app.config.get("SITE_BASE_DOMAIN") or ""
    if not base_domain:
        row = get_db().execute("SELECT name FROM domains WHERE is_default = 1").fetchone()
        base_domain = row["name"] if row else ""
    response = jsonify(
        {
            "site_base_domain": base_domain,
            "site_public_scheme": current_app.config.get("SITE_PUBLIC_SCHEME") or "http",
            "apps_enabled": bool(current_app.config.get("APPS_ENABLED")),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/mesh/secret-key")
def mesh_secret_key():
    """Lets a replica fetch this server's session-signing key so cookies
    validate on every node in the mesh. Unlike /mesh/status this is never
    public: it always requires WEBMANAGER_PEER_TOKEN, on both ends."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None:
        return jsonify({"error": "Mesh federation is not available."}), 404
    if not hub.authorize_sensitive(request.headers.get("Authorization", ""), request.remote_addr or ""):
        return jsonify({"error": "Invalid or missing peer token"}), 401
    response = current_app.response_class(current_app.config["SECRET_KEY"], mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    return response
