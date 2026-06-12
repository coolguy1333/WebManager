import atexit
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import get_db
from .git_service import (
    GitError,
    activate_repository,
    clone_repository,
    remove_repository_path,
    repository_commit,
    resolve_folder,
)


MIN_REFRESH_MINUTES = 5
MAX_REFRESH_MINUTES = 30 * 24 * 60


@dataclass(frozen=True)
class RefreshResult:
    status: str
    message: str


def next_refresh_time(minutes: int) -> str:
    next_run = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return next_run.strftime("%Y-%m-%d %H:%M:%S")


class RepositoryRefreshManager:
    def __init__(self, app):
        self.app = app
        self.poll_seconds = max(
            5,
            int(app.config.get("AUTO_REFRESH_POLL_SECONDS", 30)),
        )
        self._stop_event = threading.Event()
        self._thread = None
        self._locks_guard = threading.Lock()
        self._repository_locks = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="webmanager-repository-refresh",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop)

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def refresh(self, repository_id: int, wait: bool = True) -> RefreshResult:
        with self.repository_lock(repository_id, wait=wait) as acquired:
            if not acquired:
                return RefreshResult("busy", "A refresh is already running.")
            with self.app.app_context():
                database = get_db()
                repository = database.execute(
                    "SELECT * FROM repositories WHERE id = ?",
                    (repository_id,),
                ).fetchone()
                if repository is None:
                    return RefreshResult("missing", "Repository no longer exists.")
                if (
                    repository["update_mode"] == "approval"
                    and repository["pending_commit"]
                ):
                    database.execute(
                        """
                        UPDATE repositories
                        SET next_refresh_at = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            self._next_run(repository["auto_refresh_minutes"]),
                            repository_id,
                        ),
                    )
                    database.commit()
                    return RefreshResult(
                        "available",
                        "The existing validated update is still waiting for owner approval.",
                    )

                database.execute(
                    """
                    UPDATE repositories
                    SET update_state = 'checking', update_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (repository_id,),
                )
                database.commit()
                sites = self._sites(database, repository_id)
                target = Path(repository["local_path"])
                pending = target.parent / f".{target.name}.pending"
                staged = False

                try:
                    clone_repository(
                        repository["url"],
                        pending,
                        repository["branch"],
                        validate_staging=lambda staging: self._validate_sites(
                            sites,
                            staging,
                        ),
                    )
                    staged = True
                    pending_commit = repository_commit(pending)
                    current_commit = (
                        repository["current_commit"]
                        or repository_commit(target)
                    )
                except GitError as exc:
                    if staged:
                        remove_repository_path(pending)
                    self._record_failure(database, repository_id, str(exc))
                    self.app.logger.warning(
                        "Repository %s update check failed: %s",
                        repository_id,
                        exc,
                    )
                    return RefreshResult("error", str(exc))
                except Exception as exc:
                    message = f"Unexpected refresh failure: {exc}"
                    self._record_failure(database, repository_id, message)
                    self.app.logger.exception(
                        "Repository %s update check failed unexpectedly.",
                        repository_id,
                    )
                    return RefreshResult("error", message)

                next_run = self._next_run(repository["auto_refresh_minutes"])
                if pending_commit == current_commit:
                    remove_repository_path(pending)
                    database.execute(
                        """
                        UPDATE repositories
                        SET status = 'ready', error = NULL,
                            update_state = 'idle', update_error = NULL,
                            last_checked_at = CURRENT_TIMESTAMP,
                            current_commit = ?, pending_path = NULL,
                            pending_commit = NULL, pending_at = NULL,
                            next_refresh_at = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (current_commit, next_run, repository_id),
                    )
                    database.commit()
                    return RefreshResult("current", "Repository is already current.")

                database.execute(
                    """
                    UPDATE repositories
                    SET status = 'update_available', error = NULL,
                        update_state = 'idle', update_error = NULL,
                        last_checked_at = CURRENT_TIMESTAMP,
                        current_commit = ?, pending_path = ?,
                        pending_commit = ?, pending_at = CURRENT_TIMESTAMP,
                        next_refresh_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        current_commit,
                        str(pending.resolve()),
                        pending_commit,
                        next_run,
                        repository_id,
                    ),
                )
                database.commit()
                if repository["update_mode"] == "auto":
                    return self._apply_pending_locked(database, repository_id)
                return RefreshResult(
                    "available",
                    "A validated update is waiting for owner approval.",
                )

    def apply_pending(self, repository_id: int, wait: bool = True) -> RefreshResult:
        with self.repository_lock(repository_id, wait=wait) as acquired:
            if not acquired:
                return RefreshResult("busy", "A repository update is already running.")
            with self.app.app_context():
                return self._apply_pending_locked(get_db(), repository_id)

    def discard_pending(self, repository_id: int) -> RefreshResult:
        with self.repository_lock(repository_id) as acquired:
            if not acquired:
                return RefreshResult("busy", "A repository update is already running.")
            with self.app.app_context():
                database = get_db()
                repository = database.execute(
                    "SELECT * FROM repositories WHERE id = ?",
                    (repository_id,),
                ).fetchone()
                if repository is None:
                    return RefreshResult("missing", "Repository no longer exists.")
                if repository["pending_path"]:
                    remove_repository_path(Path(repository["pending_path"]))
                database.execute(
                    """
                    UPDATE repositories
                    SET status = 'ready', pending_path = NULL,
                        pending_commit = NULL, pending_at = NULL, error = NULL,
                        update_state = 'idle', update_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (repository_id,),
                )
                database.commit()
                return RefreshResult("discarded", "Pending update discarded.")

    @contextmanager
    def repository_lock(self, repository_id: int, wait: bool = True):
        lock = self._lock_for(repository_id)
        acquired = lock.acquire(blocking=wait)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    def run_due_once(self):
        with self.app.app_context():
            due_ids = [
                row["id"]
                for row in get_db().execute(
                    """
                    SELECT id
                    FROM repositories
                    WHERE auto_refresh_minutes IS NOT NULL
                      AND next_refresh_at IS NOT NULL
                      AND next_refresh_at <= CURRENT_TIMESTAMP
                    ORDER BY next_refresh_at
                    """
                ).fetchall()
            ]

        for repository_id in due_ids:
            self.refresh(repository_id, wait=False)
        return len(due_ids)

    def _run(self):
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self.run_due_once()
            except Exception:
                self.app.logger.exception("Automatic repository update check failed.")

    def _lock_for(self, repository_id):
        with self._locks_guard:
            return self._repository_locks.setdefault(
                repository_id,
                threading.Lock(),
            )

    def _record_failure(self, database, repository_id, error):
        interval = database.execute(
            "SELECT auto_refresh_minutes FROM repositories WHERE id = ?",
            (repository_id,),
        ).fetchone()
        next_run = (
            next_refresh_time(interval["auto_refresh_minutes"])
            if interval and interval["auto_refresh_minutes"]
            else None
        )
        database.execute(
            """
            UPDATE repositories
            SET error = ?, update_state = 'failed', update_error = ?,
                last_checked_at = CURRENT_TIMESTAMP,
                next_refresh_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, error, next_run, repository_id),
        )
        database.commit()

    def _apply_pending_locked(self, database, repository_id):
        repository = database.execute(
            "SELECT * FROM repositories WHERE id = ?",
            (repository_id,),
        ).fetchone()
        if repository is None:
            return RefreshResult("missing", "Repository no longer exists.")
        if not repository["pending_path"] or not repository["pending_commit"]:
            return RefreshResult("missing", "No pending update is available.")

        database.execute(
            """
            UPDATE repositories
            SET update_state = 'updating', update_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (repository_id,),
        )
        database.commit()
        affected_sites = self._sites(database, repository_id)
        pending = Path(repository["pending_path"])
        target = Path(repository["local_path"])
        expected_pending = target.parent / f".{target.name}.pending"
        try:
            if pending.resolve() != expected_pending.resolve():
                raise GitError("The staged repository path is invalid.")
            self._validate_sites(affected_sites, pending)
            commit = repository_commit(pending)
            if commit != repository["pending_commit"]:
                raise GitError("The staged repository revision changed unexpectedly.")
            activate_repository(pending, target)
        except GitError as exc:
            self._record_failure(database, repository_id, str(exc))
            if not pending.exists():
                database.execute(
                    """
                    UPDATE repositories
                    SET status = 'error', pending_path = NULL,
                        pending_commit = NULL, pending_at = NULL
                    WHERE id = ?
                    """,
                    (repository_id,),
                )
                database.commit()
            return RefreshResult("error", str(exc))

        database.execute(
            """
            UPDATE repositories
            SET status = 'ready', error = NULL,
                update_state = 'idle', update_error = NULL,
                current_commit = ?, pending_path = NULL,
                pending_commit = NULL, pending_at = NULL,
                last_refreshed_at = CURRENT_TIMESTAMP,
                last_update_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (commit, repository_id),
        )
        database.commit()
        restart_failures = []
        runtime = self.app.extensions.get("runtime_manager")
        if runtime:
            for site in affected_sites:
                if site["status"] != "running":
                    continue
                try:
                    runtime.restart_site(site["id"])
                except Exception as exc:
                    restart_failures.append(f"{site['name']}: {exc}")
                    self.app.logger.exception(
                        "Could not restart site %s after repository update.",
                        site["id"],
                    )
        if restart_failures:
            return RefreshResult(
                "applied",
                "Update applied, but some sites could not restart: "
                + "; ".join(restart_failures),
            )
        return RefreshResult(
            "applied",
            "Validated update applied and running sites were reloaded.",
        )

    @staticmethod
    def _sites(database, repository_id):
        return database.execute(
            """
            SELECT id, name, folder, index_file, status
            FROM sites
            WHERE repository_id = ?
            """,
            (repository_id,),
        ).fetchall()

    @staticmethod
    def _validate_sites(sites, staging):
        for site in sites:
            try:
                selected = resolve_folder(Path(staging), site["folder"])
            except GitError as exc:
                raise GitError(
                    f"Update would remove the deployed folder for {site['name']}."
                ) from exc
            if not (selected / site["index_file"]).is_file():
                raise GitError(
                    f"Update would remove {site['index_file']} from {site['name']}."
                )

    @staticmethod
    def _next_run(minutes):
        return next_refresh_time(minutes) if minutes else None
