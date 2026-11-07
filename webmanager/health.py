"""Is everything WebManager depends on actually working?

Each check looks at one thing (the web server in front of the sites, the
traffic log analytics reads, free disk, Google sign-in, DNS, the updater,
containers, other servers ...) and answers in plain words: what it found and,
when something is wrong, exactly what to do about it. The System page runs them
on demand so "it doesn't work" can be answered without reading logs.
"""

import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app

from . import analytics, peer_http, update_status
from .domains import site_hostnames

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
ORDER = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}
CHECK_TIMEOUT = 4.0


def result(check_id, title, status, detail, fix=None):
    return {"id": check_id, "title": title, "status": status, "detail": detail, "fix": fix}


def _port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Each check takes the plain `inputs` dict built on the request thread and may
# block briefly (sockets, files); they run in parallel.
# ---------------------------------------------------------------------------
def check_web_server(inputs):
    title = "Web server for hosted sites"
    if not inputs["nginx_binary"]:
        return result(
            "web_server", title, WARN,
            "Nginx isn't installed, so sites are served by WebManager's simple built-in server: no custom "
            "hostnames, no traffic analytics and no app hosting.",
            "sudo apt install nginx, then restart WebManager (sudo systemctl restart webmanager).",
        )
    if _port_open("127.0.0.1", inputs["gateway_port"]):
        return result("web_server", title, OK, f"Nginx is answering on port {inputs['gateway_port']}.")
    return result(
        "web_server", title, FAIL,
        f"Nothing is listening on port {inputs['gateway_port']}, so hosted sites can't be reached.",
        "Restart WebManager (sudo systemctl restart webmanager). If it stays down, run "
        "sudo journalctl -u webmanager -n 50 --no-pager and look for an Nginx error.",
    )


def check_sites(inputs):
    title = "Hosted sites answer"
    sites = inputs["running_sites"]
    if not sites:
        return result("sites", title, INFO, "No sites are running yet.")
    if not inputs["nginx_binary"] or not _port_open("127.0.0.1", inputs["gateway_port"]):
        return result("sites", title, INFO, "Skipped: the web server above isn't up.")
    broken = []
    checked = 0
    deadline = time.monotonic() + 3.0
    for site in sites[:20]:
        if time.monotonic() > deadline:
            break  # a slow site must not hold the whole report up
        checked += 1
        host = (site["hostnames"] or [None])[0]
        if not host:
            broken.append(f"{site['name']} (no public address)")
            continue
        request = urllib.request.Request(
            f"http://127.0.0.1:{inputs['gateway_port']}/", headers={"Host": host, "User-Agent": analytics.HEALTH_PROBE_AGENT}
        )
        try:
            with peer_http.open_peer(request, timeout=2.5) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (urllib.error.URLError, OSError):
            code = None
        if code is None or code >= 500:
            broken.append(f"{site['name']} ({'no answer' if code is None else f'error {code}'})")
    if not broken:
        more = f" (the first {checked} of {len(sites)} were checked)" if checked < len(sites) else ""
        return result("sites", title, OK, f"All {checked} running site(s) answer{more}.")
    return result(
        "sites", title, FAIL, "These running sites don't answer: " + ", ".join(broken) + ".",
        "Open the site's page and press Restart; the site's log on that page says why it failed.",
    )


def check_traffic_log(inputs):
    title = "Traffic log (for Analytics)"
    path = Path(inputs["nginx_root"]) / "access.log"
    if not inputs["nginx_binary"]:
        return result("traffic_log", title, INFO, "Analytics needs Nginx; see the web server check.")
    if not path.exists():
        if inputs["running_sites"]:
            return result(
                "traffic_log", title, WARN,
                "No traffic log exists yet, so Analytics is empty. It appears with the first visit to a site.",
                f"Visit one of your sites once. If it stays missing, check that {path.parent} is writable by the webmanager user.",
            )
        return result("traffic_log", title, INFO, "No traffic recorded yet (no sites are running).")
    if not os.access(path, os.R_OK):
        return result(
            "traffic_log", title, FAIL, f"{path} exists but WebManager can't read it, so Analytics is empty.",
            f"sudo chown webmanager:webmanager {path}",
        )
    age = time.time() - path.stat().st_mtime
    size = path.stat().st_size
    if size == 0:
        return result(
            "traffic_log", title, INFO,
            "The traffic log is empty: it was just rotated, or no one has visited a site yet.",
        )
    return result(
        "traffic_log", title, OK,
        f"Recording traffic ({_size(size)}, last request {_ago(age)}).",
    )


