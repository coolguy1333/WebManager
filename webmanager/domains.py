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
            SELECT * FROM domains
            ORDER BY is_default DESC, name COLLATE NOCASE
            """
        ).fetchall()
        if not domain_is_blocked(row["name"], blocked)
    ]


def dashboard_hostname() -> str:
    redirect_uri = current_app.config.get("GOOGLE_REDIRECT_URI", "")
    return (urlsplit(redirect_uri).hostname or "").lower()


def default_domain(database):
    domains = available_domains(database)
    return domains[0] if domains else None


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
