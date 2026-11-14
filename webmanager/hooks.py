"""Deploy hooks: a secret address per Git source that checks it for updates
the moment it is called, instead of waiting for the next scheduled check.

Add the address as a webhook on GitHub/GitLab/Gitea (any content type, the
body is ignored) or call it from a CI job with ``curl -X POST``. It only says
"look now": what happens next follows the source's own setting (install
automatically, or wait for approval), and nothing in the request is trusted.
"""

import re
import secrets
import threading
import time

from flask import Blueprint, current_app, jsonify

from . import activity
from .db import get_db

bp = Blueprint("hooks", __name__)
TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")
COOLDOWN_SECONDS = 10
_recent: dict[int, float] = {}
_recent_lock = threading.Lock()


def new_token() -> str:
    return secrets.token_hex(20)


@bp.post("/hooks/deploy/<token>")
def deploy(token):
    if not TOKEN_RE.fullmatch(token):
        return jsonify({"ok": False, "error": "Unknown deploy hook."}), 404
    repository = get_db().execute(
        "SELECT id, name FROM repositories WHERE hook_token = ?", (token,)
    ).fetchone()
    if repository is None:
        return jsonify({"ok": False, "error": "Unknown deploy hook."}), 404
    now = time.monotonic()
    with _recent_lock:
        last = _recent.get(repository["id"])
        if last is not None and now - last < COOLDOWN_SECONDS:
            return jsonify({"ok": True, "message": "A check was started a moment ago."}), 202
        _recent[repository["id"]] = now
    app = current_app._get_current_object()
    repository_id, name = repository["id"], repository["name"]

    def worker():
        result = app.extensions["repository_refresh_manager"].refresh(repository_id)
        activity.record_system(
            app, "Deploy hook checked a source", f"{name}: {result.message}",
            ok=result.status in {"applied", "current", "available", "busy"},
        )

    if app.config.get("TESTING"):
        worker()
    else:
        threading.Thread(target=worker, name="webmanager-deploy-hook", daemon=True).start()
    return jsonify({"ok": True, "message": f"Checking {name} for updates."}), 202