def check_disk(inputs):
    title = "Free disk space"
    try:
        usage = shutil.disk_usage(inputs["data_dir"])
    except OSError as exc:
        return result("disk", title, WARN, f"Couldn't read disk usage: {exc}.")
    free_percent = 100 * usage.free / usage.total if usage.total else 100
    text = f"{_size(usage.free)} free ({free_percent:.0f}%) where WebManager keeps its data."
    if free_percent < 5 or usage.free < 300 * 1024 * 1024:
        return result(
            "disk", title, FAIL, text + " Updates, backups, Git checkouts and app builds will start failing.",
            "Free space: remove unused sites and sources, run sudo apt clean, or enlarge the disk.",
        )
    if free_percent < 15:
        return result("disk", title, WARN, text, "Free some space soon: remove unused sites and sources.")
    return result("disk", title, OK, text)


def check_git(inputs):
    title = "Git"
    if shutil.which("git"):
        return result("git", title, OK, "Git is installed.")
    return result(
        "git", title, FAIL, "Git isn't installed, so repositories can't be cloned or updated.",
        "sudo apt install git",
    )


def check_sign_in(inputs):
    title = "Sign-in and sessions"
    problems, fixes = [], []
    redirect = inputs["google_redirect_uri"]
    if not inputs["google_configured"]:
        return result(
            "sign_in", title, FAIL, "Google sign-in isn't configured, so nobody new can sign in.",
            "Run bash configure-google.sh on the server.",
        )
    parts = urlsplit(redirect)
    if parts.scheme == "https" and not inputs["cookie_secure"]:
        problems.append("the callback address is https:// but session cookies aren't marked Secure")
        fixes.append("set WEBMANAGER_SESSION_COOKIE_SECURE=1 in /etc/webmanager/webmanager.env and restart")
    if parts.scheme == "http" and inputs["cookie_secure"]:
        problems.append("session cookies are marked Secure but the callback address is plain http://, so browsers drop them and sign-in loops")
        fixes.append("use an https:// callback address (bash configure-google.sh) or set WEBMANAGER_SESSION_COOKIE_SECURE=0")
    host = inputs["request_host"]
    if parts.hostname and host and host != parts.hostname and not _is_ip(host):
        problems.append(f"you're on {host} but Google sends people back to {parts.hostname}")
        fixes.append(f"open WebManager at {parts.scheme}://{parts.netloc} (or add this address to the dashboard domains and the Google client)")
    if inputs["unrestricted"]:
        return result(
            "sign_in", title, WARN if not problems else FAIL,
            ("; ".join(problems) + "; " if problems else "")
            + "any Google account can sign in (no allowed domains or emails are set).",
            "; ".join(fixes) or "Set WEBMANAGER_GOOGLE_ALLOWED_DOMAINS or WEBMANAGER_GOOGLE_ALLOWED_EMAILS (bash configure-google.sh).",
        )
    if problems:
        return result("sign_in", title, FAIL, "; ".join(problems).capitalize() + ".", "; ".join(fixes))
    return result("sign_in", title, OK, f"Google sign-in is set up; the callback is {redirect}.")


