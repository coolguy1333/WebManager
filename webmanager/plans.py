"""Plans: named levels (Starter, Basic, Advanced…) that set how many sites,
apps and Git sources a person may have, and whether they may host apps.

Everyone who isn't a super admin is on a plan: the one chosen for them, or
else the default plan. A person can still have custom limits that override
their plan's (People & access → a person → Custom limits). As everywhere in
WebManager, a limit of 0 means unlimited. Super admins are never limited.
"""

DEFAULT_PLAN_SETTING = "default_plan_id"
MAX_LIMIT = 10_000
NAME_MAX = 40


def ensure_table(database):
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            max_sites INTEGER NOT NULL DEFAULT 0,
            max_apps INTEGER NOT NULL DEFAULT 0,
            max_sources INTEGER NOT NULL DEFAULT 0,
            can_host_apps INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()}
    if "plan_id" not in columns:
        database.execute("ALTER TABLE users ADD COLUMN plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL")


def seed(database):
    """The first time: Starter, Basic (today's default limits, so nobody's
    limits change) and Advanced, with Basic the default."""
    if database.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
        return
    from .quotas import get_defaults

    defaults = get_defaults(database)
    rows = (
        ("Starter", 1, 1, 1, 0, 1),
        ("Basic", defaults["sites"], defaults["apps"], defaults["sources"], 0, 2),
        ("Advanced", 20, 5, 10, 1, 3),
    )
    database.executemany(
        """
        INSERT INTO plans (name, max_sites, max_apps, max_sources, can_host_apps, position)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    basic = database.execute("SELECT id FROM plans WHERE name = 'Basic'").fetchone()
    database.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (DEFAULT_PLAN_SETTING, str(basic["id"])),
    )


def all_plans(database):
    return database.execute(
        """
        SELECT plans.*, (
            SELECT COUNT(*) FROM users
            WHERE users.is_admin = 0 AND (
                users.plan_id = plans.id
                OR (users.plan_id IS NULL AND plans.id = (
                    SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = ?))
            )
        ) AS people
        FROM plans ORDER BY position, name COLLATE NOCASE
        """,
        (DEFAULT_PLAN_SETTING,),
    ).fetchall()


def default_plan_id(database):
    row = database.execute("SELECT value FROM app_settings WHERE key = ?", (DEFAULT_PLAN_SETTING,)).fetchone()
    try:
        plan_id = int(row["value"]) if row else None
    except (TypeError, ValueError):
        plan_id = None
    if plan_id is not None and database.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone():
        return plan_id
    first = database.execute("SELECT id FROM plans ORDER BY position, id LIMIT 1").fetchone()
    return first["id"] if first else None


def set_default(database, plan_id: int):
    database.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (DEFAULT_PLAN_SETTING, str(plan_id)),
    )


def plan_for(database, user) -> "dict | None":
    """The plan a person is on (theirs, else the default), or None if there
    are no plans at all."""
    plan_id = None
    try:
        plan_id = user["plan_id"]
    except (IndexError, KeyError):
        row = database.execute("SELECT plan_id FROM users WHERE id = ?", (user["id"],)).fetchone()
        plan_id = row["plan_id"] if row else None
    row = None
    if plan_id is not None:
        row = database.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        default = default_plan_id(database)
        row = database.execute("SELECT * FROM plans WHERE id = ?", (default,)).fetchone() if default else None
    return row


def parse(form) -> dict:
    """A plan from a form. Raises ValueError with a message."""
    name = " ".join((form.get("name") or "").split())
    if not name or len(name) > NAME_MAX:
        raise ValueError(f"Give the plan a name of up to {NAME_MAX} characters.")
    values = {"name": name, "can_host_apps": 1 if form.get("can_host_apps") == "1" else 0}
    for field, label in (("max_sites", "Sites"), ("max_apps", "Apps"), ("max_sources", "Git sources")):
        raw = (form.get(field) or "0").strip()
        try:
            number = int(raw)
        except ValueError:
            raise ValueError(f"{label}: enter a whole number (0 means unlimited).") from None
        if not 0 <= number <= MAX_LIMIT:
            raise ValueError(f"{label}: must be between 0 and {MAX_LIMIT} (0 means unlimited).")
        values[field] = number
    return values
