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
from .domains import (
    blocked_domain_names,
    dashboard_hostname,
    domain_is_dashboard,
    domain_is_blocked,
    normalize_domain,
    site_hostnames,
)
from .security import login_required, validate_csrf
from .services import RuntimeErrorDetail
from .update_status import (
    read_update_status,
    request_program_update,
    request_program_update_check,
)


bp = Blueprint("admin", __name__, url_prefix="/admin")
GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,59}$")
ACCESS_ROLES = {"", "viewer", "operator"}
PERMISSION_PROFILES = {
    "team": {
        "name": "Team only",
        "description": "No platform-wide access. Use pools to grant site access.",
        "permissions": set(),
    },
    "viewer": {
        "name": "All-site viewer",
        "description": "Can inspect every repository and site, but cannot change them.",
        "permissions": {"resources.view_all"},
    },
    "operator": {
        "name": "All-site manager",
        "description": "Can inspect and operate every repository and site.",
        "permissions": {"resources.view_all", "resources.manage_all"},
    },
    "access_admin": {
        "name": "Access administrator",
        "description": "Can manage pools and grants without managing accounts.",
        "permissions": {"access.manage"},
    },
    "account_admin": {
        "name": "Account administrator",
        "description": "Can manage people and teams.",
        "permissions": {"users.manage", "groups.manage"},
    },
    "delegated_admin": {
        "name": "Delegated administrator",
        "description": "Can manage all sites, people, teams, pools, and grants.",
        "permissions": {
            "resources.view_all",
            "resources.manage_all",
            "users.manage",
            "groups.manage",
            "access.manage",
        },
    },
}


def _permission_codes(database):
    return {
        row["code"]
        for row in database.execute("SELECT code FROM permissions").fetchall()
    }


def _selected_permissions(database):
    profile = request.form.get("permission_profile", "").strip()
    if profile and profile != "custom":
        if profile not in PERMISSION_PROFILES:
            abort(400, "Unknown access profile.")
        selected = set(PERMISSION_PROFILES[profile]["permissions"])
    else:
        selected = set(request.form.getlist("permissions"))
    if not selected <= _permission_codes(database):
        abort(400, "Unknown permission.")
    if not is_admin() and not selected <= g.permissions:
        abort(403)
    return selected


def _permission_profile(permission_codes):
    codes = set(permission_codes)
    for key, profile in PERMISSION_PROFILES.items():
        if codes == profile["permissions"]:
            return key
    return "custom"


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
    can_manage_users = has_permission(USERS_MANAGE)
    can_manage_groups = has_permission(GROUPS_MANAGE)
    can_manage_access = has_permission(ACCESS_MANAGE)
    super_admin = is_admin()
    available_sections = {"overview"}
    if can_manage_users:
        available_sections.add("people")
    if can_manage_groups:
        available_sections.add("teams")
    if super_admin:
        available_sections.add("updates")
    requested_section = request.args.get("section", "overview")
    active_section = {
        "users": "people",
        "groups": "teams",
    }.get(requested_section, requested_section)
    if active_section not in available_sections:
        active_section = "overview"
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
    group_profiles = {
        group_id: _permission_profile(codes)
        for group_id, codes in group_permissions.items()
    }
    permissions = database.execute(
        "SELECT * FROM permissions ORDER BY name"
    ).fetchall()
    counts = {
        "users": len(users),
        "active_users": sum(bool(user["is_active"]) for user in users),
        "groups": len(groups),
        "pools": database.execute("SELECT COUNT(*) AS count FROM pools").fetchone()[
            "count"
        ],
        "sites": database.execute("SELECT COUNT(*) AS count FROM sites").fetchone()[
            "count"
        ],
        "domains": database.execute("SELECT COUNT(*) AS count FROM domains").fetchone()[
            "count"
        ],
    }
    return render_template(
        "admin/dashboard.html",
        title="Admin console",
        active_section=active_section,
        counts=counts,
        users=users,
        groups=groups,
        memberships=memberships,
        group_permissions=group_permissions,
        group_profiles=group_profiles,
        permission_profiles=PERMISSION_PROFILES,
        permissions=permissions,
        can_manage_users=can_manage_users,
        can_manage_groups=can_manage_groups,
        can_manage_access=can_manage_access,
        super_admin=super_admin,
        update_status=read_update_status() if super_admin else None,
        google_access_unrestricted=not (
            current_app.config["GOOGLE_ALLOWED_DOMAINS"]
            or current_app.config["GOOGLE_ALLOWED_EMAILS"]
        ),
    )


