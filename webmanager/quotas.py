"""Per-user limits on how many sites and Git sources someone may own.

Defaults live in ``app_settings``; each user can have an override stored on
their row (NULL = use the default). A limit of 0 means unlimited. Super
admins are never limited.
"""

DEFAULT_MAX_SITES = 3
DEFAULT_MAX_SOURCES = 2
DEFAULT_MAX_APPS = 1
MAX_LIMIT = 10_000
KINDS = {
    "sites": ("quota_max_sites", DEFAULT_MAX_SITES, "max_sites", "sites", "site"),
    "sources": ("quota_max_sources", DEFAULT_MAX_SOURCES, "max_sources", "repositories", "source"),
    "apps": ("quota_max_apps", DEFAULT_MAX_APPS, "max_apps", "sites", "app"),
}
# Extra filter per kind (apps are sites with kind = 'app').
KIND_FILTERS = {"apps": " AND kind = 'app'"}


def get_defaults(database):
    rows = {
        row["key"]: row["value"]
        for row in database.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('quota_max_sites', 'quota_max_sources', 'quota_max_apps')"
        ).fetchall()
    }
    defaults = {}
    for kind, (key, fallback, *_rest) in KINDS.items():
        try:
            defaults[kind] = int(rows.get(key, fallback))
        except (TypeError, ValueError):
            defaults[kind] = fallback
    return defaults


def set_defaults(database, sites, sources, apps=None):
    pairs = [("quota_max_sites", sites), ("quota_max_sources", sources)]
    if apps is not None:
        pairs.append(("quota_max_apps", apps))
    for key, value in pairs:
        database.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def parse_limit(raw, allow_blank=False):
    """Parse a form value. Returns int, None (blank when allowed), or raises ValueError."""
    raw = (raw or "").strip()
    if raw == "":
        if allow_blank:
            return None
        raise ValueError("Enter a number (0 means unlimited).")
    value = int(raw)
    if value < 0 or value > MAX_LIMIT:
        raise ValueError(f"Limits must be between 0 and {MAX_LIMIT}.")
    return value


def _user_row(database, user_id):
    return database.execute(
        "SELECT id, is_admin, max_sites, max_sources, max_apps FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def limit_for(database, user_id, kind, defaults=None):
    """Effective limit, or None when unlimited."""
    user = _user_row(database, user_id)
    if user is None or user["is_admin"]:
        return None
    _key, _fallback, column, _table, _noun = KINDS[kind]
    value = user[column]
    if value is None:
        value = (defaults or get_defaults(database))[kind]
    return value or None


def used(database, user_id, kind):
    table = KINDS[kind][3]
    extra = KIND_FILTERS.get(kind, "")
    return database.execute(
        f"SELECT COUNT(*) FROM {table} WHERE user_id = ?{extra}", (user_id,)
    ).fetchone()[0]


def summary(database, user_id):
    defaults = get_defaults(database)
    result = {}
    for kind in KINDS:
        limit = limit_for(database, user_id, kind, defaults)
        count = used(database, user_id, kind)
        result[kind] = {
            "used": count,
            "limit": limit,
            "remaining": None if limit is None else max(0, limit - count),
            "full": limit is not None and count >= limit,
        }
    return result


def check(database, user_id, kind, adding=1):
    """Return an error message if adding would exceed the limit, else None."""
    limit = limit_for(database, user_id, kind)
    if limit is None:
        return None
    count = used(database, user_id, kind)
    if count + adding <= limit:
        return None
    noun = KINDS[kind][4]
    remaining = max(0, limit - count)
    plural = lambda n: f"{n} {noun}{'' if n == 1 else 's'}"
    if remaining == 0:
        return (
            f"You've reached your limit of {plural(limit)}. Delete one or ask an "
            "administrator to raise your limit."
        )
    return (
        f"That would exceed your limit of {plural(limit)}; you can add "
        f"{remaining} more {noun}{'' if remaining == 1 else 's'}."
    )
