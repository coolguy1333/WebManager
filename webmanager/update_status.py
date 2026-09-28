import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_STATUS = {
    "state": "unknown",
    "installed_commit": None,
    "available_commit": None,
    "update_available": False,
    "message": "No update check has completed yet. Select Check now to run one.",
    "checked_at": None,
}


IN_PROGRESS_STATES = {"testing", "backing_up", "installing"}
APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_UPDATE_REPOSITORY = "https://github.com/coolguy1333/WebManager.git"
REPOSITORY_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# The result of the last check WebManager ran itself. The root-owned updater
# service normally writes status.json, but it can be switched off (and a
# request file sits unread forever when it is), so the page must not depend
# on it just to find out whether GitHub has something newer.
_live_lock = threading.Lock()
_live_status = None
_live_running = threading.Event()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_file_status():
    path = Path(current_app.config["PROGRAM_UPDATE_STATUS_FILE"])
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError("Update status file is too large.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    status = DEFAULT_STATUS.copy()
    status.update({key: payload.get(key) for key in status if key in payload})
    status["update_available"] = bool(status["update_available"])
    return status


def _checked_key(status):
    return str((status or {}).get("checked_at") or "").removesuffix(" UTC")


def read_update_status():
    """The freshest of the updater's status file and our own live check."""
    file_status = _read_file_status()
    if file_status is not None and file_status["state"] in IN_PROGRESS_STATES:
        return file_status
    with _live_lock:
        live = dict(_live_status) if _live_status else None
    if live and (file_status is None or _checked_key(live) >= _checked_key(file_status)):
        if file_status and file_status.get("installed_commit") and not live.get("installed_commit"):
            live["installed_commit"] = file_status["installed_commit"]
        return live
    return file_status or DEFAULT_STATUS.copy()


def installed_commit():
    try:
        value = (APP_DIR / ".installed-commit").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if COMMIT_RE.fullmatch(value) else None


def update_source():
    """(repository URL, branch) to check, like the updater would use."""
    repository = str(current_app.config.get("UPDATE_REPOSITORY") or "").strip()
    branch = str(current_app.config.get("UPDATE_BRANCH") or "").strip()
    if not repository:
        try:
            text = (APP_DIR / ".git" / "config").read_text(encoding="utf-8")
            match = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text)
            repository = match.group(1) if match else ""
        except OSError:
            repository = ""
    ssh = re.match(r"^(?:ssh://)?git@github\.com[:/]([^/]+)/(.+)$", repository)
    if ssh:
        repository = f"https://github.com/{ssh.group(1)}/{ssh.group(2)}"
    repository = repository or DEFAULT_UPDATE_REPOSITORY
    return repository, branch or "main"


def _result(state, message, installed, available):
    return {
        "state": state,
        "installed_commit": installed,
        "available_commit": available,
        "update_available": bool(available and installed and not available.startswith(installed) and available != installed),
        "message": message,
        "checked_at": _now(),
    }


def check_upstream(repository, branch, installed, timeout=20):
    """Ask GitHub for the branch's latest commit (git ls-remote, no clone,
    no root needed) and compare it with what's installed."""
    global _live_status
    if not REPOSITORY_RE.fullmatch(repository):
        result = _result("error", "The update repository must be an HTTPS github.com URL.", installed, None)
    elif not BRANCH_RE.fullmatch(branch) or branch.startswith("-") or ".." in branch:
        result = _result("error", "The update branch name is invalid.", installed, None)
    else:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-c", "protocol.file.allow=never", "ls-remote", "--", repository, f"refs/heads/{branch}"],
                capture_output=True, text=True, timeout=timeout, check=False, env=environment,
            )
            first = completed.stdout.split()
            available = first[0] if first and COMMIT_RE.fullmatch(first[0]) else None
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                result = _result("error", f"Could not reach GitHub: {detail[-1] if detail else 'git failed'}", installed, None)
            elif available is None:
                result = _result("error", f"Branch {branch} was not found on {repository}.", installed, None)
            elif installed and (available == installed or (len(installed) >= 7 and available.startswith(installed))):
                result = _result("current", "WebManager is current.", installed, available)
            elif installed:
                result = _result("available", "An update is available and waiting for super-admin approval.", installed, available)
            else:
                result = _result("error", "The installed version is unknown, so it can't be compared with GitHub.", installed, available)
        except FileNotFoundError:
            result = _result("error", "Git is not installed on this server.", installed, None)
        except subprocess.TimeoutExpired:
            result = _result("error", f"GitHub didn't answer within {timeout} seconds.", installed, None)
    with _live_lock:
        _live_status = result
    return result


def run_live_check(app, wait=False, timeout=20):
    """Check GitHub now. In the background by default so a page load never
    waits on the network; returns the result when wait=True."""
    with app.app_context():
        repository, branch = update_source()
        installed = installed_commit()
        if not installed:
            file_status = _read_file_status()
            installed = (file_status or {}).get("installed_commit")
    if wait:
        return check_upstream(repository, branch, installed, timeout)
    if _live_running.is_set():
        return None
    _live_running.set()

    def worker():
        try:
            check_upstream(repository, branch, installed, timeout)
        finally:
            _live_running.clear()

    threading.Thread(target=worker, name="webmanager-update-check", daemon=True).start()
    return None


def live_checks_enabled():
    config = current_app.config
    return bool(config.get("LIVE_UPDATE_CHECK", not config.get("TESTING")))


def updater_active():
    """Whether the systemd path unit that installs approved updates (and
    answers check requests) is running: True, False, or None if unknown."""
    if "UPDATER_ACTIVE" in current_app.config:
        return current_app.config["UPDATER_ACTIVE"]
    if current_app.config.get("TESTING"):
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "webmanager-update.path"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() == "active"


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


def request_program_update_check():
    path = Path(current_app.config["PROGRAM_UPDATE_CHECK_REQUEST_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".check.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write("check\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