@bp.get("/domains")
@login_required
def domains_dashboard():
    if not is_admin():
        abort(403)
    database = get_db()
    domain_rows = database.execute(
        """
        SELECT domains.*,
               COUNT(DISTINCT primary_sites.id)
               + COUNT(DISTINCT alias_sites.site_id) AS site_count
        FROM domains
        LEFT JOIN sites AS primary_sites
          ON primary_sites.domain_id = domains.id
        LEFT JOIN site_domain_aliases AS alias_sites
          ON alias_sites.domain_id = domains.id
        GROUP BY domains.id
        ORDER BY domains.is_default DESC, domains.name COLLATE NOCASE
        """
    ).fetchall()
    blocked = blocked_domain_names(database)
    dashboard_domain_rows = database.execute(
        """
        SELECT * FROM dashboard_domains
        ORDER BY is_primary DESC, name COLLATE NOCASE
        """
    ).fetchall()
    domains = [
        {
            **dict(domain),
            "is_blocked": domain_is_blocked(domain["name"], blocked),
            "is_dashboard": domain_is_dashboard(database, domain["name"]),
        }
        for domain in domain_rows
    ]
    return render_template(
        "admin/domains.html",
        title="Domains",
        active_section="domains",
        domains=domains,
        dashboard_domains=dashboard_domain_rows,
        blocked_domains=sorted(blocked),
        dashboard_host=dashboard_hostname(),
        can_manage_users=has_permission(USERS_MANAGE),
        can_manage_groups=has_permission(GROUPS_MANAGE),
        can_manage_access=has_permission(ACCESS_MANAGE),
        super_admin=True,
    )


def _apply_dashboard_domain_change(database, success_message):
    try:
        current_app.extensions["runtime_manager"].apply_nginx_configs()
    except RuntimeErrorDetail as exc:
        database.rollback()
        try:
            current_app.extensions["runtime_manager"].apply_nginx_configs()
        except RuntimeErrorDetail:
            pass
        flash(f"Dashboard domain change was not applied: {exc}", "error")
    else:
        database.commit()
        flash(success_message, "success")
    return redirect(url_for("admin.domains_dashboard"))


