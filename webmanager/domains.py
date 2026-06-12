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
                   root_site.id AS root_site_id,
                   root_site.name AS root_site_name
            FROM domains
            LEFT JOIN sites AS root_site
              ON root_site.domain_id = domains.id
             AND root_site.use_domain_root = 1
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
