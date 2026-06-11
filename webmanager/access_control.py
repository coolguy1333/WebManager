from functools import wraps

from flask import abort, g


RESOURCE_VIEW_ALL = "resources.view_all"
RESOURCE_MANAGE_ALL = "resources.manage_all"
USERS_MANAGE = "users.manage"
GROUPS_MANAGE = "groups.manage"
ACCESS_MANAGE = "access.manage"


def is_admin():
    return bool(g.user and g.user["is_admin"])


def has_permission(code):
    return bool(g.user) and (is_admin() or code in getattr(g, "permissions", set()))


def can_view_resource(owner_id):
    return bool(g.user) and (
        owner_id == g.user["id"]
        or has_permission(RESOURCE_VIEW_ALL)
        or has_permission(RESOURCE_MANAGE_ALL)
    )


def can_manage_resource(owner_id):
    return bool(g.user) and (
        owner_id == g.user["id"]
        or has_permission(RESOURCE_MANAGE_ALL)
    )


def site_access_levels(database, user_id):
    rows = database.execute(
        """
        SELECT site_id, MAX(level) AS level
        FROM (
            SELECT site_acl.site_id,
                   CASE site_acl.role WHEN 'operator' THEN 2 ELSE 1 END AS level
            FROM site_acl
            WHERE site_acl.user_id = ?

            UNION ALL

            SELECT site_acl.site_id,
                   CASE site_acl.role WHEN 'operator' THEN 2 ELSE 1 END AS level
            FROM site_acl
            JOIN user_groups ON user_groups.group_id = site_acl.group_id
            WHERE user_groups.user_id = ?

            UNION ALL

            SELECT pool_sites.site_id,
                   CASE pool_acl.role WHEN 'operator' THEN 2 ELSE 1 END AS level
            FROM pool_acl
            JOIN pool_sites ON pool_sites.pool_id = pool_acl.pool_id
            WHERE pool_acl.user_id = ?

            UNION ALL

            SELECT pool_sites.site_id,
                   CASE pool_acl.role WHEN 'operator' THEN 2 ELSE 1 END AS level
            FROM pool_acl
            JOIN user_groups ON user_groups.group_id = pool_acl.group_id
            JOIN pool_sites ON pool_sites.pool_id = pool_acl.pool_id
            WHERE user_groups.user_id = ?
        )
        GROUP BY site_id
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    return {row["site_id"]: row["level"] for row in rows}


def can_view_site(site, database=None):
    if not g.user:
        return False
    if can_view_resource(site["user_id"]):
        return True
    if database is None:
        from .db import get_db

        database = get_db()
    return site_access_levels(database, g.user["id"]).get(site["id"], 0) >= 1


def can_manage_site(site, database=None):
    if not g.user:
        return False
    if can_manage_resource(site["user_id"]):
        return True
    if database is None:
        from .db import get_db

        database = get_db()
    return site_access_levels(database, g.user["id"]).get(site["id"], 0) >= 2


def admin_access_required(*permissions):
    def decorator(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if g.user is None:
                abort(401)
            if not is_admin() and not any(has_permission(code) for code in permissions):
                abort(403)
            return view(**kwargs)

        return wrapped_view

    return decorator
