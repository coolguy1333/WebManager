import re
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .access_control import (
    GROUPS_MANAGE,
    USERS_MANAGE,
    admin_access_required,
    has_permission,
    is_admin,
)
from .db import get_db
from .security import login_required, validate_csrf
from .update_status import read_update_status, request_program_update


bp = Blueprint("admin", __name__, url_prefix="/admin")
GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,59}$")


def _permission_codes(database):
    return {
        row["code"]
        for row in database.execute("SELECT code FROM permissions").fetchall()
    }


def _selected_permissions(database):
    selected = set(request.form.getlist("permissions"))
    if not selected <= _permission_codes(database):
        abort(400, "Unknown permission.")
    if not is_admin() and not selected <= g.permissions:
        abort(403)
    return selected


@bp.get("/")
@login_required
@admin_access_required(USERS_MANAGE, GROUPS_MANAGE)
def dashboard():
    database = get_db()
    users = database.execute(
        """
        SELECT users.*,
               GROUP_CONCAT(groups.name, ', ') AS group_names
        FROM users
        LEFT JOIN user_groups ON user_groups.user_id = users.id
        LEFT JOIN groups ON groups.id = user_groups.group_id
        GROUP BY users.id
        ORDER BY users.created_at, users.id
        """
    ).fetchall()
    groups = database.execute(
        """
        SELECT groups.*, COUNT(DISTINCT user_groups.user_id) AS member_count
        FROM groups
        LEFT JOIN user_groups ON user_groups.group_id = groups.id
        GROUP BY groups.id
        ORDER BY groups.name COLLATE NOCASE
        """
    ).fetchall()
    memberships = {
        row["user_id"]: set()
        for row in database.execute("SELECT id AS user_id FROM users").fetchall()
    }
    for row in database.execute("SELECT user_id, group_id FROM user_groups").fetchall():
        memberships.setdefault(row["user_id"], set()).add(row["group_id"])
    group_permissions = {group["id"]: set() for group in groups}
    for row in database.execute(
        "SELECT group_id, permission_code FROM group_permissions"
    ).fetchall():
        group_permissions.setdefault(row["group_id"], set()).add(
            row["permission_code"]
        )
    permissions = database.execute(
        "SELECT * FROM permissions ORDER BY name"
    ).fetchall()
    return render_template(
        "admin/dashboard.html",
        title="Administration",
        users=users,
        groups=groups,
        memberships=memberships,
        group_permissions=group_permissions,
        permissions=permissions,
        can_manage_users=has_permission(USERS_MANAGE),
        can_manage_groups=has_permission(GROUPS_MANAGE),
        super_admin=is_admin(),
        update_status=read_update_status() if is_admin() else None,
    )


@bp.post("/updates/install")
@login_required
def install_program_update():
    validate_csrf()
    if not is_admin():
        abort(403)

    status = read_update_status()
    commit = request.form.get("commit", "").strip()
    if (
        not status["update_available"]
        or status["available_commit"] != commit
        or status["state"] != "available"
    ):
        flash("That update is no longer available. Wait for the next check.", "error")
        return redirect(url_for("admin.dashboard"))

    try:
        request_program_update(commit)
    except (OSError, ValueError) as exc:
        flash(f"Could not request the update: {exc}", "error")
    else:
        flash(
            "Update approved. WebManager will test, back up all data, and install it.",
            "success",
        )
    return redirect(url_for("admin.dashboard"))


