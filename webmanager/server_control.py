"""Root-only changes a super admin can ask for from the dashboard.

The web process never gets root. It writes a one-word request file; the root
service ``webmanager-control`` (started by ``webmanager-control.path``, see
deploy/debian/control.sh) does the work and writes a status file this module
reads back. Today the one change is turning app hosting on or off, which is
exactly what ``sudo webmanager-enable-apps`` does on the server.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from flask import current_app

BUSY_STATES = {"requested", "running"}


def _requests_dir() -> Path:
    return Path(current_app.config["PROGRAM_UPDATE_REQUEST_FILE"]).parent


def apps_request_file() -> Path:
    return Path(current_app.config.get("APPS_CONTROL_REQUEST_FILE") or _requests_dir() / "apps")


def status_file() -> Path:
    configured = current_app.config.get("CONTROL_STATUS_FILE")
    if configured:
        return Path(configured)
    return Path(current_app.config["PROGRAM_UPDATE_STATUS_FILE"]).with_name("control-status.json")


def control_allowed() -> bool:
    """WEBMANAGER_DASHBOARD_APP_CONTROL=0 keeps app hosting a shell-only decision."""
    return bool(current_app.config.get("DASHBOARD_APP_CONTROL", True))


def control_active():
    """Whether the root helper is listening: True, False, or None if unknown."""
    if "CONTROL_ACTIVE" in current_app.config:
        return current_app.config["CONTROL_ACTIVE"]
    if current_app.config.get("TESTING"):
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "webmanager-control.path"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() == "active"


def read_status() -> dict | None:
    """{"state": "requested"|"running"|"done"|"error", "action", "message",
    "detail", "at"} for the latest change, or None if there never was one."""
    if apps_request_file().exists():
        return {
            "state": "requested", "action": None, "detail": None, "at": None,
            "message": "Waiting for the server to pick up the request…",
        }
    try:
        path = status_file()
        if path.stat().st_size > 64 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") not in {"running", "done", "error"}:
        return None
    return {
        "state": data["state"],
        "action": data.get("action") if data.get("action") in {"apps_on", "apps_off"} else None,
        "message": str(data.get("message") or ""),
        "detail": data.get("detail") if isinstance(data.get("detail"), str) else None,
        "at": data.get("at") if isinstance(data.get("at"), str) else None,
    }


def blocked_reason() -> str | None:
    """Why app hosting can't be switched from the dashboard here, or None."""
    if not control_allowed():
        return (
            "Switching app hosting from the dashboard is turned off on this server "
            "(WEBMANAGER_DASHBOARD_APP_CONTROL=0). On the server run: sudo webmanager-enable-apps"
        )
    if control_active() is False:
        return (
            "The service that makes this change isn't running on this server. "
            "Re-run setup.sh there (or: sudo systemctl enable --now webmanager-control.path)."
        )
    status = read_status()
    if status and status["state"] in BUSY_STATES:
        return "A change to app hosting is already under way."
    return None


def request_app_hosting(enabled: bool):
    """File the request. Raises OSError."""
    path = apps_request_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".apps.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write("on\n" if enabled else "off\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