@bp.post("/domains/dashboard")
@login_required
def create_dashboard_domain():
    validate_csrf()
    if not is_admin():
        abort(403)
    try:
        name = normalize_domain(request.form.get("name", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.domains_dashboard"))
    database = get_db()
    assigned_hostnames = {
        hostname
        for site in database.execute("SELECT * FROM sites").fetchall()
        for hostname in site_hostnames(database, site)
    }
    if name in assigned_hostnames:
        flash("That hostname is already assigned to a hosted site.", "error")
        return redirect(url_for("admin.domains_dashboard"))
    make_primary = request.form.get("is_primary") == "on"
    has_primary = database.execute(
        "SELECT 1 FROM dashboard_domains WHERE is_primary = 1"
    ).fetchone()
    if make_primary or has_primary is None:
        database.execute("UPDATE dashboard_domains SET is_primary = 0")
        make_primary = True
    try:
        database.execute(
            "INSERT INTO dashboard_domains (name, is_primary) VALUES (?, ?)",
            (name, int(make_primary)),
        )
    except sqlite3.IntegrityError:
        database.rollback()
        flash("That dashboard domain already exists.", "error")
        return redirect(url_for("admin.domains_dashboard"))
    return _apply_dashboard_domain_change(
        database,
        f"Dashboard domain {name} added.",
    )


@bp.post("/domains/dashboard/<int:domain_id>/primary")
@login_required
def set_primary_dashboard_domain(domain_id):
    validate_csrf()
    if not is_admin():
        abort(403)
    database = get_db()
    domain = database.execute(
        "SELECT * FROM dashboard_domains WHERE id = ?",
        (domain_id,),
    ).fetchone()
    if domain is None:
        abort(404)
    database.execute("UPDATE dashboard_domains SET is_primary = 0")
    database.execute(
        "UPDATE dashboard_domains SET is_primary = 1 WHERE id = ?",
        (domain_id,),
    )
    return _apply_dashboard_domain_change(
        database,
        f"{domain['name']} is now the primary dashboard domain.",
    )


@bp.post("/domains/dashboard/<int:domain_id>/delete")
@login_required
def delete_dashboard_domain(domain_id):
    validate_csrf()
    if not is_admin():
        abort(403)
    database = get_db()
    domain = database.execute(
        "SELECT * FROM dashboard_domains WHERE id = ?",
        (domain_id,),
    ).fetchone()
    if domain is None:
        abort(404)
    count = database.execute(
        "SELECT COUNT(*) AS count FROM dashboard_domains"
    ).fetchone()["count"]
    if count <= 1:
        flash("At least one dashboard domain must remain configured.", "error")
        return redirect(url_for("admin.domains_dashboard"))
    if domain["is_primary"]:
        flash("Make another dashboard domain primary before deleting this one.", "error")
        return redirect(url_for("admin.domains_dashboard"))
    database.execute("DELETE FROM dashboard_domains WHERE id = ?", (domain_id,))
    return _apply_dashboard_domain_change(
        database,
        f"Dashboard domain {domain['name']} removed.",
    )


@bp.post("/domains")
@login_required
def create_domain():
    validate_csrf()
    if not is_admin():
        abort(403)
    try:
        name = normalize_domain(request.form.get("name", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.domains_dashboard"))
    database = get_db()
    blocked = blocked_domain_names(database)
    if domain_is_blocked(name, blocked):
        flash(
            f"{name} is blocked by the deployment-domain blocklist.",
            "error",
        )
        return redirect(url_for("admin.domains_dashboard"))
    make_default = request.form.get("is_default") == "on"
    try:
        has_domains = database.execute("SELECT 1 FROM domains LIMIT 1").fetchone()
        current_default = database.execute(
            "SELECT name FROM domains WHERE is_default = 1"
        ).fetchone()
        should_make_default = make_default or has_domains is None
        if current_default and domain_is_blocked(current_default["name"], blocked):
            should_make_default = True
        if should_make_default:
            database.execute("UPDATE domains SET is_default = 0")
            make_default = True
        database.execute(
            "INSERT INTO domains (name, is_default) VALUES (?, ?)",
            (name, int(make_default)),
        )
        database.commit()
    except sqlite3.IntegrityError:
        database.rollback()
        flash("That domain already exists.", "error")
    else:
        flash(f"Domain {name} added.", "success")
    return redirect(url_for("admin.domains_dashboard"))


@bp.post("/domains/<int:domain_id>/default")
@login_required
def set_default_domain(domain_id):
    validate_csrf()
    if not is_admin():
        abort(403)
    database = get_db()
    domain = database.execute(
        "SELECT * FROM domains WHERE id = ?",
        (domain_id,),
    ).fetchone()
    if domain is None:
        abort(404)
    if domain_is_blocked(domain["name"], blocked_domain_names(database)):
        flash("A blocked domain cannot be the default.", "error")
        return redirect(url_for("admin.domains_dashboard"))
    database.execute("UPDATE domains SET is_default = 0")
    database.execute(
        "UPDATE domains SET is_default = 1 WHERE id = ?",
        (domain_id,),
    )
    database.commit()
    flash(f"{domain['name']} is now the default deployment domain.", "success")
    return redirect(url_for("admin.domains_dashboard"))


@bp.post("/domains/default/reset")
@login_required
def reset_default_domain():
    validate_csrf()
    if not is_admin():
        abort(403)
    database = get_db()
    previous = database.execute(
        "SELECT name FROM domains WHERE is_default = 1"
    ).fetchone()
    database.execute("UPDATE domains SET is_default = 0")
    database.execute(
        """
        INSERT OR REPLACE INTO app_settings (key, value)
        VALUES ('domain_defaults_initialized', '1')
        """
    )
    database.commit()
    if previous:
        flash(
            f"Default deployment domain cleared. {previous['name']} remains available.",
            "success",
        )
    else:
        flash("No default deployment domain is currently set.", "warning")
    return redirect(url_for("admin.domains_dashboard"))


@bp.post("/domains/<int:domain_id>/delete")
@login_required
def delete_domain(domain_id):
    validate_csrf()
    if not is_admin():
        abort(403)
    database = get_db()
    domain = database.execute(
        """
        SELECT domains.*,
               COUNT(DISTINCT primary_sites.id)
               + COUNT(DISTINCT alias_sites.site_id) AS site_count
        FROM domains
        LEFT JOIN sites AS primary_sites
          ON primary_sites.domain_id = domains.id
        LEFT JOIN site_domain_aliases AS alias_sites
          ON alias_sites.domain_id = domains.id
        WHERE domains.id = ?
        GROUP BY domains.id
        """,
        (domain_id,),
    ).fetchone()
    if domain is None:
        abort(404)
    if domain["site_count"]:
        flash(
            f"Move the {domain['site_count']} assigned site(s) before deleting this domain.",
            "error",
        )
        return redirect(url_for("admin.domains_dashboard"))
    database.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    if domain["is_default"]:
        blocked = blocked_domain_names(database)
        replacement = next(
            (
                row
                for row in database.execute(
                    "SELECT id, name FROM domains ORDER BY id"
                ).fetchall()
                if not domain_is_blocked(row["name"], blocked)
            ),
            None,
        )
        if replacement:
            database.execute(
                "UPDATE domains SET is_default = 1 WHERE id = ?",
                (replacement["id"],),
            )
    database.commit()
    flash(f"Domain {domain['name']} removed.", "success")
    return redirect(url_for("admin.domains_dashboard"))


@bp.post("/domains/blocklist")
@login_required
def update_domain_blocklist():
    validate_csrf()
    if not is_admin():
        abort(403)
    raw_entries = re.split(r"[\s,]+", request.form.get("blocked_domains", ""))
    try:
        blocked = {
            normalize_domain(value)
            for value in raw_entries
            if value.strip()
        }
    except ValueError as exc:
        flash(f"Blocklist not saved: {exc}", "error")
        return redirect(url_for("admin.domains_dashboard"))

    database = get_db()
    database.execute("DELETE FROM blocked_domains")
    database.executemany(
        "INSERT INTO blocked_domains (name) VALUES (?)",
        ((name,) for name in sorted(blocked)),
    )
    default = database.execute(
        "SELECT id, name FROM domains WHERE is_default = 1"
    ).fetchone()
    if default and domain_is_blocked(default["name"], blocked):
        database.execute("UPDATE domains SET is_default = 0")
        replacement = next(
            (
                row
                for row in database.execute(
                    "SELECT id, name FROM domains ORDER BY id"
                ).fetchall()
                if not domain_is_blocked(row["name"], blocked)
            ),
            None,
        )
        if replacement:
            database.execute(
                "UPDATE domains SET is_default = 1 WHERE id = ?",
                (replacement["id"],),
            )
    database.commit()
    flash(
        f"Domain blocklist saved with {len(blocked)} entr{'y' if len(blocked) == 1 else 'ies'}.",
        "success",
    )
    return redirect(url_for("admin.domains_dashboard"))


@bp.get("/access")
@login_required
@admin_access_required(ACCESS_MANAGE)
def access_dashboard():
    database = get_db()
    active_section = request.args.get("section", "pools")
    if active_section not in {"pools", "sites"}:
        active_section = "pools"
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
        title="Access control",
        active_section=active_section,
        users=users,
        groups=groups,
        pools=pools,
        sites=sites,
        pool_acl=pool_acl,
        site_acl=site_acl,
        can_manage_users=has_permission(USERS_MANAGE),
        can_manage_groups=has_permission(GROUPS_MANAGE),
        can_manage_access=True,
        super_admin=is_admin(),
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
        return redirect(url_for("admin.access_dashboard", section="pools"))
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
    return redirect(url_for("admin.access_dashboard", section="pools"))


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
        return redirect(url_for("admin.access_dashboard", section="pools"))
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
    return redirect(url_for("admin.access_dashboard", section="pools"))


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
    return redirect(url_for("admin.access_dashboard", section="pools"))


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
    return redirect(url_for("admin.access_dashboard", section="sites"))


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
        return redirect(url_for("admin.dashboard", section="updates"))

    try:
        request_program_update(commit)
    except (OSError, ValueError) as exc:
        flash(f"Could not request the update: {exc}", "error")
    else:
        flash(
            "Update approved. WebManager will test, back up all data, and install it.",
            "success",
        )
    return redirect(url_for("admin.dashboard", section="updates"))


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
    return redirect(url_for("admin.dashboard", section="updates"))


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
        return redirect(url_for("admin.dashboard", section="people"))

    if user["is_admin"] and (not admin or not active):
        active_admins = database.execute(
            "SELECT COUNT(*) AS count FROM users WHERE is_admin = 1 AND is_active = 1"
        ).fetchone()["count"]
        if active_admins <= 1:
            flash("The final active administrator cannot be disabled or demoted.", "error")
            return redirect(url_for("admin.dashboard", section="people"))

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
        abort(400, "Unknown team.")
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
    return redirect(url_for("admin.dashboard", section="people"))


@bp.post("/groups")
@login_required
@admin_access_required(GROUPS_MANAGE)
def create_group():
    validate_csrf()
    database = get_db()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not GROUP_NAME_RE.fullmatch(name):
        flash("Team names must be 2-60 letters, numbers, spaces, dots, dashes, or underscores.", "error")
        return redirect(url_for("admin.dashboard", section="teams"))
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
        flash("A team with that name already exists.", "error")
    else:
        flash(f"Team {name} created.", "success")
    return redirect(url_for("admin.dashboard", section="teams"))


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
        flash("Enter a valid team name.", "error")
        return redirect(url_for("admin.dashboard", section="teams"))
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
        flash("A team with that name already exists.", "error")
    else:
        flash(f"Team {name} updated.", "success")
    return redirect(url_for("admin.dashboard", section="teams"))


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
    flash(f"Team {group['name']} deleted.", "success")
    return redirect(url_for("admin.dashboard", section="teams"))
