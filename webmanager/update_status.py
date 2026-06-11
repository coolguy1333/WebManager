import json
import os
import re
import tempfile
from pathlib import Path

from flask import current_app


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_STATUS = {
    "state": "unknown",
    "installed_commit": None,
    "available_commit": None,
    "update_available": False,
    "message": "No update check has completed yet.",
    "checked_at": None,
}


def read_update_status():
    path = Path(current_app.config["PROGRAM_UPDATE_STATUS_FILE"])
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError("Update status file is too large.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULT_STATUS.copy()

    status = DEFAULT_STATUS.copy()
    status.update({key: payload.get(key) for key in status if key in payload})
    status["update_available"] = bool(status["update_available"])
    return status


def request_program_update(commit):
    if not COMMIT_RE.fullmatch(commit or ""):
        raise ValueError("The available update commit is invalid.")

    path = Path(current_app.config["PROGRAM_UPDATE_REQUEST_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".install.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{commit}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
