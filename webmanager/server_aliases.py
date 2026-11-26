"""Public addresses for replicas: replica1.<dashboard domain>, replica2…

A replica usually sits on the local network (http://192.168.10.116:8080),
which a browser outside that network can't open. The dashboard domain
(webmgr.dev) already reaches the primary, and so does every name under it,
so the primary serves replica1.webmgr.dev by passing it through to that
replica over the local network (its managed Nginx; see build_main_config).
The replica answers that name as its own dashboard.

Numbers are handed out by the primary the first time it sees a server
report itself as a replica, and kept: they live in the primary's database,
which every replica mirrors, so each replica knows the names too.
"""

import json
import re
import socket
from urllib.parse import urlsplit

from flask import request

from .db import get_setting, set_setting

SETTING = "server_aliases"
PREFIX = "replica"
# What may be put into the Nginx config as an upstream: scheme://host[:port].
_UPSTREAM_RE = re.compile(r"^https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
_IP_RE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[?[0-9A-Fa-f:.]+\]?)$")


def load(database) -> dict[int, str]:
    """{number: server address} for every replica that has a number."""
    try:
        data = json.loads(get_setting(database, SETTING, "{}") or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, url in data.items():
        if str(key).isdigit() and isinstance(url, str) and _UPSTREAM_RE.fullmatch(url):
            result[int(key)] = url
    return result


def _save(database, aliases: dict[int, str]):
    set_setting(database, SETTING, json.dumps({str(number): url for number, url in sorted(aliases.items())}))
    database.commit()


def assign(database, url: str) -> tuple[int, bool]:
    """The number for this server, giving it the next free one if it has
    none: (number, newly assigned). Raises ValueError for an unusable URL."""
    if not _UPSTREAM_RE.fullmatch(url or ""):
        raise ValueError(f"{url!r} can't be used as a replica address.")
    aliases = load(database)
    for number, existing in aliases.items():
        if existing == url:
            return number, False
    number = 1
    while number in aliases:
        number += 1
    aliases[number] = url
    _save(database, aliases)
    return number, True


def remove(database, url: str) -> bool:
    aliases = load(database)
    remaining = {number: existing for number, existing in aliases.items() if existing != url}
    if remaining == aliases:
        return False
    _save(database, remaining)
    return True


def base_domain(database) -> str | None:
    """The dashboard domain replica names go under, or None (no dashboard
    domain, or it is an IP address)."""
    row = database.execute("SELECT name FROM dashboard_domains WHERE is_primary = 1").fetchone()
    name = (row["name"] if row else "").strip().lower().rstrip(".")
    if not name or _IP_RE.fullmatch(name) or "." not in name:
        return None
    return name


def _site_hostnames(database) -> set[str]:
    from .domains import site_hostnames

    names = set()
    for site in database.execute("SELECT * FROM sites").fetchall():
        names.update(name.lower() for name in site_hostnames(database, site))
    return names


def routes(database) -> list[tuple[str, str]]:
    """(hostname, server address) for every replica with a usable name. A
    name a hosted site already uses stays the site's."""
    domain = base_domain(database)
    if not domain:
        return []
    taken = _site_hostnames(database)
    return [
        (f"{PREFIX}{number}.{domain}", url)
        for number, url in sorted(load(database).items())
        if f"{PREFIX}{number}.{domain}" not in taken
    ]


def hostnames(database) -> list[str]:
    return [hostname for hostname, _url in routes(database)]


def public_urls(database, scheme: str) -> dict[str, str]:
    """{server address: its public dashboard address} for the Servers list."""
    return {url: f"{scheme}://{hostname}" for hostname, url in routes(database)}


def is_alias_host(database, host: str) -> bool:
    return (host or "").split(":")[0].lower().rstrip(".") in set(hostnames(database))


def is_this_server(url: str) -> bool:
    """Whether an address points at this machine (it can bind that IP)."""
    host = urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, _type, _proto, _name, address in infos:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((address[0], 0))
            return True
        except OSError:
            continue
    return False


def nginx_plan(database, is_replica: bool) -> tuple[list[str], list[tuple[str, str]]]:
    """(names this server answers as its own dashboard, (name, address) pairs
    it passes through to another server). A replica answers the names: the
    primary only sends it its own. A primary passes them on, except one that
    points at itself (a replica that was promoted keeps its old number)."""
    own, passed_on = [], []
    for hostname, url in routes(database):
        if is_replica or is_this_server(url):
            own.append(hostname)
        else:
            passed_on.append((hostname, url))
    return own, passed_on


def request_scheme() -> str:
    """http or https as the visitor sees it. WebManager listens only behind
    this server's own Nginx, which reports the visitor's scheme."""
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if forwarded in ("http", "https") and (request.remote_addr or "") in ("127.0.0.1", "::1"):
        return forwarded
    return request.scheme
