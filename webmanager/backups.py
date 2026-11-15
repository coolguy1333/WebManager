"""Backups of everything that can't be re-created: the database (people,
sites, settings, encrypted app variables), the keys that protect it, and the
server's settings file.

A backup is one small ``.tar.gz`` in ``<data>/backups``. One is made every day
and before anything risky; the newest ``BACKUP_KEEP`` are kept. Git checkouts
are not included (they are cloned again from their repositories after a
restore) and neither is app container data, which has its own backups.

Restore with ``sudo webmanager-restore <file>`` (see deploy/debian/restore.sh).
"""

import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import activity

NAME_RE = re.compile(r"^webmanager-\d{8}-\d{6}(?:-[a-z0-9]{1,12})?\.tar\.gz$")
KEY_FILES = ("secret.key", "peer-token", "peers.json")
DATABASE_MEMBER = "webmanager.sqlite3"
SETTINGS_MEMBER = "settings.env"
MANIFEST_MEMBER = "manifest.json"
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_lock = threading.Lock()


class BackupError(RuntimeError):
    pass


def backup_dir(app) -> Path:
    return Path(app.config.get("BACKUP_DIR") or Path(app.instance_path) / "backups")


def list_backups(app) -> list[dict]:
    """Newest first: {"name", "size", "created_at" (UTC text), "age_seconds"}."""
    items = []
    try:
        candidates = list(backup_dir(app).iterdir())
    except OSError:
        return []
    now = time.time()
    for path in candidates:
        if not NAME_RE.fullmatch(path.name) or not path.is_file():
            continue
        details = path.stat()
        items.append(
            {
                "name": path.name,
                "size": details.st_size,
                "created_at": datetime.fromtimestamp(details.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "age_seconds": max(0, now - details.st_mtime),
            }
        )
    return sorted(items, key=lambda item: item["name"], reverse=True)


def backup_path(app, name: str) -> Path | None:
    """The file for a listed backup name, or None (never a path outside the folder)."""
    if not NAME_RE.fullmatch(name or ""):
        return None
    path = backup_dir(app) / name
    return path if path.is_file() else None


def create_backup(app, reason: str = "manual") -> Path:
    """Write a new backup and prune old ones. Raises BackupError."""
    directory = backup_dir(app)
    label = re.sub(r"[^a-z0-9]", "", reason.lower())[:12]
    with _lock:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            target = directory / f"webmanager-{stamp}{'-' + label if label else ''}.tar.gz"
            with tempfile.TemporaryDirectory(dir=directory, prefix=".building-") as scratch:
                snapshot = Path(scratch) / DATABASE_MEMBER
                _snapshot_database(app.config["DATABASE"], snapshot)
                staged = Path(scratch) / "backup.tar.gz"
                with tarfile.open(staged, "w:gz") as archive:
                    archive.add(snapshot, arcname=DATABASE_MEMBER)
                    for name in KEY_FILES:
                        source = Path(app.instance_path) / name
                        if source.is_file():
                            archive.add(source, arcname=name)
                    settings = Path(app.config.get("SETTINGS_FILE") or "/etc/webmanager/webmanager.env")
                    if settings.is_file() and os.access(settings, os.R_OK):
                        archive.add(settings, arcname=SETTINGS_MEMBER)
                    manifest = json.dumps(
                        {
                            "format": 1,
                            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "data_dir": str(app.instance_path),
                            "reason": reason,
                        }
                    ).encode()
                    info = tarfile.TarInfo(MANIFEST_MEMBER)
                    info.size = len(manifest)
                    info.mtime = int(time.time())
                    archive.addfile(info, io.BytesIO(manifest))
                os.chmod(staged, 0o600)
                os.replace(staged, target)
        except (OSError, sqlite3.Error, tarfile.TarError) as exc:
            raise BackupError(f"The backup could not be written: {exc}") from exc
        _prune(app)
    return target


def _snapshot_database(source: str, destination: Path):
    """A consistent copy of a database that may be in use."""
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
        ok = writer.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise sqlite3.DatabaseError(f"the copy failed its integrity check ({ok})")
    finally:
        writer.close()
        reader.close()


def _prune(app):
    keep = max(1, int(app.config.get("BACKUP_KEEP", 14)))
    for item in list_backups(app)[keep:]:
        try:
            (backup_dir(app) / item["name"]).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Restore (run by `python run.py restore FILE`, with WebManager stopped)
# ---------------------------------------------------------------------------
def restore_backup(archive_path: Path, data_dir: Path, database_path: Path) -> dict:
    """Put a backup's database and keys in place. Returns
    {"manifest": {...}, "settings": Path | None, "previous": Path | None}.

    The current database is kept next to the new one as
    ``webmanager.sqlite3.before-restore`` so a wrong restore can be undone."""
    from .replication import ReplicationError, _prepare_snapshot

    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise BackupError(f"{archive_path} does not exist.")
    data_dir.mkdir(parents=True, exist_ok=True)
    wanted = {DATABASE_MEMBER, MANIFEST_MEMBER, SETTINGS_MEMBER, *KEY_FILES}
    with tempfile.TemporaryDirectory(dir=data_dir, prefix=".restoring-") as scratch:
        scratch = Path(scratch)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                for member in archive:
                    # Only the plain files a backup contains, by exact name.
                    if member.name not in wanted or not member.isfile() or member.size > MAX_MEMBER_BYTES:
                        continue
                    source = archive.extractfile(member)
                    with open(scratch / member.name, "wb") as handle:
                        while chunk := source.read(1024 * 1024):
                            handle.write(chunk)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BackupError(f"{archive_path.name} is not a readable WebManager backup: {exc}") from exc
        new_database = scratch / DATABASE_MEMBER
        if not new_database.is_file():
            raise BackupError(f"{archive_path.name} has no database in it; it is not a WebManager backup.")
        try:
            manifest = json.loads((scratch / MANIFEST_MEMBER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        try:
            connection = sqlite3.connect(new_database)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("integrity check failed")
                connection.execute("SELECT COUNT(*) FROM users").fetchone()
            finally:
                connection.close()
            # Upgrade an older backup's structure and point its paths at this
            # server's data folder (it may come from another machine).
            _prepare_snapshot(str(new_database), str(manifest.get("data_dir") or data_dir), str(data_dir))
        except (sqlite3.Error, ReplicationError) as exc:
            raise BackupError(f"The database in {archive_path.name} is damaged: {exc}") from exc

        previous = None
        if database_path.exists():
            previous = database_path.with_name(database_path.name + ".before-restore")
            os.replace(database_path, previous)
        for stale in (database_path.with_name(database_path.name + suffix) for suffix in ("-wal", "-shm", "-journal")):
            stale.unlink(missing_ok=True)
        os.chmod(new_database, 0o640)
        os.replace(new_database, database_path)
        for name in KEY_FILES:
            if (scratch / name).is_file():
                os.chmod(scratch / name, 0o600)
                os.replace(scratch / name, data_dir / name)
        settings = None
        if (scratch / SETTINGS_MEMBER).is_file():
            settings = data_dir / "restored-settings.env"
            os.chmod(scratch / SETTINGS_MEMBER, 0o600)
            os.replace(scratch / SETTINGS_MEMBER, settings)
    return {"manifest": manifest, "settings": settings, "previous": previous}


def reclone_missing_sources(app) -> int:
    """Clone again every Git source whose checkout is gone (after a restore on
    a new machine, or a lost disk), then start the sites that use them.
    Returns how many came back."""
    from .db import get_db
    from .git_service import GitError, clone_repository, repository_commit

    restored = 0
    with app.app_context():
        database = get_db()
        missing = [
            row for row in database.execute("SELECT * FROM repositories").fetchall()
            if row["local_path"] and not Path(row["local_path"]).is_dir()
        ]
        for repository in missing:
            target = Path(repository["local_path"])
            try:
                clone_repository(
                    repository["url"], target, repository["branch"],
                    max_bytes=app.config.get("MAX_REPOSITORY_BYTES") or None,
                )
                database.execute(
                    """
                    UPDATE repositories SET status = 'ready', error = NULL, current_commit = ?,
                           pending_path = NULL, pending_commit = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (repository_commit(target), repository["id"]),
                )
                database.commit()
                restored += 1
            except (GitError, OSError) as exc:
                database.execute(
                    "UPDATE repositories SET error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (f"Its files are missing and it could not be cloned again: {exc}"[:500], repository["id"]),
                )
                database.commit()
                app.logger.warning("Could not clone %s again: %s", repository["url"], exc)
    if missing:
        activity.record_system(
            app, "Cloned missing sources again",
            f"{restored} of {len(missing)} source(s) whose files were missing were cloned again.",
            ok=restored == len(missing),
        )
        if restored:
            app.extensions["runtime_manager"].restore_sites()
    return restored


# ---------------------------------------------------------------------------
class BackupScheduler:
    """Makes a backup when the newest one is more than a day old."""

    INTERVAL_SECONDS = 24 * 3600
    POLL_SECONDS = 1800

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="webmanager-backups", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_once(self) -> bool:
        """Make a backup if one is due. Returns True when one was made."""
        if not self.app.config.get("BACKUPS_ENABLED", True):
            return False
        existing = list_backups(self.app)
        if existing and existing[0]["age_seconds"] < self.INTERVAL_SECONDS:
            return False
        try:
            path = create_backup(self.app, "daily")
        except BackupError as exc:
            self.app.logger.error("Scheduled backup failed: %s", exc)
            activity.record_system(self.app, "Scheduled backup failed", str(exc), ok=False)
            notifier = self.app.extensions.get("notifier")
            if notifier is not None:
                notifier.send("Backup failed", str(exc), level="error")
            return False
        activity.record_system(self.app, "Made the daily backup", path.name)
        return True

    def _run(self):
        # Shortly after start, so a new install has a backup within minutes.
        if self._stop.wait(120):
            return
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the schedule alive
                self.app.logger.exception("Backup scheduler error")
            if self._stop.wait(self.POLL_SECONDS):
                return
