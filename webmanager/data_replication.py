"""Site/app data replication: Git checkouts and each app's own /data volume,
mirrored one-way from primary to replica.

Unlike webmanager/replication.py's database sync, this is a full re-sync
every interval - simple and safe, at the cost of being heavier for large
repositories or app data; there is no incremental diffing. A replica never
runs an app container against this data itself: only static sites are
started automatically once synced (safe - they're read-only, stateless
files). Apps stay stopped until this replica is promoted to primary, so
nothing ever writes to a copy of app data that the next sync round is
about to overwrite.
"""

import atexit
import os
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .apps import AppError

DATA_POLL_SECONDS = 60
TIMEOUT_SECONDS = 300
REPOSITORIES_PREFIX = "repositories"
APP_DATA_PREFIX = "app-data"

bp = Blueprint("data_replication", __name__)


def _safe_members(members, strip_prefix: str):
    """Members under strip_prefix/, with that prefix removed and the name
    checked so extraction can never escape the target directory (a
    defensive check, not primarily a trust boundary - this archive is only
    ever produced by an authenticated peer)."""
    safe = []
    for member in members:
        name = member.name
        if name == strip_prefix:
            continue
        if not name.startswith(strip_prefix + "/"):
            continue
        relative = name[len(strip_prefix) + 1 :]
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            continue
        member.name = relative
        safe.append(member)
    return safe


class DataReplicationManager:
    """On a replica, periodically mirrors the primary's site/app data."""

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
        self._thread = threading.Thread(target=self._run, name="webmanager-data-replication", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _run(self):
        self.sync_once()
        while not self._stop_event.wait(DATA_POLL_SECONDS):
            self.sync_once()

    def sync_once(self) -> bool:
        """Pull and apply one data snapshot from the primary. Public so an
        admin action ("sync now") can trigger it on demand too."""
        try:
            self._pull_and_apply()
        except (AppError, OSError, RuntimeError) as exc:
            self.last_error = str(exc)
            self.app.logger.warning("Data replication: could not sync from primary: %s", exc)
            return False
        self.last_error = None
        self.last_sync_at = time.time()
        # Safe immediately: static sites are read-only, stateless files.
        # Apps stay stopped until this replica is promoted.
        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)
        return True

    def _pull_and_apply(self):
        req = urllib.request.Request(
            f"{self.primary_url}/replication/data-snapshot",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=".data-sync-", suffix=".tar")
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured primary URL
                    if response.status != 200:
                        raise RuntimeError(f"Primary returned HTTP {response.status}.")
                    with open(temporary_path, "wb") as handle:
                        shutil.copyfileobj(response, handle)
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Could not reach primary {self.primary_url}: {exc}") from exc
            self._apply_snapshot(temporary_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _apply_snapshot(self, archive_path: Path):
        repository_root = Path(self.app.config["REPOSITORY_ROOT"])
        runtime = self.app.extensions["runtime_manager"].container_runtime
        with tarfile.open(archive_path, mode="r") as archive:
            members = archive.getmembers()
            repo_members = _safe_members(
                [m for m in members if m.name == REPOSITORIES_PREFIX or m.name.startswith(REPOSITORIES_PREFIX + "/")],
                REPOSITORIES_PREFIX,
            )
            app_members = [
                member
                for member in members
                if member.name.startswith(APP_DATA_PREFIX + "/") and member.name.endswith(".tar")
            ]

            shutil.rmtree(repository_root, ignore_errors=True)
            repository_root.mkdir(parents=True, exist_ok=True)
            if repo_members:
                archive.extractall(path=repository_root, members=repo_members)

            if app_members and runtime is not None:
                with tempfile.TemporaryDirectory(prefix="wm-app-restore-") as scratch:
                    scratch_path = Path(scratch)
                    for member in app_members:
                        site_id = member.name[len(APP_DATA_PREFIX) + 1 : -len(".tar")]
                        if not site_id.isdigit():
                            continue
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            continue
                        tar_path = scratch_path / f"{site_id}.tar"
                        with open(tar_path, "wb") as handle:
                            shutil.copyfileobj(extracted, handle)
                        try:
                            runtime.restore_data(runtime.volume_name(int(site_id)), tar_path)
                        except AppError as exc:
                            self.app.logger.warning(
                                "Data replication: could not restore app %s's data: %s",
                                site_id,
                                exc,
                            )


@bp.get("/replication/data-snapshot")
def data_snapshot():
    hub = current_app.extensions.get("mesh_hub")
    if hub is None or not hub.authorize_sensitive(
        request.headers.get("Authorization", ""), request.remote_addr or ""
    ):
        return jsonify({"error": "Invalid or missing peer token"}), 401

    from .db import get_db

    repository_root = Path(current_app.config["REPOSITORY_ROOT"])
    runtime = current_app.extensions["runtime_manager"].container_runtime
    descriptor, archive_name = tempfile.mkstemp(prefix="wm-data-snapshot-", suffix=".tar")
    os.close(descriptor)
    archive_path = Path(archive_name)
    scratch_dir = tempfile.mkdtemp(prefix="wm-app-backup-")
    try:
        with tarfile.open(archive_path, mode="w") as archive:
            if repository_root.is_dir():
                archive.add(repository_root, arcname=REPOSITORIES_PREFIX)
            if runtime is not None:
                app_ids = [
                    row["id"]
                    for row in get_db().execute("SELECT id FROM sites WHERE kind = 'app'").fetchall()
                ]
                for site_id in app_ids:
                    name = runtime.container_name(site_id)
                    exported = runtime.backup_data(name, Path(scratch_dir) / str(site_id))
                    if exported is not None:
                        archive.add(exported, arcname=f"{APP_DATA_PREFIX}/{site_id}.tar")
    except Exception:
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(scratch_dir, ignore_errors=True)
        raise
    shutil.rmtree(scratch_dir, ignore_errors=True)

    def stream():
        try:
            with open(archive_path, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
        finally:
            archive_path.unlink(missing_ok=True)

    response = current_app.response_class(stream(), mimetype="application/x-tar")
    response.headers["Cache-Control"] = "no-store"
    return response