def check_dns(inputs):
    title = "Site addresses (DNS)"
    base = inputs["base_domain"]
    if not base:
        return result(
            "dns", title, WARN, "No base domain is set, so sites get plain port addresses instead of names.",
            "Add a domain under Domains, or re-run setup.sh and enter one.",
        )
    probe = f"webmanager-check-{int(time.time()) % 100000}.{base}"
    outcome = {}

    def look():
        try:
            outcome["addresses"] = {item[4][0] for item in socket.getaddrinfo(probe, None)}
        except OSError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(3)
    if "addresses" in outcome:
        return result("dns", title, OK, f"*.{base} resolves (to {', '.join(sorted(outcome['addresses'])[:3])}).")
    return result(
        "dns", title, WARN,
        f"*.{base} doesn't resolve from this server, so new sites won't be reachable by name. "
        "(Fine if you only use a Cloudflare Tunnel, which has its own routing.)",
        f"Add a wildcard DNS record: *.{base} pointing at this server's address.",
    )


def check_updates(inputs):
    title = "WebManager updates"
    status = inputs["update_status"]
    active = inputs["updater_active"]
    state = status.get("state")
    if active is False:
        return result(
            "updates", title, WARN,
            "The updater service is off, so this server can check for updates but not install them.",
            "sudo systemctl enable --now webmanager-update.path webmanager-update.timer (or re-run setup.sh).",
        )
    if state == "error":
        return result(
            "updates", title, FAIL, f"The last update attempt failed: {status.get('message')}",
            "Open System → WebManager updates for the details and a Try again button.",
        )
    if state == "available":
        auto = "It installs by itself." if inputs["auto_install"] else "Approve it on this page to install it."
        return result("updates", title, INFO, f"A newer version is available. {auto}")
    if state == "current":
        return result("updates", title, OK, "WebManager is up to date.")
    return result("updates", title, INFO, status.get("message") or "No update check has run yet.")


def check_apps(inputs):
    title = "App hosting"
    if not inputs["apps_enabled"]:
        return result("apps", title, INFO, "Off (static sites only). Turn it on with sudo bash deploy/debian/enable-apps.sh.")
    ready, message = inputs["apps_status"]
    if ready:
        return result("apps", title, OK, f"Containers are available ({message}).")
    return result(
        "apps", title, FAIL, message,
        "Check that Docker is running (sudo systemctl status docker) and re-run sudo bash deploy/debian/enable-apps.sh.",
    )


def check_data_dir(inputs):
    title = "WebManager's data folder"
    path = Path(inputs["data_dir"])
    if not os.access(path, os.W_OK):
        return result(
            "data_dir", title, FAIL, f"WebManager can't write to {path}.",
            f"sudo chown -R webmanager:webmanager {path}",
        )
    return result("data_dir", title, OK, f"{path} is writable.")


def check_servers(inputs):
    title = "Other servers"
    peers = inputs["peers"]
    replica = inputs["replication"]
    if not peers and not replica["is_replica"]:
        return result("servers", title, INFO, "This server runs on its own. Add others under System → Servers.")
    down = [peer["hostname"] for peer in peers if not peer["reachable"] and peer["checked_at"]]
    problems = []
    if replica["is_replica"] and replica["last_error"]:
        problems.append(f"settings sync is failing: {replica['last_error']}")
    if replica["is_replica"] and replica["data_last_error"]:
        problems.append(f"data sync is failing: {replica['data_last_error']}")
    if down:
        problems.append("not answering: " + ", ".join(down))
    if problems:
        return result(
            "servers", title, WARN, "; ".join(problems) + ".",
            "Each server's row in the list below says what's wrong and how to fix it.",
        )
    return result("servers", title, OK, f"{len(peers)} other server(s) reachable.")


CHECKS = (
    check_web_server, check_sites, check_traffic_log, check_sign_in, check_dns, check_disk,
    check_data_dir, check_git, check_updates, check_apps, check_servers,
)


# ---------------------------------------------------------------------------
def _size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _ago(seconds):
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _is_ip(host):
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host.strip("[]"))
        return True
    except OSError:
        return False


