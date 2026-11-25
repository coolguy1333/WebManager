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
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, render_template, request, session

from . import db as database_module
from . import peer_http

DB_POLL_SECONDS = 15
# The primary tells replicas where its data directory is, so a replica whose
# own directory differs can rewrite the absolute paths stored in the database.
DATA_DIR_HEADER = "X-WebManager-Data-Dir"
# Absolute-path columns in the mirrored database (see _rebase_paths).
_PATH_COLUMNS = (
    ("repositories", "local_path"),
    ("repositories", "pending_path"),
    ("sites", "document_root"),
    ("sites", "nginx_config"),
)
TIMEOUT_SECONDS = 30
# Headers that must not be copied verbatim between the original request/
# response and the forwarded one - either because they're connection-
# specific (hop-by-hop) or because the receiving end recomputes them.
_DO_NOT_FORWARD_REQUEST_HEADERS = {"host", "content-length", "connection"}
_DO_NOT_FORWARD_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}
# Endpoints that manage the replica itself (not the mirrored config) and so
# must always run locally, never be forwarded to the primary.
_LOCAL_ONLY_ENDPOINTS = {
    "admin.sync_replication",
    "admin.sync_data_replication",
    "admin.promote_replica",
    # WebManager's own program update is a per-machine install (each
    # server updates its own /opt/webmanager checkout), never the shared
    # config - forwarding it would update the primary while telling the
    # admin it happened here.
    "admin.check_program_update",
    "admin.install_program_update",
    "admin.set_program_auto_install",
    # Also about this machine only: its own backups, and its own list of
    # other servers and sharing token (files in its data folder).
    "admin.create_backup",
    "admin.test_notification",
    "admin.enable_server_sharing",
    "admin.add_server",
    "admin.update_server",
    "admin.control_server",
    "admin.acknowledge_failover",
    # Root-level change to this machine (Docker), never the primary's.
    "admin.set_app_hosting",
    "admin.remove_server",
}


def promoted_marker(app) -> Path:
    """Exists once a replica has been promoted to primary."""
    return Path(app.config.get("MESH_STATE_DIR") or app.instance_path) / "promoted"


def server_role(app) -> dict:
    """This server's place in a replica group, for load balancers and
    failover scripts: {"role": "primary" | "replica", "promoted_at": unix
    time this server was promoted, or None if it never was}. A load balancer
    can send traffic to whichever server says "primary"; when two do (an old
    primary came back after its replica was promoted), the later
    promoted_at is the current one."""
    manager = app.extensions.get("replication_manager")
    role = "replica" if manager is not None and manager.is_replica else "primary"
    marker = promoted_marker(app)
    try:
        promoted_at = int(marker.stat().st_mtime) if role == "primary" else None
    except OSError:
        promoted_at = None
    return {"role": role, "promoted_at": promoted_at}

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
        with peer_http.open_peer(req, timeout=timeout) as response:  # noqa: S310 - configured primary URL
            key = response.read().decode("utf-8").strip()
    except (urllib.error.URLError, OSError) as exc:  # HTTPError is a URLError
        raise ReplicationError(
            f"Could not fetch the secret key from {primary_url}: {peer_http.describe_failure(exc, timeout)}"
        ) from exc
    if not key:
        raise ReplicationError(f"{primary_url} returned an empty secret key.")
    return key


