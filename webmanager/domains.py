import re
from urllib.parse import urlsplit

from flask import current_app


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("Enter a valid domain such as example.com.")
    return domain


def blocked_domain_names(database) -> set[str]:
    return {
        row["name"]
        for row in database.execute("SELECT name FROM blocked_domains").fetchall()
    }


def domain_is_blocked(domain: str, blocked: set[str]) -> bool:
    normalized = domain.lower().rstrip(".")
    return any(
        normalized == denied or normalized.endswith(f".{denied}")
        for denied in blocked
    )


def available_domains(database):
    blocked = blocked_domain_names(database)
    return [
        row
        for row in database.execute(
            """
            SELECT domains.*,
                   COALESCE(primary_root.id, alias_root.id) AS root_site_id,
                   COALESCE(primary_root.name, alias_root.name) AS root_site_name
            FROM domains
            LEFT JOIN sites AS primary_root
              ON primary_root.domain_id = domains.id
             AND primary_root.use_domain_root = 1
            LEFT JOIN site_domain_aliases AS root_alias
              ON root_alias.domain_id = domains.id
             AND root_alias.use_domain_root = 1
            LEFT JOIN sites AS alias_root
              ON alias_root.id = root_alias.site_id
            ORDER BY domains.is_default DESC, domains.name COLLATE NOCASE
            """
        ).fetchall()
        if not domain_is_blocked(row["name"], blocked)
    ]


def dashboard_hostname() -> str:
    from .db import get_db

    database = get_db()
    primary = database.execute(
        "SELECT name FROM dashboard_domains WHERE is_primary = 1"
    ).fetchone()
    if primary:
        return primary["name"]
    redirect_uri = current_app.config.get("GOOGLE_REDIRECT_URI", "")
    return (urlsplit(redirect_uri).hostname or "").lower()


def dashboard_hostnames(database) -> set[str]:
    return {
        row["name"]
        for row in database.execute("SELECT name FROM dashboard_domains").fetchall()
    }


def domain_is_dashboard(database, domain: str) -> bool:
    return domain.lower().rstrip(".") in dashboard_hostnames(database)


def default_domain(database):
    blocked = blocked_domain_names(database)
    domain = database.execute(
        "SELECT * FROM domains WHERE is_default = 1"
    ).fetchone()
    if domain and not domain_is_blocked(domain["name"], blocked):
        return domain
    return None


def site_domain(database, site):
    keys = site.keys()
    if "domain_name" in keys and site["domain_name"]:
        return site["domain_name"]
    if "domain_id" in keys and site["domain_id"]:
        row = database.execute(
            "SELECT name FROM domains WHERE id = ?",
            (site["domain_id"],),
        ).fetchone()
        if row:
            return row["name"]
    fallback = current_app.config.get("SITE_BASE_DOMAIN", "")
    return fallback or None


def site_hostname(database, site) -> str | None:
    domain = site_domain(database, site)
    if not domain:
        return None
    if "use_domain_root" in site.keys() and site["use_domain_root"]:
        return domain
    return f"{site['slug']}.{domain}"


def site_domain_bindings(database, site):
    bindings = []
    domain = site_domain(database, site)
    if domain:
        bindings.append(
            {
                "domain_id": site["domain_id"] if "domain_id" in site.keys() else None,
                "domain": domain,
                "use_domain_root": bool(
                    "use_domain_root" in site.keys() and site["use_domain_root"]
                ),
                "is_primary": True,
            }
        )
    if "id" not in site.keys():
        return bindings
    bindings.extend(
        {
            "domain_id": row["domain_id"],
            "domain": row["domain_name"],
            "use_domain_root": bool(row["use_domain_root"]),
            "is_primary": False,
        }
        for row in database.execute(
            """
            SELECT aliases.domain_id, aliases.use_domain_root,
                   domains.name AS domain_name
            FROM site_domain_aliases AS aliases
            JOIN domains ON domains.id = aliases.domain_id
            WHERE aliases.site_id = ?
              AND aliases.domain_id != COALESCE(?, -1)
            ORDER BY domains.name COLLATE NOCASE
            """,
            (
                site["id"],
                site["domain_id"] if "domain_id" in site.keys() else None,
            ),
        ).fetchall()
    )
    return bindings


def site_hostnames(database, site) -> list[str]:
    return list(
        dict.fromkeys(
            binding["domain"]
            if binding["use_domain_root"]
            else f"{site['slug']}.{binding['domain']}"
            for binding in site_domain_bindings(database, site)
        )
    )


def domain_root_owner(database, domain_id: int, exclude_site_id: int | None = None):
    return database.execute(
        """
        SELECT sites.id, sites.name
        FROM sites
        WHERE sites.domain_id = ?
          AND sites.use_domain_root = 1
          AND sites.id != ?
        UNION ALL
        SELECT sites.id, sites.name
        FROM site_domain_aliases AS aliases
        JOIN sites ON sites.id = aliases.site_id
        WHERE aliases.domain_id = ?
          AND aliases.use_domain_root = 1
          AND sites.id != ?
        LIMIT 1
        """,
        (domain_id, exclude_site_id or -1, domain_id, exclude_site_id or -1),
    ).fetchone()
