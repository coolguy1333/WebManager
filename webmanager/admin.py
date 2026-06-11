import re
import sqlite3

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .access_control import (
    ACCESS_MANAGE,
    GROUPS_MANAGE,
    USERS_MANAGE,
    admin_access_required,
    has_permission,
    is_admin,
)
from .db import get_db
from .security import login_required, validate_csrf
from .update_status import (
    read_update_status,
    request_program_update,
    request_program_update_check,
)


bp = Blueprint("admin", __name__, url_prefix="/admin")
GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,59}$")
ACCESS_ROLES = {"", "viewer", "operator"}


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


def _replace_acl(database, table, scope_column, scope_id, users, groups):
    grants = []
    for subject, rows in (("user", users), ("group", groups)):
        for row in rows:
            role = request.form.get(f"{subject}_role_{row['id']}", "")
            if role not in ACCESS_ROLES:
                abort(400, "Unknown access role.")
            if role:
                grants.append(
                    (
                        scope_id,
                        row["id"] if subject == "user" else None,
                        row["id"] if subject == "group" else None,
                        role,
                    )
                )
    database.execute(f"DELETE FROM {table} WHERE {scope_column} = ?", (scope_id,))
    database.executemany(
        f"""
        INSERT INTO {table} ({scope_column}, user_id, group_id, role)
        VALUES (?, ?, ?, ?)
        """,
        grants,
    )


@bp.get("/")
@login_required
@admin_access_required(USERS_MANAGE, GROUPS_MANAGE, ACCESS_MANAGE)
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
        can_manage_access=has_permission(ACCESS_MANAGE),
        super_admin=is_admin(),
        update_status=read_update_status() if is_admin() else None,
        google_access_unrestricted=not (
            current_app.config["GOOGLE_ALLOWED_DOMAINS"]
            or current_app.config["GOOGLE_ALLOWED_EMAILS"]
        ),
    )


@bp.get("/access")
@login_required
@admin_access_required(ACCESS_MANAGE)
def access_dashboard():
    database = get_db()
    users = database.execute(
        """
        SELECT id, username, email, display_name, is_active
        FROM users
        ORDER BY COALESCE(display_name, email, username) COLLATE NOCASE
        """
    ).fetchall()
    groups = database.execute(
        "SELECT id, name FROM groups ORDER BY name COLLATE NOCASE"
    ).fetchall()
    pools = database.execute(
        """
        SELECT pools.*, COUNT(pool_sites.site_id) AS site_count
        FROM pools
        LEFT JOIN pool_sites ON pool_sites.pool_id = pools.id
        GROUP BY pools.id
        ORDER BY pools.name COLLATE NOCASE
        """
    ).fetchall()
    sites = database.execute(
        """
        SELECT sites.id, sites.name, sites.slug,
               users.display_name AS owner_name, users.email AS owner_email,
               pools.id AS pool_id, pools.name AS pool_name
        FROM sites
        JOIN users ON users.id = sites.user_id
        LEFT JOIN pool_sites ON pool_sites.site_id = sites.id
        LEFT JOIN pools ON pools.id = pool_sites.pool_id
        ORDER BY sites.name COLLATE NOCASE
        """
    ).fetchall()
    pool_acl = {
        (row["pool_id"], "user" if row["user_id"] is not None else "group",
         row["user_id"] if row["user_id"] is not None else row["group_id"]): row["role"]
        for row in database.execute("SELECT * FROM pool_acl").fetchall()
    }
    site_acl = {
        (row["site_id"], "user" if row["user_id"] is not None else "group",
         row["user_id"] if row["user_id"] is not None else row["group_id"]): row["role"]
        for row in database.execute("SELECT * FROM site_acl").fetchall()
    }
    return render_template(
        "admin/access.html",
        title="Pools and access",
        users=users,
        groups=groups,
        pools=pools,
        sites=sites,
        pool_acl=pool_acl,
        site_acl=site_acl,
    )