@bp.post("/users/<int:user_id>")
@login_required
@admin_access_required(USERS_MANAGE)
def update_user(user_id):
    validate_csrf()
    database = get_db()
    user = database.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None:
        abort(404)
    if user["is_admin"] and not is_admin():
        abort(403)

    active = request.form.get("is_active") == "on"
    admin = request.form.get("is_admin") == "on" if is_admin() else bool(user["is_admin"])
    if user_id == g.user["id"] and not active:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("admin.dashboard"))

    if user["is_admin"] and (not admin or not active):
        active_admins = database.execute(
            "SELECT COUNT(*) AS count FROM users WHERE is_admin = 1 AND is_active = 1"
        ).fetchone()["count"]
        if active_admins <= 1:
            flash("The final active administrator cannot be disabled or demoted.", "error")
            return redirect(url_for("admin.dashboard"))

    group_ids = {
        int(value)
        for value in request.form.getlist("groups")
        if value.isdigit()
    }
    valid_group_ids = {
        row["id"]
        for row in database.execute("SELECT id FROM groups").fetchall()
    }
    if not group_ids <= valid_group_ids:
        abort(400, "Unknown group.")
    if not is_admin() and group_ids:
        delegated_permissions = {
            row["permission_code"]
            for row in database.execute(
                f"""
                SELECT DISTINCT permission_code
                FROM group_permissions
                WHERE group_id IN ({",".join("?" for _ in group_ids)})
                """,
                tuple(sorted(group_ids)),
            ).fetchall()
        }
        if not delegated_permissions <= g.permissions:
            abort(403)

    database.execute(
        "UPDATE users SET is_active = ?, is_admin = ? WHERE id = ?",
        (int(active), int(admin), user_id),
    )
    database.execute("DELETE FROM user_groups WHERE user_id = ?", (user_id,))
    database.executemany(
        "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
        ((user_id, group_id) for group_id in sorted(group_ids)),
    )
    database.commit()
    flash(f"Updated {user['display_name'] or user['username']}.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/groups")
@login_required
@admin_access_required(GROUPS_MANAGE)
def create_group():
    validate_csrf()
    database = get_db()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not GROUP_NAME_RE.fullmatch(name):
        flash("Group names must be 2-60 letters, numbers, spaces, dots, dashes, or underscores.", "error")
        return redirect(url_for("admin.dashboard"))
    permissions = _selected_permissions(database)
    try:
        cursor = database.execute(
            "INSERT INTO groups (name, description) VALUES (?, ?)",
            (name, description[:300] or None),
        )
        database.executemany(
            "INSERT INTO group_permissions (group_id, permission_code) VALUES (?, ?)",
            ((cursor.lastrowid, code) for code in sorted(permissions)),
        )
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("A group with that name already exists.", "error")
    else:
        flash(f"Group {name} created.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/groups/<int:group_id>")
@login_required
@admin_access_required(GROUPS_MANAGE)
def update_group(group_id):
    validate_csrf()
    database = get_db()
    group = database.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if group is None:
        abort(404)
    existing_permissions = {
        row["permission_code"]
        for row in database.execute(
            "SELECT permission_code FROM group_permissions WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    }
    if not is_admin() and not existing_permissions <= g.permissions:
        abort(403)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not GROUP_NAME_RE.fullmatch(name):
        flash("Enter a valid group name.", "error")
        return redirect(url_for("admin.dashboard"))
    permissions = _selected_permissions(database)
    try:
        database.execute(
            """
            UPDATE groups
            SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, description[:300] or None, group_id),
        )
        database.execute("DELETE FROM group_permissions WHERE group_id = ?", (group_id,))
        database.executemany(
            "INSERT INTO group_permissions (group_id, permission_code) VALUES (?, ?)",
            ((group_id, code) for code in sorted(permissions)),
        )
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("A group with that name already exists.", "error")
    else:
        flash(f"Group {name} updated.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("/groups/<int:group_id>/delete")
@login_required
@admin_access_required(GROUPS_MANAGE)
def delete_group(group_id):
    validate_csrf()
    database = get_db()
    group = database.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if group is None:
        abort(404)
    existing_permissions = {
        row["permission_code"]
        for row in database.execute(
            "SELECT permission_code FROM group_permissions WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    }
    if not is_admin() and not existing_permissions <= g.permissions:
        abort(403)
    database.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    database.commit()
    flash(f"Group {group['name']} deleted.", "success")
    return redirect(url_for("admin.dashboard"))
