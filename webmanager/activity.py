"""Activity log: who did what, and what WebManager did by itself.

Every change someone makes through the dashboard is recorded automatically
(one hook, not a call in every view): the person, what they did, and the
message they were shown, so "who stopped that site?" and "when did this start
failing?" have answers. Background work (the watchdog, backups, updates)
records its own entries with ``record_system``.
"""

from flask import g, request, session
from flask.globals import request_ctx

from .db import get_db

KEEP_ROWS = 5000
SYSTEM = "WebManager"

# Endpoint -> what it means in words. Anything missing falls back to a
# readable form of the endpoint's name, so new actions are never unlogged.
LABELS = {
    "deployments.inspect_repository": "Connected a Git source",
    "deployments.refresh_repository": "Checked a source for updates",
    "deployments.force_repository_update": "Forced a source update",
    "deployments.schedule_repository_refresh": "Changed a source's update schedule",
    "deployments.approve_repository_update": "Approved a source update",
    "deployments.discard_repository_update": "Discarded a source update",
    "deployments.deploy_repository": "Deployed sites",
    "deployments.deploy_app": "Deployed an app",
    "deployments.app_variables": "Changed an app's variables",
    "deployments.app_resources": "Changed an app's resources",
    "deployments.site_settings": "Changed site settings",
    "deployments.start_site": "Started a site",
    "deployments.stop_site": "Stopped a site",
    "deployments.restart_site": "Restarted a site",
    "deployments.edit_config": "Edited a site's Nginx configuration",
    "deployments.delete_site": "Deleted a site",
    "deployments.delete_repository": "Deleted a Git source",
    "deployments.repository_hook": "Changed a source's deploy hook",
    "admin.install_program_update": "Approved a WebManager update",
    "admin.set_program_auto_install": "Changed automatic WebManager updates",
    "admin.check_program_update": "Checked for WebManager updates",
    "admin.check_all_sources": "Checked all sources for updates",
    "admin.enable_server_sharing": "Turned on server sharing",
    "admin.add_server": "Added a server",
    "admin.remove_server": "Removed a server",
    "admin.update_server": "Updated another server",
    "admin.control_server": "Changed another server's settings",
    "admin.acknowledge_failover": "Confirmed the failover was checked",
    "admin.set_app_hosting": "Turned app hosting on or off",
    "admin.promote_replica": "Promoted this server to primary",
    "admin.create_backup": "Made a backup",
    "admin.save_notifications": "Changed notification settings",
    "admin.test_notification": "Sent a test notification",
    "admin.create_domain": "Added a domain",
    "admin.delete_domain": "Removed a domain",
    "admin.set_default_domain": "Changed the default domain",
    "admin.reset_default_domain": "Cleared the default domain",
    "admin.update_domain_blocklist": "Changed the domain blocklist",
    "admin.create_dashboard_domain": "Added a dashboard address",
    "admin.delete_dashboard_domain": "Removed a dashboard address",
    "admin.set_primary_dashboard_domain": "Changed the main dashboard address",
    "admin.create_group": "Created a team",
    "admin.update_group": "Changed a team",
    "admin.delete_group": "Deleted a team",
    "admin.create_pool": "Created a pool",
    "admin.update_pool": "Changed a pool",
    "admin.delete_pool": "Deleted a pool",
    "admin.update_site_access": "Changed who can access a site",
    "admin.update_user": "Changed a person's account",
    "admin.delete_user": "Deleted a person's account",
    "admin.create_plan": "Added a plan",
    "admin.update_plan": "Changed a plan",
    "admin.default_plan": "Changed the default plan",
    "admin.delete_plan": "Deleted a plan",
    "deployments.deploy_choice": "Deployed a site or app",
    "deployments.account": "Changed their own name",
    "admin.save_settings": "Changed WebManager's settings",
    "admin.update_default_limits": "Changed the default limits",
    "admin.sync_replication": "Synced settings from the primary",
    "admin.sync_data_replication": "Synced data from the primary",
    "auth.logout": "Signed out",
}
SKIPPED_BLUEPRINTS = {"mesh", "replication", "data_replication", "hooks"}


def _label(endpoint: str) -> str:
    if endpoint in LABELS:
        return LABELS[endpoint]
    name = endpoint.rsplit(".", 1)[-1].replace("_", " ")
    return name[:1].upper() + name[1:]


def record(database, action, detail=None, *, user=None, path=None, ok=True):
    """Add one entry (and commit). ``user`` is a users row or None for WebManager itself."""
    actor = SYSTEM
    user_id = None
    if user is not None:
        user_id = user["id"]
        actor = user["display_name"] or user["email"] or user["username"]
    database.execute(
        "INSERT INTO activity (user_id, actor, action, detail, path, ok) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, actor, action, (detail or "")[:1000] or None, (path or "")[:300] or None, int(ok)),
    )
    database.execute(
        "DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - ?", (KEEP_ROWS,)
    )
    database.commit()


def record_system(app, action, detail=None, ok=True):
    """An entry for something WebManager did by itself. Never raises, and does
    nothing on a replica (its database is a copy of the primary's)."""
    manager = app.extensions.get("replication_manager")
    if manager is not None and manager.is_replica:
        return
    try:
        with app.app_context():
            record(get_db(), action, detail, ok=ok)
    except Exception:  # noqa: BLE001 - logging must never break the work it describes
        app.logger.exception("Could not record activity: %s", action)


def register(app):
    @app.after_request
    def _record_changes(response):
        try:
            _after(app, response)
        except Exception:  # noqa: BLE001 - never turn a finished request into an error
            app.logger.exception("Could not record activity for %s", request.path)
        return response


def _after(app, response):
    if request.method in ("GET", "HEAD", "OPTIONS") or not request.endpoint:
        return
    if request.blueprint in SKIPPED_BLUEPRINTS or request.endpoint == "static":
        return
    user = getattr(g, "user", None)
    if user is None:
        return
    manager = app.extensions.get("replication_manager")
    if manager is not None and manager.is_replica:
        return  # the primary handled (and logged) the forwarded request
    # The messages this request showed, or is about to show after its redirect.
    shown = getattr(request_ctx, "flashes", None) or session.get("_flashes") or []
    messages = [message for _category, message in shown]
    failed = response.status_code >= 400 or any(category == "error" for category, _ in shown)
    record(
        get_db(),
        _label(request.endpoint),
        " ".join(messages) or None,
        user=user,
        path=request.path,
        ok=not failed,
    )


def recent(database, limit=100, offset=0, query=""):
    where, parameters = "", []
    if query:
        where = "WHERE actor LIKE ? OR action LIKE ? OR detail LIKE ? OR path LIKE ?"
        like = f"%{query}%"
        parameters = [like, like, like, like]
    rows = database.execute(
        f"SELECT * FROM activity {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*parameters, limit + 1, offset),
    ).fetchall()
    return rows[:limit], len(rows) > limit


def record_sign_in(app, user_id):
    manager = app.extensions.get("replication_manager")
    if manager is not None and manager.is_replica:
        return
    try:
        database = get_db()
        user = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is not None:
            record(database, "Signed in", user=user, path=request.path)
    except Exception:  # noqa: BLE001
        app.logger.exception("Could not record the sign-in")