@bp.post("/pools")
@login_required
@admin_access_required(ACCESS_MANAGE)
def create_pool():
    validate_csrf()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not GROUP_NAME_RE.fullmatch(name):
        flash("Pool names must be 2-60 letters, numbers, spaces, dots, dashes, or underscores.", "error")
        return redirect(url_for("admin.access_dashboard"))
    try:
        database = get_db()
        database.execute(
            "INSERT INTO pools (name, description) VALUES (?, ?)",
            (name, description[:300] or None),
        )
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("A pool with that name already exists.", "error")
    else:
        flash(f"Pool {name} created.", "success")
    return redirect(url_for("admin.access_dashboard"))


@bp.post("/pools/<int:pool_id>")
@login_required
@admin_access_required(ACCESS_MANAGE)
def update_pool(pool_id):
    validate_csrf()
    database = get_db()
    pool = database.execute("SELECT * FROM pools WHERE id = ?", (pool_id,)).fetchone()
    if pool is None:
        abort(404)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not GROUP_NAME_RE.fullmatch(name):
        flash("Enter a valid pool name.", "error")
        return redirect(url_for("admin.access_dashboard"))
    users = database.execute("SELECT id FROM users").fetchall()
    groups = database.execute("SELECT id FROM groups").fetchall()
    valid_site_ids = {
        row["id"] for row in database.execute("SELECT id FROM sites").fetchall()
    }
    site_ids = {
        int(value) for value in request.form.getlist("sites") if value.isdigit()
    }
    if not site_ids <= valid_site_ids:
        abort(400, "Unknown site.")
    try:
        database.execute(
            """
            UPDATE pools
            SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, description[:300] or None, pool_id),
        )
        database.execute("DELETE FROM pool_sites WHERE pool_id = ?", (pool_id,))
        if site_ids:
            placeholders = ",".join("?" for _ in site_ids)
            database.execute(
                f"DELETE FROM pool_sites WHERE site_id IN ({placeholders})",
                tuple(sorted(site_ids)),
            )
            database.executemany(
                "INSERT INTO pool_sites (pool_id, site_id) VALUES (?, ?)",
                ((pool_id, site_id) for site_id in sorted(site_ids)),
            )
        _replace_acl(database, "pool_acl", "pool_id", pool_id, users, groups)
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("That pool name or assignment conflicts with an existing pool.", "error")
    else:
        flash(f"Pool {name} updated.", "success")
    return redirect(url_for("admin.access_dashboard"))


@bp.post("/pools/<int:pool_id>/delete")
@login_required
@admin_access_required(ACCESS_MANAGE)
def delete_pool(pool_id):
    validate_csrf()
    database = get_db()
    pool = database.execute("SELECT * FROM pools WHERE id = ?", (pool_id,)).fetchone()
    if pool is None:
        abort(404)
    database.execute("DELETE FROM pools WHERE id = ?", (pool_id,))
    database.commit()
    flash(f"Pool {pool['name']} deleted. Its sites were not deleted.", "success")
    return redirect(url_for("admin.access_dashboard"))


@bp.post("/sites/<int:site_id>/access")
@login_required
@admin_access_required(ACCESS_MANAGE)
def update_site_access(site_id):
    validate_csrf()
    database = get_db()
    site = database.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    if site is None:
        abort(404)
    users = database.execute("SELECT id FROM users").fetchall()
    groups = database.execute("SELECT id FROM groups").fetchall()
    _replace_acl(database, "site_acl", "site_id", site_id, users, groups)
    database.commit()
    flash(f"Direct access for {site['name']} updated.", "success")
    return redirect(url_for("admin.access_dashboard"))


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


@bp.post("/updates/check")
@login_required
def check_program_update():
    validate_csrf()
    if not is_admin():
        abort(403)

    try:
        request_program_update_check()
    except OSError as exc:
        flash(f"Could not request an update check: {exc}", "error")
    else:
        flash(
            "Update check requested. Refresh this page in a few seconds.",
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