def find_or_create_user_via_primary(manager, claims: dict) -> int:
    """A replica must never write its own database - but Google sign-in
    creates or updates a user row on every login. Ask the primary to do
    that write instead and hand back the resulting user_id, which the
    replica can safely put in the session (the row itself arrives on the
    next database sync). Raises ReplicationError on any failure."""
    body = json.dumps(claims).encode("utf-8")
    req = urllib.request.Request(
        f"{manager.primary_url}/replication/find-or-create-user",
        data=body,
        headers={
            "Authorization": f"Bearer {manager.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with peer_http.open_peer(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReplicationError(f"The primary rejected the sign-in: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ReplicationError(
            f"Could not reach primary {manager.primary_url}: {peer_http.describe_failure(exc, TIMEOUT_SECONDS)}"
        ) from exc
    user_id = payload.get("user_id")
    if not isinstance(user_id, int):
        raise ReplicationError("Primary returned an unexpected response.")
    return user_id


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


def _prepare_snapshot(path, primary_data_dir: str, own_data_dir: str):
    """Make a downloaded snapshot usable by this server before it goes live:
    upgrade its structure when the primary runs another version, and point
    absolute paths at this server's own data directory when that differs."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        database_module.migrate_structure(connection)
        if primary_data_dir and primary_data_dir.rstrip("/") != own_data_dir.rstrip("/"):
            _rebase_paths(connection, primary_data_dir.rstrip("/"), own_data_dir.rstrip("/"))
        connection.commit()
    except sqlite3.DatabaseError as exc:
        raise ReplicationError(f"Could not prepare the downloaded database: {exc}") from exc
    finally:
        connection.close()


def _rebase_paths(connection, old_root: str, new_root: str):
    for table, column in _PATH_COLUMNS:
        connection.execute(
            f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) WHERE {column} LIKE ?",
            (old_root, new_root, f"%{old_root}%"),
        )


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
        self._sync_lock = threading.Lock()
        self.started_at = time.time()

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

    def promote(self):
        """Stop mirroring and become a primary, in memory, right away. What
        makes that survive a restart is the "promoted" marker file (see
        promoted_marker), written by the System page and webmanager-promote."""
        self.stop()
        self.primary_url = ""
        self._stop_event = threading.Event()

    def _run(self):
        self.sync_once()
        while not self._stop_event.wait(DB_POLL_SECONDS):
            self.sync_once()

    def sync_once(self):
        """Pull and apply one database snapshot from the primary. Public so
        an admin action ("sync now") can trigger it on demand too."""
        with self._sync_lock:
            before = self._served_state()
            try:
                self._pull_db_snapshot()
            except (ReplicationError, OSError) as exc:
                self.last_error = str(exc)
                self.app.logger.warning("Replication: could not sync from primary: %s", exc)
                return False
            self.last_error = None
            self.last_sync_at = time.time()
            after = self._served_state()
        if after is not None and after != before:
            # A site was started, stopped, renamed or re-addressed on the
            # primary: serve it the same way here now, not at the next data sync.
            try:
                self.app.extensions["runtime_manager"].restore_sites(include_apps=False)
            except Exception:  # noqa: BLE001 - the data sync round applies it again
                self.app.logger.exception("Replication: could not apply the new site state")
        return True

    def _served_state(self):
        """Everything in the database that decides which sites this server
        serves and how, or None if it can't be read (no database yet)."""
        try:
            connection = sqlite3.connect(
                f"file:{self.app.config['DATABASE']}?mode=ro", uri=True, timeout=5
            )
            try:
                return [
                    connection.execute(query).fetchall()
                    for query in (
                        "SELECT id, kind, status, slug, port, document_root, index_file, "
                        "nginx_config, domain_id FROM sites ORDER BY id",
                        "SELECT * FROM domains ORDER BY id",
                        "SELECT * FROM site_domain_aliases ORDER BY site_id, domain_id",
                    )
                ]
            finally:
                connection.close()
        except sqlite3.Error:
            return None

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
                with peer_http.open_peer(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
                    if response.status != 200:
                        raise ReplicationError(f"Primary returned HTTP {response.status}.")
                    primary_data_dir = response.headers.get(DATA_DIR_HEADER, "") or ""
                    with os.fdopen(descriptor, "wb") as handle:
                        shutil.copyfileobj(response, handle)
            except urllib.error.URLError as exc:
                raise ReplicationError(
                    f"Could not reach primary {self.primary_url}: "
                    f"{peer_http.describe_failure(exc, TIMEOUT_SECONDS)}"
                ) from exc
            _validate_snapshot(temporary_name)
            _prepare_snapshot(temporary_name, primary_data_dir, str(self.app.instance_path))
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
            with peer_http.open_peer(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
                body = response.read()
                status = response.status
                response_headers = list(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            response_headers = list(exc.headers.items())
        except (urllib.error.URLError, OSError) as exc:
            detail = (
                f"The primary server ({self.primary_url}) could not be reached: "
                f"{peer_http.describe_failure(exc, TIMEOUT_SECONDS)}"
            )
            if request.accept_mimetypes.best_match(["text/html", "application/json"]) == "text/html":
                return (
                    render_template(
                        "error.html",
                        title="Primary server unavailable",
                        message=(
                            "This server forwards changes to its primary, which can't be reached "
                            "right now, so nothing was saved. Try again in a moment."
                        ),
                        error_code=502,
                    ),
                    502,
                )
            return jsonify({"error": detail}), 502
        relayed = Response(body, status=status)
        for key, value in response_headers:
            lowered = key.lower()
            if lowered in _DO_NOT_FORWARD_RESPONSE_HEADERS:
                continue
            if lowered == "set-cookie":
                value = _cookie_for_this_server(value, self.app.config.get("SESSION_COOKIE_SECURE"))
            relayed.headers.add(key, value)
        return relayed


def _cookie_for_this_server(header: str, secure_here) -> str:
    """A cookie set by the primary, adjusted so a browser talking to *this*
    server keeps it: no Domain (it was the primary's), and no Secure flag when
    this server is reached over plain http (a LAN replica of an https primary)."""
    kept = []
    for index, part in enumerate(header.split(";")):
        name = part.strip().split("=", 1)[0].lower()
        if index and (name == "domain" or (name == "secure" and not secure_here)):
            continue
        kept.append(part)
    return ";".join(kept)


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
        if request.blueprint in ("mesh", "replication", "data_replication"):
            return None
        if request.endpoint == "static" or request.endpoint in _LOCAL_ONLY_ENDPOINTS:
            return None
        response = manager.forward_current_request()
        if getattr(response, "status_code", 502) < 400:
            # Show the result straight away: the page this request leads to
            # is read from this server's copy of the database.
            try:
                manager.sync_once()
            except Exception:  # noqa: BLE001 - the change itself was saved; the next round catches up
                app.logger.exception("Replication: could not sync right after a forwarded change")
        # The primary's answer carries the session cookie (with the "saved"
        # or error message to show next). This server must not write its own
        # copy of the session on top of it, which a signed-in ("permanent")
        # session otherwise does on every response.
        session.permanent = False
        session.modified = False
        return response


@bp.post("/replication/find-or-create-user")
def find_or_create_user():
    """Performs the one database write a replica cannot safely avoid on its
    own: Google sign-in creates or touches a user row on every login. A
    replica calls this on its primary instead of writing locally (see
    auth.google_callback and find_or_create_user_via_primary above)."""
    hub = current_app.extensions.get("mesh_hub")
    if hub is None or not hub.authorize_sensitive(
        request.headers.get("Authorization", ""), request.remote_addr or ""
    ):
        return jsonify({"error": "Invalid or missing peer token"}), 401

    from .auth import _find_or_create_user

    claims = request.get_json(silent=True)
    if not isinstance(claims, dict) or not claims.get("sub") or not claims.get("email"):
        return jsonify({"error": "Missing or invalid claims."}), 400
    try:
        user_id = _find_or_create_user(claims)
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"user_id": user_id})


@bp.get("/replication/db-snapshot")
def db_snapshot():
    hub = current_app.extensions.get("mesh_hub")
    if hub is None or not hub.authorize_sensitive(
        request.headers.get("Authorization", ""), request.remote_addr or ""
    ):
        return jsonify({"error": "Invalid or missing peer token"}), 401

    from .db import close_db, get_db

    # Closing the request-scoped connection first avoids a needless
    # "database is locked" edge case if SQLite's own file lock is held
    # elsewhere on the same process at the moment of backup.
    close_db()
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
    response.headers[DATA_DIR_HEADER] = str(current_app.instance_path)
    return response