def gather_inputs(request_host=""):
    """Everything the checks need, read on the request thread (database,
    config, caches) so the checks themselves can run in parallel threads."""
    from .auth import google_is_configured
    from .db import get_db

    app = current_app
    runtime = app.extensions["runtime_manager"]
    database = get_db()
    sites = database.execute(
        "SELECT * FROM sites WHERE status = 'running' ORDER BY name COLLATE NOCASE"
    ).fetchall()
    running = [
        {"id": site["id"], "name": site["name"], "hostnames": site_hostnames(database, site)}
        for site in sites
    ]
    base = app.config.get("SITE_BASE_DOMAIN") or ""
    if not base:
        try:
            default = database.execute("SELECT name FROM domains WHERE is_default = 1").fetchone()
            base = default["name"] if default else ""
        except Exception:  # noqa: BLE001 - a missing table must not hide every other check
            base = ""
    replication = app.extensions["replication_manager"]
    data_replication = app.extensions["data_replication_manager"]
    return {
        "nginx_binary": runtime.nginx_binary,
        "gateway_port": app.config["SITE_GATEWAY_PORT"],
        "nginx_root": app.config["NGINX_ROOT"],
        "data_dir": app.instance_path,
        "running_sites": running,
        "google_configured": google_is_configured(),
        "google_redirect_uri": app.config.get("GOOGLE_REDIRECT_URI") or "",
        "cookie_secure": bool(app.config.get("SESSION_COOKIE_SECURE")),
        "unrestricted": not (app.config["GOOGLE_ALLOWED_DOMAINS"] or app.config["GOOGLE_ALLOWED_EMAILS"]),
        "request_host": (request_host or "").split(":")[0].lower(),
        "base_domain": base,
        "update_status": update_status.read_update_status(),
        "updater_active": update_status.updater_active(),
        "auto_install": update_status.auto_install_enabled(),
        "apps_enabled": bool(app.config.get("APPS_ENABLED")),
        "apps_status": runtime.apps_status() if app.config.get("APPS_ENABLED") else (False, ""),
        "peers": app.extensions["mesh_hub"].entries(),
        "replication": {
            "is_replica": replication.is_replica,
            "last_error": replication.last_error,
            "data_last_error": data_replication.last_error,
        },
    }


def run_checks(inputs) -> dict:
    """Run every check in parallel; a check that crashes or hangs becomes a
    warning instead of taking the page down."""
    results = []
    pool = ThreadPoolExecutor(max_workers=len(CHECKS))
    futures = [(check, pool.submit(check, inputs)) for check in CHECKS]
    deadline = time.monotonic() + CHECK_TIMEOUT
    for check, future in futures:
        name = check.__name__.removeprefix("check_")
        try:
            results.append(future.result(timeout=max(0.1, deadline - time.monotonic())))
        except Exception as exc:  # noqa: BLE001 - includes timeouts
            results.append(
                result(name, name.replace("_", " ").capitalize(), WARN,
                       f"This check didn't finish ({type(exc).__name__}).")
            )
    pool.shutdown(wait=False, cancel_futures=True)
    counts = {status: sum(item["status"] == status for item in results) for status in ORDER}
    results.sort(key=lambda item: ORDER[item["status"]])
    return {"checks": results, "counts": counts, "checked_at": int(time.time())}


def format_report(report) -> str:
    """The report as plain text, for `webmanager-doctor`."""
    symbols = {OK: "✓", INFO: "i", WARN: "!", FAIL: "✕"}
    lines = ["WebManager health check", ""]
    for item in report["checks"]:
        lines.append(f" {symbols[item['status']]} {item['title']}: {item['detail']}")
        if item["fix"]:
            lines.append(f"     What to do: {item['fix']}")
    counts = report["counts"]
    lines.append("")
    if counts[FAIL]:
        lines.append(f"{counts[FAIL]} problem(s) and {counts[WARN]} thing(s) to look at.")
    elif counts[WARN]:
        lines.append(f"No problems; {counts[WARN]} thing(s) to look at.")
    else:
        lines.append("Everything checks out.")
    return "\n".join(lines)


def run_doctor() -> int:
    """`python run.py doctor`: print the report; exit 1 if anything is broken."""
    from . import create_app

    app = create_app({"BACKGROUND_TASKS": False})
    with app.app_context():
        report = run_checks(gather_inputs(""))
    print(format_report(report))
    return 1 if report["counts"][FAIL] else 0
