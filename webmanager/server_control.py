"""Root-only changes a super admin can ask for from the dashboard.

The web process never gets root. It writes a one-word request file; the root
service ``webmanager-control`` (started by ``webmanager-control.path``, see
deploy/debian/control.sh) does the work and writes a status file this module
reads back. The changes: turning app hosting on or off (exactly what
``sudo webmanager-enable-apps`` does on the server), and saving the settings
listed in settings_editor.py to /etc/webmanager/webmanager.env.
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


def settings_request_file() -> Path:
    return Path(current_app.config.get("SETTINGS_CONTROL_REQUEST_FILE") or _requests_dir() / "settings")


def status_file() -> Path:
    configured = current_app.config.get("CONTROL_STATUS_FILE")
    if configured:
        return Path(configured)
    return Path(current_app.config["PROGRAM_UPDATE_STATUS_FILE"]).with_name("control-status.json")


def settings_allowed() -> bool:
    """WEBMANAGER_DASHBOARD_SETTINGS=0 keeps the settings file a shell-only matter."""
    return bool(current_app.config.get("DASHBOARD_SETTINGS", True))


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
    for action, request_file in (("apps", apps_request_file()), ("settings", settings_request_file())):
        if request_file.exists():
            return {
                "state": "requested", "action": "settings" if action == "settings" else None,
                "detail": None, "at": None,
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
        "action": data.get("action") if data.get("action") in {"apps_on", "apps_off", "settings"} else None,
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


def settings_blocked_reason() -> str | None:
    """Why the settings can't be saved from the dashboard here, or None."""
    if not settings_allowed():
        return (
            "Changing settings from the dashboard is turned off on this server "
            "(WEBMANAGER_DASHBOARD_SETTINGS=0). Edit /etc/webmanager/webmanager.env on the server."
        )
    if control_active() is False:
        return (
            "The service that makes this change isn't running on this server. "
            "Re-run setup.sh there (or: sudo systemctl enable --now webmanager-control.path)."
        )
    status = read_status()
    if status and status["state"] in BUSY_STATES:
        return "Another change to this server is under way. Try again when it's done."
    return None


def request_settings(changes: dict):
    """File the settings to save (already checked). Raises OSError."""
    _write_request(settings_request_file(), json.dumps(changes, sort_keys=True) + "\n")


def request_app_hosting(enabled: bool):
    """File the request. Raises OSError."""
    _write_request(apps_request_file(), "on\n" if enabled else "off\n")


def _write_request(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
