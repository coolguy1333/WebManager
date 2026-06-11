from functools import wraps

from flask import abort, g


RESOURCE_VIEW_ALL = "resources.view_all"
RESOURCE_MANAGE_ALL = "resources.manage_all"
USERS_MANAGE = "users.manage"
GROUPS_MANAGE = "groups.manage"


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
