import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .access_control import (
    RESOURCE_MANAGE_ALL,
    RESOURCE_VIEW_ALL,
    can_manage_site,
    can_manage_resource,
    can_view_site,
    can_view_resource,
    has_permission,
    site_access_levels,
)
from .analytics import aggregate_analytics
from .db import get_db
from .domains import (
    available_domains,
    binding_hostname,
    blocked_domain_names,
    domain_root_owner,
    hostname_owner,
    domain_is_dashboard,
    default_domain,
    domain_is_blocked,
    site_domain_bindings,
    site_hostname,
    site_hostnames,
)
from .git_service import (
    GitError,
    check_repository_host,
    clone_repository,
    display_repo_url,
    find_index_folders,
    repo_name_from_url,
    repository_commit,
    resolve_folder,
    validate_repo_url,
)
from .nginx import NginxConfigError, build_app_config, build_site_config, validate_site_config
from .repository_refresh import (
    DEFAULT_CHECK_MINUTES,
    INTERVAL_UNITS,
    MAX_REFRESH_MINUTES,
    MIN_REFRESH_MINUTES,
    format_interval,
    next_refresh_time,
    split_interval,
)
from . import apps as app_support
from . import quotas
from .security import login_required, safe_local_path, validate_csrf
from .services import RuntimeErrorDetail, allocate_port, port_is_available


bp = Blueprint("deployments", __name__)
bp.add_app_template_filter(format_interval, "interval")
bp.add_app_template_global(split_interval, "split_interval")
SLUG_RE = re.compile(r"[^a-z0-9]+")
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


CONTROL_CHARACTERS_RE = re.compile(r"[\x00-\x1f\x7f]")


# Folder names that describe build output rather than the site itself.
BUILD_FOLDERS = {"dist", "build", "out", "_site", "public", "www", "site", "html"}
GENERIC_FOLDERS = BUILD_FOLDERS | {"src", "static", "web", "app", "client", "frontend"}


