import atexit
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import get_db
from .git_service import GitError, clone_repository, resolve_folder


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

                sites = database.execute(
                    """
                    SELECT name, folder, index_file
                    FROM sites
                    WHERE repository_id = ?
                    """,
                    (repository_id,),
                ).fetchall()

                def validate_deployments(staging):
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

                try:
                    clone_repository(
                        repository["url"],
                        Path(repository["local_path"]),
                        repository["branch"],
                        validate_staging=validate_deployments,
                    )
                except GitError as exc:
                    self._record_failure(database, repository_id, str(exc))
                    self.app.logger.warning(
                        "Repository %s refresh failed: %s",
                        repository_id,
                        exc,
                    )
                    return RefreshResult("error", str(exc))
                except Exception as exc:
                    message = f"Unexpected refresh failure: {exc}"
                    self._record_failure(database, repository_id, message)
                    self.app.logger.exception(
                        "Repository %s refresh failed unexpectedly.",
                        repository_id,
                    )
                    return RefreshResult("error", message)

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
                    SET status = 'ready', error = NULL,
                        last_refreshed_at = CURRENT_TIMESTAMP,
                        next_refresh_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (next_run, repository_id),
                )
                database.commit()
                return RefreshResult("success", "Repository refreshed from Git.")

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
                self.app.logger.exception("Automatic repository refresh cycle failed.")

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
            SET error = ?, next_refresh_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, next_run, repository_id),
        )
        database.commit()
