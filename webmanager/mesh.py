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
import json
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, current_app, jsonify, request

from . import system_metrics
from .db import get_db
from .update_status import read_update_status

POLL_SECONDS = 20
TIMEOUT_SECONDS = 10
MAX_FAILED_ATTEMPTS = 10
FAILED_ATTEMPT_WINDOW_SECONDS = 60
# Bound on tracked failing IPs before a sweep drops expired ones, so a flood
# of one-off bad-token attempts from many source addresses can't grow this
# dict without limit.
MAX_TRACKED_FAILURES = 1000

bp = Blueprint("mesh", __name__)


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


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
    metrics = system_metrics.collect(app)
    installed_commit = read_update_status().get("installed_commit")
    return {
        "hostname": metrics.get("hostname"),
        "version": installed_commit[:12] if installed_commit else None,
        "sites": {"total": sites["total"], "running": sites["running"] or 0},
        "apps_enabled": apps_enabled,
        "apps": apps,
        "cpu_percent": metrics.get("cpu_percent"),
        "memory_percent": (metrics.get("memory") or {}).get("percent"),
        "disk_percent": (metrics.get("disk") or {}).get("percent"),
    }


class MeshHub:
    """Polls sibling servers and authenticates their polls of us."""

    def __init__(self, app, urls: list[str], token: str):
        self.app = app
        self.urls = list(dict.fromkeys(url for url in urls if url))
        self.token = token
        self._token_hash = _hash_token(token) if token else None
        self._remote: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._fails: dict[str, dict] = {}
        self._fails_lock = threading.Lock()

    def start(self):
        if not self.urls or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="webmanager-mesh", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self):
        # Stagger the first round so a restart doesn't burst-poll every peer
        # at once; subsequent rounds poll all peers back-to-back every 20s.
        for index, url in enumerate(self.urls):
            if self._stop_event.wait(1 + index * 0.5):
                return
            self._poll(url)
        while not self._stop_event.wait(POLL_SECONDS):
            for url in self.urls:
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
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured peer URL
                body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Peer returned an unexpected response.")
            with self._lock:
                self._remote[url] = {"reachable": True, "error": None, "checked_at": time.time(), "data": body}
            if was_reachable is False:
                self.app.logger.info("Mesh peer %s is reachable again.", url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            message = str(getattr(exc, "reason", None) or exc)
            with self._lock:
                previous = self._remote.get(url, {})
                self._remote[url] = {
                    "reachable": False,
                    "error": message,
                    "checked_at": time.time(),
                    # Keep the last known data so the panel can still show
                    # "last seen" figures for a peer that's gone offline.
                    "data": previous.get("data"),
                }
            if was_reachable is not False:
                self.app.logger.warning("Mesh peer %s is unreachable: %s", url, message)

    def entries(self) -> list[dict]:
        """One entry per configured peer, for the admin panel."""
        with self._lock:
            snapshot = {url: dict(value) for url, value in self._remote.items()}
        results = []
        for url in self.urls:
            remote = snapshot.get(url, {})
            data = remote.get("data") or {}
            results.append(
                {
                    "url": url,
                    "hostname": data.get("hostname") or url.replace("https://", "").replace("http://", ""),
                    "reachable": bool(remote.get("reachable")),
                    "error": remote.get("error"),
                    "checked_at": remote.get("checked_at"),
                    "version": data.get("version"),
                    "sites": data.get("sites"),
                    "apps_enabled": data.get("apps_enabled"),
                    "apps": data.get("apps"),
                    "cpu_percent": data.get("cpu_percent"),
                    "memory_percent": data.get("memory_percent"),
                    "disk_percent": data.get("disk_percent"),
                }
            )
        return results

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