def _title_from(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").replace(".", " ").strip().title() or "Site"


def valid_site_name(name: str) -> bool:
    return bool(name) and len(name) <= 80 and not CONTROL_CHARACTERS_RE.search(name)


APP_HOST_PERMISSION = "apps.host"


def can_host_apps() -> bool:
    """Apps are opt-in per server and per person."""
    return bool(current_app.config.get("APPS_ENABLED")) and (
        bool(g.user and g.user["is_admin"]) or has_permission(APP_HOST_PERMISSION)
    )


def app_manifest(site):
    try:
        return app_support.load_manifest(
            Path(site["document_root"]), current_app.config["APP_DEFAULT_MEMORY_MB"]
        ), None
    except app_support.AppError as exc:
        return None, str(exc)


def slugify(value: str) -> str:
    return SLUG_RE.sub("-", value.lower()).strip("-")[:48] or "site"


def unique_slug(database, value: str, exclude_site_id: int | None = None) -> str:
    base = slugify(value)
    slug = base
    suffix = 2
    while database.execute(
        "SELECT 1 FROM sites WHERE slug = ? AND id != ?",
        (slug, exclude_site_id or -1),
    ).fetchone():
        suffix_text = f"-{suffix}"
        slug = f"{base[:48 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return slug


def request_hostname() -> str:
    hostname = urlsplit(request.host_url).hostname or "localhost"
    return f"[{hostname}]" if ":" in hostname else hostname


def action_redirect(endpoint: str, **values):
    requested = safe_local_path(request.form.get("next", "").strip())
    if requested:
        return redirect(requested, code=303)
    return redirect(url_for(endpoint, **values), code=303)


def site_public_url(site) -> str:
    hostname = site_hostname(get_db(), site)
    if hostname:
        scheme = current_app.config["SITE_PUBLIC_SCHEME"]
        return f"{scheme}://{hostname}"
    return f"http://{request_hostname()}:{site['port']}"


def site_public_urls(site) -> list[str]:
    hostnames = site_hostnames(get_db(), site)
    if hostnames:
        scheme = current_app.config["SITE_PUBLIC_SCHEME"]
        return [f"{scheme}://{hostname}" for hostname in hostnames]
    return [f"http://{request_hostname()}:{site['port']}"]


def site_routing_details(site) -> list[dict[str, str]]:
    database = get_db()
    details = []
    for binding in site_domain_bindings(database, site):
        hostname = binding_hostname(site, binding)
        details.append(
            {
                "hostname": hostname,
                "wildcard": f"*.{binding['domain']}",
                "use_domain_root": binding["use_domain_root"],
                "is_primary": binding["is_primary"],
                "origin": "http://localhost:8080",
                "local_test": (
                    f"curl -I -H 'Host: {hostname}' http://127.0.0.1:8080/"
                ),
            }
        )
    return details


def site_domain_summary(site):
    bindings = site_domain_bindings(get_db(), site)
    groups = {"root": [], "subdomains": [], "aliases": []}
    items = []
    main = None
    for binding in bindings:
        hostname = binding_hostname(site, binding)
        item = {
            **binding,
            "hostname": hostname,
            "url": f"{current_app.config['SITE_PUBLIC_SCHEME']}://{hostname}",
            "label": (
                "Root domain"
                if binding["use_domain_root"]
                else "Primary subdomain"
                if binding["is_primary"]
                else "Custom subdomain"
                if binding["hostname_prefix"]
                else "Alias"
            ),
        }
        items.append(item)
        if binding["is_primary"]:
            main = item
        if binding["use_domain_root"]:
            groups["root"].append(item)
        elif not binding["is_primary"] and not binding["hostname_prefix"]:
            groups["aliases"].append(item)
        else:
            groups["subdomains"].append(item)
    return {
        "main": main,
        "items": items,
        "groups": groups,
        "count": len(bindings),
    }


def repository_update_display(repository):
    keys = repository.keys()
    state_key = (
        "update_state"
        if "update_state" in keys
        else "repository_update_state"
    )
    interval_key = (
        "auto_refresh_minutes"
        if "auto_refresh_minutes" in keys
        else "repository_auto_refresh_minutes"
    )
    error_key = (
        "update_error"
        if "update_error" in keys
        else "repository_update_error"
    )
    fallback_error_key = "error" if "error" in keys else "repository_error"
    mode_key = "update_mode" if "update_mode" in keys else "repository_update_mode"
    pending_key = "pending_commit" if "pending_commit" in keys else "repository_pending_commit"
    state = repository[state_key] or "idle"
    automatic = repository[mode_key] == "auto" if mode_key in keys else False
    interval = repository[interval_key]
    if state == "checking":
        label = "Checking"
    elif state == "updating":
        label = "Updating"
    elif state == "failed":
        label = "Failed"
    elif pending_key in keys and repository[pending_key]:
        label = "Update ready"
    elif automatic:
        label = f"Auto every {format_interval(interval)}"
    else:
        label = "Notify only"
    return {
        "state": state,
        "label": label,
        "automatic": automatic,
        "enabled": automatic,
        "interval": interval,
        "error": repository[error_key] or repository[fallback_error_key],
    }


def site_attention_reasons(site):
    reasons = []
    is_app = "kind" in site.keys() and site["kind"] == "app"
    if is_app and site["last_error"] and site["status"] in ("error", "running"):
        # App errors carry container logs after the first line; the logs are
        # shown in full on the app page.
        summary = site["last_error"].strip().splitlines()[0]
        if site["status"] == "running":
            summary += " The previous version is still running."
        reasons.append(summary)
    elif site["status"] == "error":
        reasons.append(site["last_error"] or "The hosting process failed.")
    elif site["status"] == "running" and not site["runtime_backend"] and not (
        "kind" in site.keys() and site["kind"] == "app"
    ):
        reasons.append("The site is marked running but has no active hosting backend.")
    document_root = Path(site["document_root"])
    if not document_root.is_dir():
        reasons.append("The selected repository folder is missing.")
    elif is_app:
        if not (document_root / app_support.DOCKERFILE_NAME).is_file():
            reasons.append("The app's Dockerfile is missing.")
    elif not (document_root / site["index_file"]).is_file():
        reasons.append(f"The required index page {site['index_file']} is missing.")
    if (
        "repository_update_state" in site.keys()
        and site["repository_update_state"] == "failed"
    ):
        prefix = (
            "Automatic update failed"
            if site["repository_update_mode"] == "auto"
            else "Source update check failed"
        )
        reasons.append(
            f"{prefix}: "
            f"{site['repository_update_error'] or site['repository_error'] or 'unknown error'}"
        )
    if "repository_pending_commit" in site.keys() and site["repository_pending_commit"]:
        reasons.append(
            f"A source update ({site['repository_pending_commit'][:7]}) is ready "
            "and waiting for approval."
        )
    return list(dict.fromkeys(reasons))


def recent_site_log(site_id: int, limit=80):
    path = Path(current_app.config["LOG_ROOT"]) / f"site-{site_id}.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def requested_additional_domains(database, primary_domain_id: int | None):
    requested_ids = {
        int(value)
        for value in request.form.getlist("additional_domain_ids")
        if value.isdigit()
    }
    if primary_domain_id:
        requested_ids.discard(primary_domain_id)
    if not requested_ids:
        return []
    placeholders = ",".join("?" for _ in requested_ids)
    rows = database.execute(
        f"SELECT * FROM domains WHERE id IN ({placeholders}) ORDER BY name COLLATE NOCASE",
        tuple(sorted(requested_ids)),
    ).fetchall()
    if len(rows) != len(requested_ids):
        abort(400, "Unknown additional domain.")
    return rows


def requested_alias_bindings(
    database,
    primary_domain_id: int | None,
    site_slug: str,
    legacy_use_domain_root=False,
):
    if "alias_configuration" not in request.form:
        return [
            {
                "domain": domain,
                "use_domain_root": legacy_use_domain_root,
                "hostname_prefix": None,
            }
            for domain in requested_additional_domains(database, primary_domain_id)
        ]

    bindings = []
    domains = database.execute(
        "SELECT * FROM domains ORDER BY name COLLATE NOCASE"
    ).fetchall()
    for domain in domains:
        if domain["id"] == primary_domain_id:
            continue
        if request.form.get(f"alias_enabled_{domain['id']}") != "on":
            continue
        mode = request.form.get(f"alias_mode_{domain['id']}", "alternate")
        if mode not in {"alternate", "subdomain", "root"}:
            abort(400, "Unknown alias hosting mode.")
        prefix = None
        if mode == "subdomain":
            prefix = request.form.get(f"alias_prefix_{domain['id']}", "").strip().lower()
            if not HOST_LABEL_RE.fullmatch(prefix):
                raise ValueError(
                    f"Enter a valid subdomain for {domain['name']}."
                )
        bindings.append(
            {
                "domain": domain,
                "use_domain_root": mode == "root",
                "hostname_prefix": prefix if prefix != site_slug else None,
            }
        )
    return bindings


def validate_site_domains(
    database,
    domains,
    use_domain_root,
    exclude_site_id=None,
    allowed_blocked_domain_ids=(),
):
    blocked = blocked_domain_names(database)
    allowed_blocked_domain_ids = set(allowed_blocked_domain_ids)
    for domain in domains:
        if (
            domain_is_blocked(domain["name"], blocked)
            and domain["id"] not in allowed_blocked_domain_ids
        ):
            raise ValueError(f"{domain['name']} is blocked by an administrator.")
        hostname = domain["name"] if use_domain_root else None
        if hostname and domain_is_dashboard(database, hostname):
            raise ValueError(
                "The WebManager dashboard hostname cannot also host a site."
            )
        if use_domain_root:
            owner = domain_root_owner(database, domain["id"], exclude_site_id)
            if owner:
                raise ValueError(
                    f"{domain['name']} already has a site at its root."
                )


def validate_site_bindings(
    database,
    bindings,
    site_slug,
    exclude_site_id=None,
    allowed_blocked_domain_ids=(),
):
    blocked = blocked_domain_names(database)
    allowed_blocked_domain_ids = set(allowed_blocked_domain_ids)
    hostnames = set()
    site_stub = {"slug": site_slug}
    for binding in bindings:
        domain = binding["domain"]
        if (
            domain_is_blocked(domain["name"], blocked)
            and domain["id"] not in allowed_blocked_domain_ids
        ):
            raise ValueError(f"{domain['name']} is blocked by an administrator.")
        normalized = {
            "domain": domain["name"],
            "use_domain_root": binding["use_domain_root"],
            "hostname_prefix": binding.get("hostname_prefix"),
        }
        hostname = binding_hostname(site_stub, normalized)
        if hostname in hostnames:
            raise ValueError(f"{hostname} is selected more than once.")
        hostnames.add(hostname)
        if domain_is_dashboard(database, hostname):
            raise ValueError(
                "The WebManager dashboard hostname cannot also host a site."
            )
        if binding["use_domain_root"]:
            owner = domain_root_owner(database, domain["id"], exclude_site_id)
            if owner:
                raise ValueError(
                    f"{domain['name']} already has a site at its root."
                )
        owner = hostname_owner(database, hostname, exclude_site_id)
        if owner:
            raise ValueError(
                f"{hostname} is already used by {owner['name']}."
            )


def refresh_routing():
    """Rewrite Nginx configs and reload so addresses match the database."""
    runtime = current_app.extensions["runtime_manager"]
    try:
        if runtime.nginx_binary:
            runtime.apply_nginx_configs()
        else:
            runtime.sync_nginx_configs()
    except RuntimeErrorDetail as exc:
        current_app.logger.warning("Could not refresh Nginx routing: %s", exc)


def repository_path_is_managed(path: str | Path) -> bool:
    root = Path(current_app.config["REPOSITORY_ROOT"]).resolve()
    candidate = Path(path).resolve()
    return candidate != root and root in candidate.parents


def owned_repository(repository_id: int, manage=False):
    repository = get_db().execute(
        "SELECT * FROM repositories WHERE id = ?",
        (repository_id,),
    ).fetchone()
    allowed = (
        can_manage_resource(repository["user_id"])
        if repository is not None and manage
        else repository is not None and can_view_resource(repository["user_id"])
    )
    if not allowed:
        abort(404)
    return repository


def owned_site(site_id: int, manage=False):
    database = get_db()
    site = database.execute(
        """
        SELECT sites.*, repositories.name AS repository_name,
               repositories.url AS repository_url,
               repositories.pending_commit AS repository_pending_commit,
               repositories.pending_at AS repository_pending_at,
               repositories.update_mode AS repository_update_mode,
               repositories.auto_refresh_minutes AS repository_auto_refresh_minutes,
               repositories.last_checked_at AS repository_last_checked_at,
               repositories.last_update_at AS repository_last_update_at,
               repositories.update_state AS repository_update_state,
               repositories.update_error AS repository_update_error,
               repositories.error AS repository_error,
               domains.name AS domain_name
        FROM sites
        JOIN repositories ON repositories.id = sites.repository_id
        LEFT JOIN domains ON domains.id = sites.domain_id
        WHERE sites.id = ?
        """,
        (site_id,),
    ).fetchone()
    allowed = site is not None and (
        can_manage_site(site, database) if manage else can_view_site(site, database)
    )
    if not allowed:
        abort(404)
    return site


@bp.get("/")
@login_required
def dashboard():
    database = get_db()
    active_view = request.args.get("view", "sites")
    if active_view not in {"sites", "apps", "sources"}:
        active_view = "sites"
    if active_view == "apps" and not current_app.config.get("APPS_ENABLED"):
        active_view = "sites"
    show_all = has_permission(RESOURCE_VIEW_ALL) or has_permission(RESOURCE_MANAGE_ALL)
    sites = database.execute(
        """
        SELECT sites.*, repositories.name AS repository_name,
               repositories.error AS repository_error,
               repositories.update_state AS repository_update_state,
               repositories.update_error AS repository_update_error,
               repositories.auto_refresh_minutes AS repository_auto_refresh_minutes,
               repositories.update_mode AS repository_update_mode,
               repositories.pending_commit AS repository_pending_commit,
               users.display_name AS owner_name, users.email AS owner_email,
               pools.name AS pool_name, domains.name AS domain_name
        FROM sites JOIN repositories ON repositories.id = sites.repository_id
        JOIN users ON users.id = sites.user_id
        LEFT JOIN domains ON domains.id = sites.domain_id
        LEFT JOIN pool_sites ON pool_sites.site_id = sites.id
        LEFT JOIN pools ON pools.id = pool_sites.pool_id
        ORDER BY sites.created_at DESC
        """
    ).fetchall()
    access_levels = site_access_levels(database, g.user["id"])
    if not show_all:
        sites = [
            site
            for site in sites
            if site["user_id"] == g.user["id"] or site["id"] in access_levels
        ]
    if active_view == "apps":
        sites = [site for site in sites if site["kind"] == "app"]
    elif active_view == "sites":
        sites = [site for site in sites if site["kind"] != "app"]
    manageable_site_ids = {
        site["id"]
        for site in sites
        if site["user_id"] == g.user["id"]
        or has_permission(RESOURCE_MANAGE_ALL)
        or access_levels.get(site["id"], 0) >= 2
    }
    site_domain_summaries = {
        site["id"]: site_domain_summary(site)
        for site in sites
    }
    attention_reasons = {
        site["id"]: site_attention_reasons(site)
        for site in sites
    }
    repository_where = "" if show_all else "WHERE repositories.user_id = ?"
    parameters = () if show_all else (g.user["id"],)
    repositories = database.execute(
        f"""
        SELECT repositories.*, COUNT(sites.id) AS site_count,
               GROUP_CONCAT(sites.name, ', ') AS affected_site_names,
               users.display_name AS owner_name, users.email AS owner_email
        FROM repositories LEFT JOIN sites ON sites.repository_id = repositories.id
        JOIN users ON users.id = repositories.user_id
        {repository_where}
        GROUP BY repositories.id
        ORDER BY repositories.created_at DESC
        """,
        parameters,
    ).fetchall()
    summary = {
        "running": sum(site["status"] == "running" for site in sites),
        "stopped": sum(site["status"] == "stopped" for site in sites),
        "attention": sum(bool(attention_reasons[site["id"]]) for site in sites),
        "repositories": len(repositories),
    }
    repository_update_states = {
        repository["id"]: repository_update_display(repository)
        for repository in repositories
    }
    return render_template(
        "dashboard.html",
        title={"sites": "Sites", "apps": "Apps"}.get(active_view, "Sources"),
        sites=sites,
        repositories=repositories,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        summary=summary,
        host=request_hostname(),
        site_public_url=site_public_url,
        site_public_urls=site_public_urls,
        site_domains=available_domains(database),
        refresh_min=MIN_REFRESH_MINUTES,
        refresh_max=MAX_REFRESH_MINUTES,
        show_all=show_all,
        manage_all=has_permission(RESOURCE_MANAGE_ALL),
        manageable_site_ids=manageable_site_ids,
        site_domain_summaries=site_domain_summaries,
        attention_reasons=attention_reasons,
        repository_update_states=repository_update_states,
        auto_refresh_service_enabled=current_app.config["AUTO_REFRESH_ENABLED"],
        active_view=active_view,
        quota=quotas.summary(database, g.user["id"]),
        apps_enabled=bool(current_app.config.get("APPS_ENABLED")),
        can_host_apps=can_host_apps(),
        container_stats=(
            current_app.extensions["runtime_manager"].stats_for_sites(
                [site["id"] for site in sites]
            )
            if active_view == "apps"
            else {}
        ),
    )


ANALYTICS_PERIODS = (7, 30, 90)
REFRESH_COOLDOWN_SECONDS = 15


def visible_sites(database):
    """Sites the signed-in user may view (all of them for view-all roles)."""
    show_all = has_permission(RESOURCE_VIEW_ALL) or has_permission(RESOURCE_MANAGE_ALL)
    sites = database.execute(
        """
        SELECT sites.*, repositories.name AS repository_name,
               users.display_name AS owner_name, users.email AS owner_email,
               pools.name AS pool_name, domains.name AS domain_name
        FROM sites JOIN repositories ON repositories.id = sites.repository_id
        JOIN users ON users.id = sites.user_id
        LEFT JOIN domains ON domains.id = sites.domain_id
        LEFT JOIN pool_sites ON pool_sites.site_id = sites.id
        LEFT JOIN pools ON pools.id = pool_sites.pool_id
        ORDER BY sites.name COLLATE NOCASE
        """
    ).fetchall()
    if show_all:
        return sites, True
    access_levels = site_access_levels(database, g.user["id"])
    return [
        site
        for site in sites
        if site["user_id"] == g.user["id"] or site["id"] in access_levels
    ], False


@bp.get("/analytics")
@login_required
def analytics():
    database = get_db()
    sites, show_all = visible_sites(database)
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    if days not in ANALYTICS_PERIODS:
        days = 30
    requested_site = request.args.get("site", "all")
    selected = next(
        (site for site in sites if str(site["id"]) == requested_site),
        None,
    )
    hostnames = {site["id"]: site_hostnames(database, site) for site in sites}
    scope = {selected["id"]: hostnames[selected["id"]]} if selected else hostnames
    data = aggregate_analytics(
        Path(current_app.config["NGINX_ROOT"]) / "access.log",
        scope,
        days,
    )
    ranked = sorted(
        (site for site in sites if site["id"] in scope),
        key=lambda site: data["per_site"][site["id"]]["requests"],
        reverse=True,
    )
    return render_template(
        "analytics.html",
        title="Analytics",
        data=data,
        sites=sites,
        ranked_sites=ranked,
        selected_site=selected,
        hostnames=hostnames,
        days=days,
        periods=ANALYTICS_PERIODS,
        show_all=show_all,
    )


@bp.post("/repositories/inspect")
@login_required
def inspect_repository():
    validate_csrf()
    database = get_db()
    raw_url = request.form.get("repository_url", "")
    branch = request.form.get("branch", "").strip() or None

    try:
        if branch and len(branch) > 200:
            raise GitError("Branch name must be 200 characters or fewer.")
        url = validate_repo_url(raw_url)
        if not g.user["is_admin"]:
            check_repository_host(url)
    except GitError as exc:
        flash(str(exc), "error")
        return action_redirect("deployments.dashboard", view="sources")

    limit_error = quotas.check(database, g.user["id"], "sources")
    if limit_error:
        flash(limit_error, "error")
        return action_redirect("deployments.dashboard", view="sources")

    name = repo_name_from_url(url)
    cursor = database.execute(
        """
        INSERT INTO repositories (
            user_id, name, url, branch, local_path, status,
            auto_refresh_minutes, next_refresh_at, update_mode
        )
        VALUES (?, ?, ?, ?, '', 'cloning', ?, ?, 'approval')
        """,
        (
            g.user["id"], name, display_repo_url(url), branch,
            DEFAULT_CHECK_MINUTES, next_refresh_time(DEFAULT_CHECK_MINUTES),
        ),
    )
    repository_id = cursor.lastrowid
    target = Path(current_app.config["REPOSITORY_ROOT"]) / str(g.user["id"]) / str(repository_id)
    database.execute(
        "UPDATE repositories SET local_path = ? WHERE id = ?",
        (str(target.resolve()), repository_id),
    )
    database.commit()

    try:
        clone_repository(
            url, target, branch,
            max_bytes=current_app.config.get("MAX_REPOSITORY_BYTES") or None,
        )
        current_commit = repository_commit(target)
        candidates = find_index_folders(target)
        database.execute(
            """
            UPDATE repositories
            SET status = 'ready', error = NULL,
                update_state = 'idle', update_error = NULL,
                last_checked_at = CURRENT_TIMESTAMP,
                current_commit = ?,
                last_refreshed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (current_commit, repository_id),
        )
        database.commit()
    except GitError as exc:
        # Don't leave a broken source behind (it would also use up one of the
        # person's source slots); just explain what went wrong.
        database.execute("DELETE FROM repositories WHERE id = ?", (repository_id,))
        database.commit()
        if repository_path_is_managed(target):
            shutil.rmtree(target, ignore_errors=True)
        flash(f"Could not clone {display_repo_url(url)}: {exc}", "error")
        return redirect(
            url_for("deployments.dashboard", view="sources", connect=1) + "#connect",
            code=303,
        )

    if not candidates:
        flash("Repository cloned, but no index.html or index.htm file was found.", "warning")
    return redirect(url_for("deployments.select_folder", repository_id=repository_id))


@bp.get("/repositories/<int:repository_id>/select")
@login_required
def select_folder(repository_id):
    repository = owned_repository(repository_id, manage=True)
    candidates = find_index_folders(Path(repository["local_path"]))
    suggested_name = _title_from(repository["name"])
    for candidate in candidates:
        parts = [] if candidate["folder"] == "." else candidate["folder"].split("/")
        meaningful = [part for part in parts if part.lower() not in GENERIC_FOLDERS]
        candidate["suggested_name"] = (
            _title_from(meaningful[-1]) if meaningful else suggested_name
        )
        candidate["suggested_slug"] = slugify(candidate["suggested_name"])
        candidate["parent"] = "/".join(parts[:-1])
        candidate["leaf"] = parts[-1] if parts else ""
        candidate["is_build_output"] = bool(parts) and parts[-1].lower() in BUILD_FOLDERS
    recommended = next(
        (index for index, candidate in enumerate(candidates) if candidate["is_build_output"]),
        0,
    )
    app_candidates = app_support.find_app_folders(Path(repository["local_path"]))
    return render_template(
        "select_folder.html",
        title="Select site folder",
        app_candidates=app_candidates,
        can_host_apps=can_host_apps(),
        apps_enabled=bool(current_app.config.get("APPS_ENABLED")),
        repository=repository,
        candidates=candidates,
        suggested_name=suggested_name,
        suggested_slug=slugify(suggested_name),
        recommended_index=recommended,
        quota=quotas.summary(get_db(), repository["user_id"]),
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        site_domains=available_domains(get_db()),
        site_public_scheme=current_app.config["SITE_PUBLIC_SCHEME"],
    )


@bp.post("/repositories/<int:repository_id>/refresh")
@login_required
def refresh_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    recent = get_db().execute(
        """
        SELECT (julianday('now') - julianday(last_checked_at)) * 86400 AS age
        FROM repositories WHERE id = ?
        """,
        (repository_id,),
    ).fetchone()
    if (
        recent["age"] is not None
        and recent["age"] < REFRESH_COOLDOWN_SECONDS
        and repository["update_state"] != "failed"
    ):
        flash("Checked a moment ago; nothing new yet. Try again in a few seconds.", "info")
        return action_redirect("deployments.dashboard", view="sources")
    result = current_app.extensions["repository_refresh_manager"].refresh(repository_id)
    if result.status in {"applied", "current"}:
        flash(result.message, "success")
    elif result.status in {"available", "busy"}:
        flash(result.message, "warning")
    else:
        flash(f"Update check failed: {result.message}", "error")
    return action_redirect("deployments.dashboard", view="sources")


@bp.post("/repositories/<int:repository_id>/updates/force")
@login_required
def force_repository_update(repository_id):
    """Install the latest commit right away, skipping the safety checks that
    left this source showing "Update check failed" - an explicit override,
    not a normal check."""
    validate_csrf()
    owned_repository(repository_id, manage=True)
    result = current_app.extensions["repository_refresh_manager"].force_update(repository_id)
    if result.status in {"applied", "current"}:
        flash(result.message, "success")
    elif result.status == "busy":
        flash(result.message, "warning")
    else:
        flash(f"Could not force the update through: {result.message}", "error")
    return action_redirect("deployments.dashboard", view="sources")


@bp.post("/repositories/<int:repository_id>/schedule")
@login_required
def schedule_repository_refresh(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    update_mode = request.form.get("update_mode", "approval")
    if update_mode not in {"approval", "auto"}:
        abort(400, "Unknown update mode.")

    if update_mode == "approval":
        # Checks never stop; owners are notified and approve each update.
        minutes = DEFAULT_CHECK_MINUTES
    else:
        raw_amount = request.form.get(
            "auto_update_every", request.form.get("auto_refresh_minutes", "")
        ).strip()
        unit = request.form.get("auto_update_unit", "minutes")
        if unit not in INTERVAL_UNITS:
            abort(400, "Unknown interval unit.")
        try:
            minutes = int(raw_amount) * INTERVAL_UNITS[unit]
        except ValueError:
            flash("Enter how often to update as a whole number.", "error")
            return action_redirect("deployments.dashboard", view="sources")
        if minutes < MIN_REFRESH_MINUTES or minutes > MAX_REFRESH_MINUTES:
            flash(
                f"Automatic updates must run between every {MIN_REFRESH_MINUTES} "
                f"minutes and every {format_interval(MAX_REFRESH_MINUTES)}.",
                "error",
            )
            return action_redirect("deployments.dashboard", view="sources")
    next_run = next_refresh_time(minutes)

    database = get_db()
    database.execute(
        """
        UPDATE repositories
        SET auto_refresh_minutes = ?, next_refresh_at = ?, update_mode = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (minutes, next_run, update_mode, repository_id),
    )
    database.commit()
    if update_mode == "auto":
        flash(
            f"Updates will install automatically every {format_interval(minutes)}.",
            "success",
        )
    else:
        flash(
            "Automatic updates turned off. WebManager keeps checking every "
            f"{format_interval(minutes)} and will ask you to approve new versions.",
            "success",
        )
    if update_mode == "auto":
        result = current_app.extensions[
            "repository_refresh_manager"
        ].apply_pending(repository_id)
        if result.status == "applied":
            flash(result.message, "success")
        elif repository["pending_commit"] and result.status != "missing":
            flash(f"Could not apply pending update: {result.message}", "error")
    return action_redirect("deployments.dashboard", view="sources")


@bp.post("/repositories/<int:repository_id>/updates/approve")
@login_required
def approve_repository_update(repository_id):
    validate_csrf()
    owned_repository(repository_id, manage=True)
    result = current_app.extensions["repository_refresh_manager"].apply_pending(
        repository_id
    )
    if result.status == "applied":
        flash(result.message, "success")
    elif result.status == "busy":
        flash(result.message, "warning")
    else:
        flash(f"Could not apply update: {result.message}", "error")
    return action_redirect("deployments.dashboard", view="sources")


@bp.post("/repositories/<int:repository_id>/updates/discard")
@login_required
def discard_repository_update(repository_id):
    validate_csrf()
    owned_repository(repository_id, manage=True)
    result = current_app.extensions[
        "repository_refresh_manager"
    ].discard_pending(repository_id)
    flash(
        result.message,
        "warning" if result.status == "busy" else "success",
    )
    return action_redirect("deployments.dashboard", view="sources")


@bp.post("/repositories/<int:repository_id>/deploy")
@login_required
def deploy_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    owner_id = repository["user_id"]
    database = get_db()
    requested_domain = request.form.get("domain_id", "").strip()
    if requested_domain:
        if not requested_domain.isdigit():
            abort(400, "Unknown domain.")
        domain = database.execute(
            "SELECT * FROM domains WHERE id = ?",
            (int(requested_domain),),
        ).fetchone()
    else:
        domain = default_domain(database)
    if requested_domain and domain is None:
        abort(400, "Unknown domain.")
    additional_domains = requested_additional_domains(
        database,
        domain["id"] if domain else None,
    )
    selected_domains = ([domain] if domain else []) + additional_domains
    hosting_mode = request.form.get("hosting_mode", "")
    use_domain_root = (
        hosting_mode == "root"
        or request.form.get("use_domain_root") == "on"
    )
    deployments = []
    selected_indexes = list(
        dict.fromkeys(
            value for value in request.form.getlist("selected") if value.isdigit()
        )
    )
    if request.form.get("multi_deploy") == "1" and not selected_indexes:
        flash("Select at least one site folder to deploy.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if use_domain_root and len(selected_indexes) != 1:
        flash("Select exactly one site when hosting at the domain root.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if use_domain_root and domain is None:
        flash("Select a public domain before using its root address.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    try:
        validate_site_domains(database, selected_domains, use_domain_root)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if selected_indexes:
        for index in selected_indexes:
            deployments.append(
                {
                    "name": request.form.get(f"site_name_{index}", "").strip(),
                    "folder": request.form.get(f"folder_{index}", ""),
                    "spa_fallback": request.form.get(f"spa_fallback_{index}") == "on",
                    "slug": request.form.get(f"slug_{index}", "").strip().lower(),
                    "port": "",
                }
            )
    else:
        deployments.append(
            {
                "name": request.form.get("site_name", "").strip(),
                "folder": request.form.get("folder", ""),
                "spa_fallback": request.form.get("spa_fallback") == "on",
                "port": request.form.get("port", "").strip(),
            }
        )

    if not deployments or any(
        not valid_site_name(item["name"]) for item in deployments
    ):
        flash("Every selected site needs a name of 80 characters or fewer.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))

    if not g.user["is_admin"]:
        limit_error = quotas.check(database, owner_id, "sites", adding=len(deployments))
        if limit_error:
            flash(limit_error, "error")
            return redirect(url_for("deployments.select_folder", repository_id=repository_id))

    created = []
    for item in deployments:
        try:
            selected = resolve_folder(
                Path(repository["local_path"]),
                item["folder"],
            )
            index_files = {
                path.name.lower(): path.name
                for path in selected.iterdir()
                if path.is_file()
            }
            index_file = index_files.get("index.html") or index_files.get("index.htm")
            if not index_file:
                raise GitError(
                    f"{item['folder']} no longer contains an index page."
                )
            requested_port = int(item["port"]) if item["port"] else None
            port = allocate_port(
                database,
                current_app.config["SITE_PORT_MIN"],
                current_app.config["SITE_PORT_MAX"],
                requested_port,
            )
            if item.get("slug") and not use_domain_root:
                if not HOST_LABEL_RE.fullmatch(item["slug"]):
                    raise ValueError(
                        f"{item['slug']} is not a valid subdomain. Use letters, numbers, and dashes."
                    )
                if database.execute(
                    "SELECT 1 FROM sites WHERE slug = ?", (item["slug"],)
                ).fetchone():
                    raise ValueError(f"The subdomain {item['slug']} is already taken.")
                slug = item["slug"]
            else:
                slug = unique_slug(database, item["name"])
            hostnames = [
                selected_domain["name"]
                if use_domain_root
                else f"{slug}.{selected_domain['name']}"
                for selected_domain in selected_domains
            ]
            for hostname in hostnames:
                if domain_is_dashboard(database, hostname):
                    raise ValueError(
                        "The generated hostname is reserved for the WebManager dashboard."
                    )
                owner = hostname_owner(database, hostname)
                if owner:
                    raise ValueError(f"{hostname} is already used by {owner['name']}.")
            config = build_site_config(
                item["name"],
                selected,
                index_file,
                port,
                item["spa_fallback"],
                hostnames,
                current_app.config["SITE_GATEWAY_PORT"] if hostnames else None,
            )
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, domain_id, use_domain_root,
                    name, slug, folder, document_root,
                    index_file, port, spa_fallback, nginx_config, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped')
                """,
                (
                    owner_id,
                    repository_id,
                    domain["id"] if domain else None,
                    int(use_domain_root),
                    item["name"],
                    slug,
                    item["folder"],
                    str(selected),
                    index_file,
                    port,
                    int(item["spa_fallback"]),
                    config,
                ),
            )
            site_id = cursor.lastrowid
            database.executemany(
                """
                INSERT INTO site_domain_aliases (
                    site_id, domain_id, use_domain_root
                ) VALUES (?, ?, ?)
                """,
                (
                    (site_id, alias["id"], int(use_domain_root))
                    for alias in additional_domains
                ),
            )
            database.commit()
        except (GitError, RuntimeErrorDetail, ValueError, sqlite3.IntegrityError) as exc:
            database.rollback()
            flash(f"Could not deploy {item['name']}: {exc}", "error")
            continue

        created.append(
            {
                "id": site_id,
                "slug": slug,
                "port": port,
                "name": item["name"],
                "domain_id": domain["id"] if domain else None,
                "domain_name": domain["name"] if domain else None,
                "use_domain_root": int(use_domain_root),
            }
        )
        try:
            current_app.extensions["runtime_manager"].start_site(site_id)
        except RuntimeErrorDetail as exc:
            flash(f"{item['name']} was created, but hosting failed: {exc}", "warning")

    if not created:
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if len(created) == 1:
        deployed = created[0]
        flash(
            f"Site deployed at {site_public_url(deployed)}.",
            "success",
        )
        return redirect(url_for("deployments.site_detail", site_id=deployed["id"]))
    flash(
        f"Deployed {len(created)} sites from {repository['name']}.",
        "success",
    )
    return action_redirect("deployments.dashboard", view="sites")


@bp.post("/repositories/<int:repository_id>/deploy-app")
@login_required
def deploy_app(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    back = redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if not can_host_apps():
        abort(403)
    runtime = current_app.extensions["runtime_manager"]
    ready, message = runtime.apps_status()
    if not ready:
        flash(message, "error")
        return back
    database = get_db()
    owner_id = repository["user_id"]
    if not g.user["is_admin"]:
        for kind in ("sites", "apps"):
            limit_error = quotas.check(database, owner_id, kind)
            if limit_error:
                flash(limit_error, "error")
                return back

    name = request.form.get("site_name", "").strip()
    slug = request.form.get("slug", "").strip().lower() or slugify(name)
    if not valid_site_name(name):
        flash("Give the app a name of 80 characters or fewer.", "error")
        return back
    if not HOST_LABEL_RE.fullmatch(slug):
        flash("Use lowercase letters, numbers, and dashes for the subdomain.", "error")
        return back
    if database.execute("SELECT 1 FROM sites WHERE slug = ?", (slug,)).fetchone():
        flash(f"The subdomain {slug} is already taken.", "error")
        return back
    raw_domain = request.form.get("domain_id", "").strip()
    domain = (
        database.execute("SELECT * FROM domains WHERE id = ?", (int(raw_domain),)).fetchone()
        if raw_domain.isdigit()
        else default_domain(database)
    )
    if domain is None:
        flash("Apps need a public domain. Ask an administrator to add one under Domains.", "error")
        return back
    try:
        validate_site_domains(database, [domain], False)
        hostname = f"{slug}.{domain['name']}"
        if domain_is_dashboard(database, hostname):
            raise ValueError("That hostname is reserved for the WebManager dashboard.")
        owner = hostname_owner(database, hostname)
        if owner:
            raise ValueError(f"{hostname} is already used by {owner['name']}.")
        folder = request.form.get("folder", ".")
        selected = resolve_folder(Path(repository["local_path"]), folder)
        manifest = app_support.load_manifest(selected, current_app.config["APP_DEFAULT_MEMORY_MB"])
        port = allocate_port(
            database,
            current_app.config["SITE_PORT_MIN"],
            current_app.config["SITE_PORT_MAX"],
        )
        cursor = database.execute(
            """
            INSERT INTO sites (
                user_id, repository_id, domain_id, use_domain_root, name, slug,
                folder, document_root, index_file, port, spa_fallback,
                nginx_config, status, kind, app_memory_mb
            ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, '', ?, 0, '', 'stopped', 'app', ?)
            """,
            (owner_id, repository_id, domain["id"], name, slug, folder, str(selected), port, manifest["memory_mb"]),
        )
        database.commit()
    except (GitError, ValueError, RuntimeErrorDetail, sqlite3.IntegrityError) as exc:
        database.rollback()
        flash(f"Could not deploy the app: {exc}", "error")
        return back

    site_id = cursor.lastrowid
    missing = [v["name"] for v in manifest["env"] if v["required"] and v["default"] is None]
    if missing:
        flash(
            f"{name} was created. Fill in the required settings ({', '.join(missing)}), then save to start it.",
            "warning",
        )
        return redirect(url_for("deployments.app_variables", site_id=site_id))
    runtime.start_app_async(site_id)
    flash(f"Building and starting {name}. This page updates when it's ready.", "success")
    return redirect(url_for("deployments.site_detail", site_id=site_id))


@bp.route("/sites/<int:site_id>/variables", methods=("GET", "POST"))
@login_required
def app_variables(site_id):
    site = owned_site(site_id, manage=True)
    if site["kind"] != "app":
        abort(404)
    manifest, manifest_error = app_manifest(site)
    secret_key = current_app.config["SECRET_KEY"]
    try:
        stored = app_support.decrypt_env(site["app_env"], secret_key)
        decrypt_error = None
    except app_support.AppError as exc:
        stored, decrypt_error = {}, str(exc)
    errors = []
    if request.method == "POST" and manifest:
        validate_csrf()
        updated = {}
        for variable in manifest["env"]:
            name = variable["name"]
            raw = request.form.get(f"var_{name}", "")
            if variable["secret"]:
                if request.form.get(f"clear_{name}") == "on":
                    continue
                value = raw if raw != "" else stored.get(name, "")
            else:
                value = raw.strip()
            if value == "":
                continue
            problem = app_support.validate_value(name, value, variable)
            if problem:
                errors.append(problem)
            updated[name] = value
        missing = [
            v["name"]
            for v in manifest["env"]
            if v["required"] and not updated.get(v["name"]) and v["default"] is None
        ]
        if missing:
            errors.append(f"Required: {', '.join(missing)}.")
        if not errors:
            database = get_db()
            database.execute(
                "UPDATE sites SET app_env = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (app_support.encrypt_env(updated, secret_key), site_id),
            )
            database.commit()
            runtime = current_app.extensions["runtime_manager"]
            container_runtime = runtime.container_runtime
            never_started = container_runtime is not None and container_runtime.inspect(
                container_runtime.container_name(site_id)
            ) is None
            # Restart running/failed apps; start brand-new ones. A deliberately
            # stopped app stays stopped.
            if site["status"] in ("running", "error", "starting") or (
                site["status"] == "stopped" and never_started
            ):
                if runtime.start_app_async(site_id, force=True):
                    flash("Settings saved. Restarting the app with the new values.", "success")
                else:
                    flash("Settings saved. The app is busy; restart it when the current operation finishes.", "warning")
            else:
                flash("Settings saved.", "success")
            return redirect(url_for("deployments.site_detail", site_id=site_id))
        for problem in errors:
            flash(problem, "error")
        stored = {**stored, **{k: v for k, v in updated.items()}}
    return render_template(
        "app_variables.html",
        title=f"Variables for {site['name']}",
        site=site,
        manifest=manifest,
        manifest_error=manifest_error,
        decrypt_error=decrypt_error,
        stored=stored,
    ), (422 if errors else 200)


@bp.get("/docs")
@login_required
def docs():
    return render_template(
        "docs.html",
        title="Docs",
        apps_enabled=bool(current_app.config.get("APPS_ENABLED")),
        can_host_apps=can_host_apps(),
        mesh_configured=bool(current_app.extensions["mesh_hub"].urls),
    )


@bp.get("/sites/<int:site_id>")
@login_required
def site_detail(site_id):
    site = owned_site(site_id)
    database = get_db()
    hostname = site_hostname(database, site) or request_hostname()
    hostnames = site_hostnames(database, site) or [hostname]
    return render_template(
        "site_detail.html",
        title=site["name"],
        site=site,
        host=request_hostname(),
        public_url=site_public_url(site),
        public_urls=site_public_urls(site),
        site_public_scheme=current_app.config["SITE_PUBLIC_SCHEME"],
        nginx_available=bool(current_app.extensions["runtime_manager"].nginx_binary),
        can_manage=can_manage_site(site, database),
        can_manage_repository=can_manage_resource(site["user_id"]),
        routing=site_routing_details(site),
        domain_summary=site_domain_summary(site),
        attention_reasons=site_attention_reasons(site),
        update_display=repository_update_display(site),
        site_log=recent_site_log(site_id),
        app=(
            current_app.extensions["runtime_manager"].app_details(site)
            if site["kind"] == "app"
            else None
        ),
        app_manifest=app_manifest(site)[0] if site["kind"] == "app" else None,
        analytics=aggregate_analytics(
            Path(current_app.config["NGINX_ROOT"]) / "access.log",
            {site["id"]: hostnames},
            30,
        ),
    )


@bp.get("/sites/<int:site_id>/status.json")
@login_required
def site_status_json(site_id):
    """Live container status/usage for the app page, polled every few seconds."""
    site = owned_site(site_id)
    if site["kind"] != "app":
        abort(404)
    runtime = current_app.extensions["runtime_manager"]
    response = jsonify(runtime.app_status(site))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/apps/stats.json")
@login_required
def apps_stats_json():
    """Live CPU/memory for the signed-in user's Apps view, polled every few seconds."""
    database = get_db()
    sites, _show_all = visible_sites(database)
    app_ids = [site["id"] for site in sites if site["kind"] == "app"]
    runtime = current_app.extensions["runtime_manager"]
    stats = runtime.stats_for_sites(app_ids)
    response = jsonify({str(site_id): value for site_id, value in stats.items()})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/sites/<int:site_id>/settings", methods=("GET", "POST"))
@login_required
def site_settings(site_id):
    site = owned_site(site_id, manage=True)
    database = get_db()
    repository = database.execute(
        "SELECT * FROM repositories WHERE id = ?",
        (site["repository_id"],),
    ).fetchone()
    candidates = find_index_folders(Path(repository["local_path"]))
    domains = available_domains(database)
    current_domain = None
    current_domain_blocked = False
    alias_bindings = [
        binding
        for binding in site_domain_bindings(database, site)
        if not binding["is_primary"]
    ]
    current_bindings = {
        binding["domain_id"]: binding
        for binding in site_domain_bindings(database, site)
        if binding["domain_id"]
    }
    alias_domain_ids = {binding["domain_id"] for binding in alias_bindings}
    alias_bindings_by_id = {
        binding["domain_id"]: binding for binding in alias_bindings
    }
    if site["domain_id"]:
        current_domain = database.execute(
            "SELECT * FROM domains WHERE id = ?",
            (site["domain_id"],),
        ).fetchone()
        current_domain_blocked = bool(
            current_domain
            and domain_is_blocked(
                current_domain["name"],
                blocked_domain_names(database),
            )
        )
        if current_domain and all(
            domain["id"] != current_domain["id"] for domain in domains
        ):
            domains.append(current_domain)
    for binding in alias_bindings:
        if all(domain["id"] != binding["domain_id"] for domain in domains):
            alias_domain = database.execute(
                "SELECT * FROM domains WHERE id = ?",
                (binding["domain_id"],),
            ).fetchone()
            if alias_domain:
                domains.append(alias_domain)
    if request.method == "POST":
        validate_csrf()
        name = request.form.get("name", "").strip()
        slug = slugify(request.form.get("slug", "").strip() or name)
        folder = request.form.get("folder", "")
        spa_fallback = request.form.get("spa_fallback") == "on"
        hosting_mode = request.form.get("hosting_mode", "")
        use_domain_root = (
            hosting_mode == "root"
            or request.form.get("use_domain_root") == "on"
        )
        domain = None
        if "domain_id" in request.form:
            raw_domain_id = request.form.get("domain_id", "").strip()
            if raw_domain_id:
                if not raw_domain_id.isdigit():
                    abort(400, "Unknown domain.")
                domain = database.execute(
                    "SELECT * FROM domains WHERE id = ?",
                    (int(raw_domain_id),),
                ).fetchone()
                if domain is None:
                    abort(400, "Unknown domain.")
                if (
                    domain_is_blocked(
                        domain["name"],
                        blocked_domain_names(database),
                    )
                    and domain["id"] != site["domain_id"]
                ):
                    flash(
                        "The selected domain is blocked by an administrator.",
                        "error",
                    )
                    return redirect(
                        url_for("deployments.site_settings", site_id=site_id)
                    )
        elif site["domain_id"]:
            domain = database.execute(
                "SELECT * FROM domains WHERE id = ?",
                (site["domain_id"],),
            ).fetchone()
        else:
            domain = default_domain(database)
        try:
            requested_aliases = requested_alias_bindings(
                database,
                domain["id"] if domain else None,
                slug,
                legacy_use_domain_root=use_domain_root,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("deployments.site_settings", site_id=site_id),
                code=303,
            )
        selected_bindings = (
            [
                {
                    "domain": domain,
                    "use_domain_root": use_domain_root,
                    "hostname_prefix": None,
                }
            ]
            if domain
            else []
        ) + requested_aliases
        try:
            port = int(request.form.get("port", ""))
        except ValueError:
            port = -1
        is_app = site["kind"] == "app"
        if is_app:
            # Apps keep their folder and internal port; only name/address change.
            port = site["port"]
            folder = site["folder"]
            spa_fallback = False
            if domain is None:
                flash("Apps need a public domain.", "error")
                return redirect(url_for("deployments.site_settings", site_id=site_id), code=303)

        if not valid_site_name(name):
            flash("Site name is required and must be 80 characters or fewer.", "error")
        elif port < current_app.config["SITE_PORT_MIN"] or port > current_app.config["SITE_PORT_MAX"]:
            flash(
                f"Port must be between {current_app.config['SITE_PORT_MIN']} "
                f"and {current_app.config['SITE_PORT_MAX']}.",
                "error",
            )
        elif port != site["port"] and (
            database.execute(
                "SELECT 1 FROM sites WHERE port = ? AND id != ?",
                (port, site_id),
            ).fetchone()
            or not port_is_available(port)
        ):
            flash(f"Port {port} is already in use.", "error")
        elif use_domain_root and domain is None:
            flash("Select a public domain before using its root address.", "error")
        elif domain is None and requested_aliases:
            flash(
                "Choose a primary public domain before adding domain aliases.",
                "error",
            )
        else:
            try:
                validate_site_bindings(
                    database,
                    selected_bindings,
                    slug,
                    exclude_site_id=site_id,
                    allowed_blocked_domain_ids=current_bindings,
                )
                selected = resolve_folder(Path(repository["local_path"]), folder)
                if is_app:
                    index_file = ""
                else:
                    index_files = {
                        path.name.lower(): path.name
                        for path in selected.iterdir()
                        if path.is_file()
                    }
                    index_file = index_files.get("index.html") or index_files.get("index.htm")
                    if not index_file:
                        raise GitError("The selected folder does not contain an index page.")
                if slug != site["slug"] and database.execute(
                    "SELECT 1 FROM sites WHERE slug = ? AND id != ?",
                    (slug, site_id),
                ).fetchone():
                    raise ValueError(
                        f"The subdomain {slug} is already used by another site. Pick a different one."
                    )
                slug = unique_slug(database, slug, exclude_site_id=site_id)
                site_stub = {"slug": slug}
                hostnames = [
                    binding_hostname(
                        site_stub,
                        {
                            "domain": binding["domain"]["name"],
                            "use_domain_root": binding["use_domain_root"],
                            "hostname_prefix": binding.get("hostname_prefix"),
                        },
                    )
                    for binding in selected_bindings
                ]
                blocked = blocked_domain_names(database)
                for binding, hostname in zip(selected_bindings, hostnames):
                    selected_domain = binding["domain"]
                    previous = current_bindings.get(selected_domain["id"])
                    if (
                        previous
                        and domain_is_blocked(selected_domain["name"], blocked)
                    ):
                        previous_hostname = binding_hostname(site, previous)
                        if hostname != previous_hostname:
                            raise GitError(
                                "The current domain is blocked. Keep the existing "
                                "public hostname or move the site to an allowed domain."
                            )
                if is_app:
                    config = build_app_config(
                        name, port, hostnames, current_app.config["SITE_GATEWAY_PORT"]
                    )
                else:
                    config = build_site_config(
                        name,
                        selected,
                        index_file,
                        port,
                        spa_fallback,
                        hostnames,
                        current_app.config["SITE_GATEWAY_PORT"] if hostnames else None,
                    )
                database.execute(
                    """
                    UPDATE sites
                    SET domain_id = ?, use_domain_root = ?, name = ?, slug = ?,
                        folder = ?, document_root = ?,
                        index_file = ?, port = ?, spa_fallback = ?,
                        nginx_config = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        domain["id"] if domain else None,
                        int(use_domain_root),
                        name,
                        slug,
                        folder,
                        str(selected),
                        index_file,
                        port,
                        int(spa_fallback),
                        config,
                        site_id,
                    ),
                )
                database.execute(
                    "DELETE FROM site_domain_aliases WHERE site_id = ?",
                    (site_id,),
                )
                database.executemany(
                    """
                    INSERT INTO site_domain_aliases (
                        site_id, domain_id, use_domain_root, hostname_prefix
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            site_id,
                            alias["domain"]["id"],
                            int(alias["use_domain_root"]),
                            alias.get("hostname_prefix"),
                        )
                        for alias in requested_aliases
                    ),
                )
                database.commit()
            except (GitError, ValueError, sqlite3.IntegrityError) as exc:
                database.rollback()
                flash(f"Could not save site settings: {exc}", "error")
            else:
                runtime = current_app.extensions["runtime_manager"]
                if is_app and site["status"] == "running":
                    runtime.start_app_async(site_id, force=True)
                    flash("Settings saved. Restarting the app with its new address.", "success")
                    return redirect(url_for("deployments.site_detail", site_id=site_id))
                if site["status"] == "running":
                    try:
                        runtime.restart_site(site_id)
                    except RuntimeErrorDetail as exc:
                        flash(f"Settings saved, but restart failed: {exc}", "warning")
                    else:
                        flash("Site settings saved and the site restarted.", "success")
                else:
                    try:
                        if runtime.nginx_binary:
                            runtime.apply_nginx_configs()
                        else:
                            runtime.sync_nginx_configs()
                    except RuntimeErrorDetail as exc:
                        flash(f"Settings saved, but Nginx sync failed: {exc}", "warning")
                    else:
                        flash("Site settings saved.", "success")
                return redirect(url_for("deployments.site_detail", site_id=site_id))
        # Validation failed: show the form again with what was typed rather
        # than throwing the edits away.
        form_site = dict(site)
        form_site.update(
            name=name or site["name"],
            slug=request.form.get("slug", "").strip() or site["slug"],
            folder=folder or site["folder"],
            port=port if port > 0 else site["port"],
            spa_fallback=int(spa_fallback),
            use_domain_root=int(use_domain_root),
            domain_id=domain["id"] if domain else None,
        )
        status_code = 422
    else:
        form_site = site
        status_code = 200

    return render_template(
        "site_settings.html",
        title=f"Settings for {site['name']}",
        site=form_site,
        candidates=candidates,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        site_domains=domains,
        current_domain_blocked=current_domain_blocked,
        alias_domain_ids=alias_domain_ids,
        alias_bindings_by_id=alias_bindings_by_id,
        site_public_scheme=current_app.config["SITE_PUBLIC_SCHEME"],
    ), status_code


@bp.post("/sites/<int:site_id>/start")
@login_required
def start_site(site_id):
    validate_csrf()
    site = owned_site(site_id, manage=True)
    runtime = current_app.extensions["runtime_manager"]
    if site["kind"] == "app":
        if runtime.start_app_async(site_id):
            flash("Starting the app. This page updates when it's ready.", "success")
        else:
            flash("The app is already starting or updating.", "warning")
        return action_redirect("deployments.site_detail", site_id=site_id)
    try:
        backend = runtime.start_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Could not start site: {exc}", "error")
    else:
        flash(f"Site started with {backend}.", "success")
    return action_redirect("deployments.site_detail", site_id=site_id)


@bp.post("/sites/<int:site_id>/stop")
@login_required
def stop_site(site_id):
    validate_csrf()
    owned_site(site_id, manage=True)
    try:
        current_app.extensions["runtime_manager"].stop_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Could not stop site: {exc}", "error")
    else:
        flash("Site stopped.", "success")
    return action_redirect("deployments.site_detail", site_id=site_id)


@bp.post("/sites/<int:site_id>/restart")
@login_required
def restart_site(site_id):
    validate_csrf()
    site = owned_site(site_id, manage=True)
    runtime = current_app.extensions["runtime_manager"]
    if site["kind"] == "app":
        if runtime.start_app_async(site_id, force=True):
            flash("Restarting the app. This page updates when it's ready.", "success")
        else:
            flash("The app is already starting or updating.", "warning")
        return action_redirect("deployments.site_detail", site_id=site_id)
    try:
        runtime.restart_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Could not restart site: {exc}", "error")
    else:
        flash("Site restarted.", "success")
    return action_redirect("deployments.site_detail", site_id=site_id)


@bp.route("/sites/<int:site_id>/config", methods=("GET", "POST"))
@login_required
def edit_config(site_id):
    site = owned_site(site_id, manage=True)
    if site["kind"] == "app":
        flash("Apps use a managed proxy configuration that can't be edited.", "info")
        return redirect(url_for("deployments.site_detail", site_id=site_id))
    if request.method == "POST":
        validate_csrf()
        config = request.form.get("nginx_config", "")
        def rejected(message):
            flash(message, "error")
            return render_template(
                "config_editor.html",
                title=f"Edit {site['name']}",
                site=site,
                submitted_config=config,
            ), 422

        if len(config.encode("utf-8")) > 128 * 1024:
            return rejected("Configuration must be smaller than 128 KB.")
        else:
            try:
                hostnames = site_hostnames(get_db(), site)
                validate_site_config(
                    config,
                    site["document_root"],
                    site["port"],
                    hostnames,
                    current_app.config["SITE_GATEWAY_PORT"] if hostnames else None,
                )
            except NginxConfigError as exc:
                return rejected(str(exc))

            database = get_db()
            database.execute(
                "UPDATE sites SET nginx_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (config, site_id),
            )
            runtime = current_app.extensions["runtime_manager"]
            if not runtime.nginx_binary:
                database.commit()
                flash(
                    "Configuration saved. Nginx is not installed, so syntax validation is unavailable.",
                    "warning",
                )
                return redirect(url_for("deployments.edit_config", site_id=site_id))

            valid, message = runtime.validate_nginx(activating_site_id=site_id)
            if not valid:
                database.rollback()
                runtime.sync_nginx_configs()
                return rejected(f"Configuration was not saved: {message}")

            database.commit()
            if site["status"] == "running" and site["runtime_backend"] == "nginx":
                try:
                    runtime.restart_site(site_id)
                except RuntimeErrorDetail as exc:
                    flash(f"Saved, but Nginx reload failed: {exc}", "warning")
                else:
                    flash("Configuration saved, validated, and reloaded.", "success")
            else:
                runtime.sync_nginx_configs()
                flash(message, "success")
            return redirect(url_for("deployments.edit_config", site_id=site_id))
    return render_template("config_editor.html", title=f"Edit {site['name']}", site=site)


@bp.post("/sites/<int:site_id>/delete")
@login_required
def delete_site(site_id):
    validate_csrf()
    site = owned_site(site_id, manage=True)
    try:
        current_app.extensions["runtime_manager"].stop_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Site was not deleted because it could not be stopped: {exc}", "error")
        return action_redirect("deployments.site_detail", site_id=site_id)
    if site["kind"] == "app":
        try:
            current_app.extensions["runtime_manager"].remove_app(
                site, delete_data=request.form.get("delete_data") == "on"
            )
        except app_support.AppError as exc:
            flash(f"The app's container could not be fully removed: {exc}", "warning")
    database = get_db()
    database.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    database.commit()
    refresh_routing()
    flash(f"{site['name']} was deleted.", "success")
    return action_redirect("deployments.dashboard", view="apps" if site["kind"] == "app" else "sites")


@bp.post("/repositories/<int:repository_id>/delete")
@login_required
def delete_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    refresh_manager = current_app.extensions["repository_refresh_manager"]
    with refresh_manager.repository_lock(repository_id):
        database = get_db()
        sites = database.execute(
            "SELECT * FROM sites WHERE repository_id = ?",
            (repository_id,),
        ).fetchall()
        for site in sites:
            try:
                current_app.extensions["runtime_manager"].stop_site(site["id"])
            except RuntimeErrorDetail as exc:
                flash(
                    f"Repository was not deleted because one of its sites could not be stopped: {exc}",
                    "error",
                )
                return action_redirect("deployments.dashboard", view="sources")
        for site in sites:
            if site["kind"] == "app":
                try:
                    # Keep app data volumes; they can be removed by an admin.
                    current_app.extensions["runtime_manager"].remove_app(site, delete_data=False)
                except app_support.AppError:
                    pass
        database.execute(
            "DELETE FROM repositories WHERE id = ?",
            (repository_id,),
        )
        database.commit()
        refresh_routing()
        if repository["pending_path"]:
            pending = Path(repository["pending_path"])
            if repository_path_is_managed(pending):
                shutil.rmtree(pending, ignore_errors=True)
        if repository_path_is_managed(repository["local_path"]):
            shutil.rmtree(repository["local_path"], ignore_errors=True)
            flash(f"Repository {repository['name']} and its sites were deleted.", "success")
        else:
            flash(
                f"Repository record {repository['name']} was deleted, but its unsafe stored path was not removed.",
                "warning",
            )
    return action_redirect("deployments.dashboard", view="sources")
