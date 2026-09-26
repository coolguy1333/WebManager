"""Primary/replica config replication and write forwarding.

Unlike the plain mesh (webmanager/mesh.py, visibility only), this makes a
replica an actual mirror of a primary server's database - users, teams,
permissions, domains, and site/app definitions - so every server in the
group has the same config. There is exactly one writer at all times (the
primary): a replica never writes its database directly. Instead:

- It pulls a consistent snapshot of the primary's database on an interval
  and atomically swaps it in (webmanager/db.py opens a fresh connection
  per request, so this is safe to do between requests).
- Any state-changing request it receives (a POST/PUT/DELETE from someone
  using the replica's own address) is transparently forwarded to the
  primary and the response relayed back, so every function keeps working
  identically regardless of which node you're on. This requires every
  node to share the same SECRET_KEY, which a replica fetches from its
  primary at startup (see fetch_secret_key()) - Flask's session cookie is
  signed with it, so a session created on one node validates on every
  other node once they share it.

Site/app *data* (Git checkouts, each app's own /data volume) is replicated
separately - see webmanager/data_replication.py.
"""

import atexit
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, Response, current_app, g, jsonify, request

DB_POLL_SECONDS = 15
TIMEOUT_SECONDS = 30
# Headers that must not be copied verbatim between the original request/
# response and the forwarded one - either because they're connection-
# specific (hop-by-hop) or because the receiving end recomputes them.
_DO_NOT_FORWARD_REQUEST_HEADERS = {"host", "content-length", "connection"}
_DO_NOT_FORWARD_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}
# Endpoints that manage the replica itself (not the mirrored config) and so
# must always run locally, never be forwarded to the primary.
_LOCAL_ONLY_ENDPOINTS = {"admin.sync_replication"}

bp = Blueprint("replication", __name__)


class ReplicationError(RuntimeError):
    pass


def fetch_secret_key(primary_url: str, token: str, timeout: float = 10) -> str:
    """Fetch the primary's SECRET_KEY so this replica's sessions validate on
    every node in the mesh. Raises ReplicationError on any failure."""
    req = urllib.request.Request(
        f"{primary_url.rstrip('/')}/mesh/secret-key",
        headers={"Accept": "text/plain", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - configured primary URL
            key = response.read().decode("utf-8").strip()
    except (urllib.error.URLError, OSError) as exc:
        raise ReplicationError(f"Could not fetch the secret key from {primary_url}: {exc}") from exc
    if not key:
        raise ReplicationError(f"{primary_url} returned an empty secret key.")
    return key


def _validate_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ReplicationError("Downloaded database snapshot failed its integrity check.")
        connection.execute("SELECT COUNT(*) FROM users").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ReplicationError(f"Downloaded database snapshot is not a valid database: {exc}") from exc
    finally:
        connection.close()


class ReplicationManager:
    """On a replica, periodically mirrors the primary's database. On a
    primary (primary_url is empty), this is inert other than serving the
    snapshot endpoint other servers pull from."""

    def __init__(self, app, primary_url: str, token: str):
        self.app = app
        self.primary_url = primary_url.rstrip("/") if primary_url else ""
        self.token = token
        self._stop_event = threading.Event()
        self._thread = None
        self.last_sync_at = None
        self.last_error = None

    @property
    def is_replica(self) -> bool:
        return bool(self.primary_url)

    def start(self):
        if not self.is_replica or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="webmanager-replication", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run(self):
        self.sync_once()
        while not self._stop_event.wait(DB_POLL_SECONDS):
            self.sync_once()

    def sync_once(self):
        """Pull and apply one database snapshot from the primary. Public so
        an admin action ("sync now") can trigger it on demand too."""
        try:
            self._pull_db_snapshot()
        except (ReplicationError, OSError) as exc:
            self.last_error = str(exc)
            self.app.logger.warning("Replication: could not sync from primary: %s", exc)
            return False
        self.last_error = None
        self.last_sync_at = time.time()
        return True

    def _pull_db_snapshot(self):
        database_path = Path(self.app.config["DATABASE"])
        req = urllib.request.Request(
            f"{self.primary_url}/replication/db-snapshot",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=database_path.parent, prefix=".db-sync-", suffix=".sqlite3"
        )
        try:
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
                    if response.status != 200:
                        raise ReplicationError(f"Primary returned HTTP {response.status}.")
                    with os.fdopen(descriptor, "wb") as handle:
                        shutil.copyfileobj(response, handle)
            except urllib.error.URLError as exc:
                raise ReplicationError(f"Could not reach primary {self.primary_url}: {exc}") from exc
            _validate_snapshot(temporary_name)
            os.chmod(temporary_name, 0o640)
            os.replace(temporary_name, database_path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    # -- write forwarding -----------------------------------------------
    def forward_current_request(self):
        """Relay the in-flight Flask request to the primary verbatim
        (method, headers, cookies, body) and return its response as-is, so
        a write attempted on a replica works exactly like it would on the
        primary. Returns a Flask Response, or a 502 if the primary can't be
        reached."""
        target = f"{self.primary_url}{request.path}"
        if request.query_string:
            target += f"?{request.query_string.decode('utf-8')}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _DO_NOT_FORWARD_REQUEST_HEADERS
        }
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        client_ip = request.remote_addr or ""
        headers["X-Forwarded-For"] = f"{forwarded_for}, {client_ip}" if forwarded_for else client_ip
        req = urllib.request.Request(
            target,
            data=request.get_data() or None,
            headers=headers,
            method=request.method,
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
                body = response.read()
                status = response.status
                response_headers = list(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            response_headers = list(exc.headers.items())
        except (urllib.error.URLError, OSError) as exc:
            return jsonify({"error": f"The primary server ({self.primary_url}) could not be reached: {exc}"}), 502
        relayed = Response(body, status=status)
        for key, value in response_headers:
            if key.lower() in _DO_NOT_FORWARD_RESPONSE_HEADERS:
                continue
            relayed.headers.add(key, value)
        return relayed


def register_write_forwarding(app):
    """Any state-changing request landing on a replica is transparently
    forwarded to the primary instead of being handled locally, so every
    admin function works the same regardless of which node you're on."""

    @app.before_request
    def _forward_writes_to_primary():
        manager = app.extensions.get("replication_manager")
        if manager is None or not manager.is_replica:
            return None
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.blueprint in ("mesh", "replication"):
            return None
        if request.endpoint == "static" or request.endpoint in _LOCAL_ONLY_ENDPOINTS:
            return None
        return manager.forward_current_request()


@bp.get("/replication/db-snapshot")
def db_snapshot():
    hub = current_app.extensions.get("mesh_hub")
    if hub is None or not hub.authorize_sensitive(
        request.headers.get("Authorization", ""), request.remote_addr or ""
    ):
        return jsonify({"error": "Invalid or missing peer token"}), 401

    from .db import get_db

    # Closing the request-scoped connection first avoids a needless
    # "database is locked" edge case if SQLite's own file lock is held
    # elsewhere on the same process at the moment of backup.
    g.pop("db", None)
    source = sqlite3.connect(current_app.config["DATABASE"])
    descriptor, temporary_name = tempfile.mkstemp(prefix="wm-db-snapshot-", suffix=".sqlite3")
    os.close(descriptor)
    try:
        destination = sqlite3.connect(temporary_name)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    get_db()  # restore g.db for any after_request handling that expects it

    def stream():
        try:
            with open(temporary_name, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    response = current_app.response_class(stream(), mimetype="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    return response
