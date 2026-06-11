import sqlite3

import click
from flask import current_app, g


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    google_sub TEXT,
    email TEXT COLLATE NOCASE,
    display_name TEXT,
    picture_url TEXT,
    auth_provider TEXT NOT NULL DEFAULT 'legacy',
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permissions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_groups (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS group_permissions (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (group_id, permission_code)
);

CREATE TABLE IF NOT EXISTS pools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    branch TEXT,
    local_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT,
    auto_refresh_minutes INTEGER,
    next_refresh_at TEXT,
    last_refreshed_at TEXT,
    update_mode TEXT NOT NULL DEFAULT 'approval',
    current_commit TEXT,
    pending_path TEXT,
    pending_commit TEXT,
    pending_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS repositories_user_id_idx ON repositories(user_id);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    folder TEXT NOT NULL,
    document_root TEXT NOT NULL,
    index_file TEXT NOT NULL,
    port INTEGER NOT NULL UNIQUE,
    spa_fallback INTEGER NOT NULL DEFAULT 1,
    nginx_config TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    runtime_backend TEXT,
    runtime_pid INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS sites_user_id_idx ON sites(user_id);

CREATE TABLE IF NOT EXISTS pool_sites (
    pool_id INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL UNIQUE REFERENCES sites(id) ON DELETE CASCADE,
    PRIMARY KEY (pool_id, site_id)
);

CREATE TABLE IF NOT EXISTS pool_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('viewer', 'operator')),
    CHECK((user_id IS NOT NULL) != (group_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS pool_acl_user_uq
ON pool_acl(pool_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pool_acl_group_uq
ON pool_acl(pool_id, group_id) WHERE group_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS site_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('viewer', 'operator')),
    CHECK((user_id IS NOT NULL) != (group_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS site_acl_user_uq
ON site_acl(site_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS site_acl_group_uq
ON site_acl(site_id, group_id) WHERE group_id IS NOT NULL;
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def init_db():
    database = get_db()
    database.executescript(SCHEMA)
    _migrate_users(database)
    _migrate_repositories(database)
    _migrate_sites(database)
    _seed_permissions(database)
    _ensure_initial_admin(database)
    database.commit()


def _migrate_users(database):
    columns = {
        row["name"]
        for row in database.execute("PRAGMA table_info(users)").fetchall()
    }
    additions = {
        "google_sub": "TEXT",
        "email": "TEXT COLLATE NOCASE",
        "display_name": "TEXT",
        "picture_url": "TEXT",
        "auth_provider": "TEXT NOT NULL DEFAULT 'legacy'",
        "is_admin": "INTEGER NOT NULL DEFAULT 0",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "last_login_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            database.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

    database.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub_uq
        ON users(google_sub) WHERE google_sub IS NOT NULL
        """
    )
    database.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_email_uq
        ON users(email COLLATE NOCASE) WHERE email IS NOT NULL
        """
    )


def _seed_permissions(database):
    permissions = (
        (
            "resources.view_all",
            "View all resources",
            "View every user's repositories and sites.",
        ),
        (
            "resources.manage_all",
            "Manage all resources",
            "Start, stop, edit, refresh, schedule, and delete any user's resources.",
        ),
        (
            "users.manage",
            "Manage users",
            "Activate users and assign their group memberships.",
        ),
        (
            "groups.manage",
            "Manage groups",
            "Create, edit, and delete permission groups.",
        ),
        (
            "access.manage",
            "Manage pools and access",
            "Create resource pools and grant users or groups access to sites.",
        ),
    )
    database.executemany(
        """
        INSERT INTO permissions (code, name, description)
        VALUES (?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name = excluded.name,
            description = excluded.description
        """,
        permissions,
    )


def _migrate_sites(database):
    used_slugs = set()
    sites = database.execute("SELECT id, slug FROM sites ORDER BY id").fetchall()
    for site in sites:
        base = site["slug"] or "site"
        slug = base
        suffix = 2
        while slug in used_slugs:
            suffix_text = f"-{suffix}"
            slug = f"{base[:48 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        if slug != site["slug"]:
            database.execute(
                "UPDATE sites SET slug = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (slug, site["id"]),
            )
        used_slugs.add(slug)
    database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS sites_slug_uq ON sites(slug)"
    )


def _ensure_initial_admin(database):
    has_admin = database.execute(
        "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
    ).fetchone()
    if has_admin is not None:
        return
    first_user = database.execute(
        "SELECT id FROM users ORDER BY id LIMIT 1"
    ).fetchone()
    if first_user is not None:
        database.execute(
            "UPDATE users SET is_admin = 1, is_active = 1 WHERE id = ?",
            (first_user["id"],),
        )


def _migrate_repositories(database):
    columns = {
        row["name"]
        for row in database.execute("PRAGMA table_info(repositories)").fetchall()
    }
    additions = {
        "auto_refresh_minutes": "INTEGER",
        "next_refresh_at": "TEXT",
        "last_refreshed_at": "TEXT",
        "update_mode": "TEXT NOT NULL DEFAULT 'approval'",
        "current_commit": "TEXT",
        "pending_path": "TEXT",
        "pending_commit": "TEXT",
        "pending_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            database.execute(
                f"ALTER TABLE repositories ADD COLUMN {column} {definition}"
            )

    database.execute(
        """
        CREATE INDEX IF NOT EXISTS repositories_next_refresh_idx
        ON repositories(next_refresh_at)
        WHERE auto_refresh_minutes IS NOT NULL
        """
    )


@click.command("init-db")
def init_db_command():
    init_db()
    click.echo("Initialized the database.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
