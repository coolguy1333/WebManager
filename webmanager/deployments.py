import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

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
from .analytics import site_analytics
from .db import get_db
from .git_service import (
    GitError,
    clone_repository,
    display_repo_url,
    find_index_folders,
    repo_name_from_url,
    repository_commit,
    resolve_folder,
    validate_repo_url,
)
from .nginx import NginxConfigError, build_site_config, validate_site_config
from .repository_refresh import (
    MAX_REFRESH_MINUTES,
    MIN_REFRESH_MINUTES,
    next_refresh_time,
)
from .security import login_required, validate_csrf
from .services import RuntimeErrorDetail, allocate_port, port_is_available


bp = Blueprint("deployments", __name__)
SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def site_public_url(site) -> str:
    domain = current_app.config["SITE_BASE_DOMAIN"]
    if domain:
        scheme = current_app.config["SITE_PUBLIC_SCHEME"]
        return f"{scheme}://{site['slug']}.{domain}"
    return f"http://{request_hostname()}:{site['port']}"


def site_routing_details(site) -> dict[str, str] | None:
    domain = current_app.config["SITE_BASE_DOMAIN"]
    if not domain:
        return None
    hostname = f"{site['slug']}.{domain}"
    return {
        "hostname": hostname,
        "wildcard": f"*.{domain}",
        "origin": "http://localhost:8080",
        "local_test": (
            f"curl -I -H 'Host: {hostname}' http://127.0.0.1:8080/"
        ),
    }


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
               repositories.update_mode AS repository_update_mode
        FROM sites
        JOIN repositories ON repositories.id = sites.repository_id
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
    show_all = has_permission(RESOURCE_VIEW_ALL) or has_permission(RESOURCE_MANAGE_ALL)
    sites = database.execute(
        """
        SELECT sites.*, repositories.name AS repository_name,
               users.display_name AS owner_name, users.email AS owner_email,
               pools.name AS pool_name
        FROM sites JOIN repositories ON repositories.id = sites.repository_id
        JOIN users ON users.id = sites.user_id
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
    manageable_site_ids = {
        site["id"]
        for site in sites
        if site["user_id"] == g.user["id"]
        or has_permission(RESOURCE_MANAGE_ALL)
        or access_levels.get(site["id"], 0) >= 2
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
        "attention": sum(site["status"] == "error" for site in sites),
        "repositories": len(repositories),
    }
    return render_template(
        "dashboard.html",
        title="Dashboard",
        sites=sites,
        repositories=repositories,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        summary=summary,
        host=request_hostname(),
        site_public_url=site_public_url,
        site_base_domain=current_app.config["SITE_BASE_DOMAIN"],
        refresh_min=MIN_REFRESH_MINUTES,
        refresh_max=MAX_REFRESH_MINUTES,
        show_all=show_all,
        manage_all=has_permission(RESOURCE_MANAGE_ALL),
        manageable_site_ids=manageable_site_ids,
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
    except GitError as exc:
        flash(str(exc), "error")
        return redirect(url_for("deployments.dashboard"))

    name = repo_name_from_url(url)
    cursor = database.execute(
        """
        INSERT INTO repositories (user_id, name, url, branch, local_path, status)
        VALUES (?, ?, ?, ?, '', 'cloning')
        """,
        (g.user["id"], name, display_repo_url(url), branch),
    )
    repository_id = cursor.lastrowid
    target = Path(current_app.config["REPOSITORY_ROOT"]) / str(g.user["id"]) / str(repository_id)
    database.execute(
        "UPDATE repositories SET local_path = ? WHERE id = ?",
        (str(target.resolve()), repository_id),
    )
    database.commit()

    try:
        clone_repository(url, target, branch)
        current_commit = repository_commit(target)
        candidates = find_index_folders(target)
        database.execute(
            """
            UPDATE repositories
            SET status = 'ready', error = NULL,
                current_commit = ?,
                last_refreshed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (current_commit, repository_id),
        )
        database.commit()
    except GitError as exc:
        database.execute(
            "UPDATE repositories SET status = 'error', error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(exc), repository_id),
        )
        database.commit()
        flash(f"Could not clone repository: {exc}", "error")
        return redirect(url_for("deployments.dashboard"))

    if not candidates:
        flash("Repository cloned, but no index.html or index.htm file was found.", "warning")
    return redirect(url_for("deployments.select_folder", repository_id=repository_id))


@bp.get("/repositories/<int:repository_id>/select")
@login_required
def select_folder(repository_id):
    repository = owned_repository(repository_id, manage=True)
    candidates = find_index_folders(Path(repository["local_path"]))
    suggested_name = repository["name"].replace("_", " ").replace("-", " ").title()
    for candidate in candidates:
        candidate["suggested_name"] = (
            suggested_name
            if candidate["folder"] == "."
            else Path(candidate["folder"]).name.replace("_", " ").replace("-", " ").title()
        )
    return render_template(
        "select_folder.html",
        title="Select site folder",
        repository=repository,
        candidates=candidates,
        suggested_name=suggested_name,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        site_base_domain=current_app.config["SITE_BASE_DOMAIN"],
        site_public_scheme=current_app.config["SITE_PUBLIC_SCHEME"],
    )


@bp.post("/repositories/<int:repository_id>/refresh")
@login_required
def refresh_repository(repository_id):
    validate_csrf()
    owned_repository(repository_id, manage=True)
    result = current_app.extensions["repository_refresh_manager"].refresh(repository_id)
    if result.status in {"applied", "current"}:
        flash(result.message, "success")
    elif result.status in {"available", "busy"}:
        flash(result.message, "warning")
    else:
        flash(f"Update check failed: {result.message}", "error")
    return redirect(url_for("deployments.dashboard"))


@bp.post("/repositories/<int:repository_id>/schedule")
@login_required
def schedule_repository_refresh(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    raw_minutes = request.form.get("auto_refresh_minutes", "").strip()
    update_mode = request.form.get("update_mode", "approval")
    if update_mode not in {"approval", "auto"}:
        abort(400, "Unknown update mode.")

    if not raw_minutes:
        minutes = None
        next_run = None
    else:
        try:
            minutes = int(raw_minutes)
        except ValueError:
            flash("Automatic refresh must be a whole number of minutes.", "error")
            return redirect(url_for("deployments.dashboard"))
        if minutes < MIN_REFRESH_MINUTES or minutes > MAX_REFRESH_MINUTES:
            flash(
                f"Automatic refresh must be between {MIN_REFRESH_MINUTES} "
                f"and {MAX_REFRESH_MINUTES} minutes.",
                "error",
            )
            return redirect(url_for("deployments.dashboard"))
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
    if minutes:
        action = "apply automatically" if update_mode == "auto" else "wait for owner approval"
        flash(
            f"Update checks set to every {minutes} minutes and will {action}.",
            "success",
        )
    else:
        flash("Scheduled update checks disabled. Manual checks remain available.", "success")
    if update_mode == "auto":
        result = current_app.extensions[
            "repository_refresh_manager"
        ].apply_pending(repository_id)
        if result.status == "applied":
            flash(result.message, "success")
        elif repository["pending_commit"] and result.status != "missing":
            flash(f"Could not apply pending update: {result.message}", "error")
    return redirect(url_for("deployments.dashboard"))


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
    return redirect(url_for("deployments.dashboard"))


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
    return redirect(url_for("deployments.dashboard"))


@bp.post("/repositories/<int:repository_id>/deploy")
@login_required
def deploy_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    owner_id = repository["user_id"]
    database = get_db()
    deployments = []
    selected_indexes = list(
        dict.fromkeys(
            value for value in request.form.getlist("selected") if value.isdigit()
        )
    )
    if request.form.get("multi_deploy") == "1" and not selected_indexes:
        flash("Select at least one site folder to deploy.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))
    if selected_indexes:
        for index in selected_indexes:
            deployments.append(
                {
                    "name": request.form.get(f"site_name_{index}", "").strip(),
                    "folder": request.form.get(f"folder_{index}", ""),
                    "spa_fallback": request.form.get(f"spa_fallback_{index}") == "on",
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
        not item["name"] or len(item["name"]) > 80 for item in deployments
    ):
        flash("Every selected site needs a name of 80 characters or fewer.", "error")
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
            slug = unique_slug(database, item["name"])
            hostname = (
                f"{slug}.{current_app.config['SITE_BASE_DOMAIN']}"
                if current_app.config["SITE_BASE_DOMAIN"]
                else None
            )
            config = build_site_config(
                item["name"],
                selected,
                index_file,
                port,
                item["spa_fallback"],
                hostname,
                current_app.config["SITE_GATEWAY_PORT"] if hostname else None,
            )
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, name, slug, folder, document_root,
                    index_file, port, spa_fallback, nginx_config, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped')
                """,
                (
                    owner_id,
                    repository_id,
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
            database.commit()
        except (GitError, RuntimeErrorDetail, ValueError, sqlite3.IntegrityError) as exc:
            database.rollback()
            flash(f"Could not deploy {item['name']}: {exc}", "error")
            continue

        site_id = cursor.lastrowid
        created.append({"id": site_id, "slug": slug, "port": port, "name": item["name"]})
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
    return redirect(url_for("deployments.dashboard"))


@bp.get("/sites/<int:site_id>")
@login_required
def site_detail(site_id):
    site = owned_site(site_id)
    database = get_db()
    hostname = (
        f"{site['slug']}.{current_app.config['SITE_BASE_DOMAIN']}"
        if current_app.config["SITE_BASE_DOMAIN"]
        else request_hostname()
    )
    return render_template(
        "site_detail.html",
        title=site["name"],
        site=site,
        host=request_hostname(),
        public_url=site_public_url(site),
        nginx_available=bool(current_app.extensions["runtime_manager"].nginx_binary),
        can_manage=can_manage_site(site, database),
        can_manage_repository=can_manage_resource(site["user_id"]),
        routing=site_routing_details(site),
        analytics=site_analytics(
            Path(current_app.config["NGINX_ROOT"]) / "access.log",
            hostname,
        ),
    )


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
    if request.method == "POST":
        validate_csrf()
        name = request.form.get("name", "").strip()
        slug = slugify(request.form.get("slug", "").strip() or name)
        folder = request.form.get("folder", "")
        spa_fallback = request.form.get("spa_fallback") == "on"
        try:
            port = int(request.form.get("port", ""))
        except ValueError:
            port = -1

        if not name or len(name) > 80:
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
        else:
            try:
                selected = resolve_folder(Path(repository["local_path"]), folder)
                index_files = {
                    path.name.lower(): path.name
                    for path in selected.iterdir()
                    if path.is_file()
                }
                index_file = index_files.get("index.html") or index_files.get("index.htm")
                if not index_file:
                    raise GitError("The selected folder does not contain an index page.")
                slug = unique_slug(database, slug, exclude_site_id=site_id)
                hostname = (
                    f"{slug}.{current_app.config['SITE_BASE_DOMAIN']}"
                    if current_app.config["SITE_BASE_DOMAIN"]
                    else None
                )
                config = build_site_config(
                    name,
                    selected,
                    index_file,
                    port,
                    spa_fallback,
                    hostname,
                    current_app.config["SITE_GATEWAY_PORT"] if hostname else None,
                )
                database.execute(
                    """
                    UPDATE sites
                    SET name = ?, slug = ?, folder = ?, document_root = ?,
                        index_file = ?, port = ?, spa_fallback = ?,
                        nginx_config = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
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
                database.commit()
            except (GitError, sqlite3.IntegrityError) as exc:
                database.rollback()
                flash(f"Could not save site settings: {exc}", "error")
            else:
                runtime = current_app.extensions["runtime_manager"]
                if site["status"] == "running":
                    try:
                        runtime.restart_site(site_id)
                    except RuntimeErrorDetail as exc:
                        flash(f"Settings saved, but restart failed: {exc}", "warning")
                    else:
                        flash("Site settings saved and the site restarted.", "success")
                else:
                    try:
                        runtime.sync_nginx_configs()
                    except RuntimeErrorDetail as exc:
                        flash(f"Settings saved, but Nginx sync failed: {exc}", "warning")
                    else:
                        flash("Site settings saved.", "success")
                return redirect(url_for("deployments.site_detail", site_id=site_id))
        site = owned_site(site_id, manage=True)

    return render_template(
        "site_settings.html",
        title=f"Settings for {site['name']}",
        site=site,
        candidates=candidates,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
        site_base_domain=current_app.config["SITE_BASE_DOMAIN"],
    )


@bp.post("/sites/<int:site_id>/start")
@login_required
def start_site(site_id):
    validate_csrf()
    owned_site(site_id, manage=True)
    try:
        backend = current_app.extensions["runtime_manager"].start_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Could not start site: {exc}", "error")
    else:
        flash(f"Site started with {backend}.", "success")
    return redirect(url_for("deployments.site_detail", site_id=site_id))


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
    return redirect(url_for("deployments.site_detail", site_id=site_id))


@bp.post("/sites/<int:site_id>/restart")
@login_required
def restart_site(site_id):
    validate_csrf()
    owned_site(site_id, manage=True)
    try:
        current_app.extensions["runtime_manager"].restart_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Could not restart site: {exc}", "error")
    else:
        flash("Site restarted.", "success")
    return redirect(url_for("deployments.site_detail", site_id=site_id))


@bp.route("/sites/<int:site_id>/config", methods=("GET", "POST"))
@login_required
def edit_config(site_id):
    site = owned_site(site_id, manage=True)
    if request.method == "POST":
        validate_csrf()
        config = request.form.get("nginx_config", "")
        if len(config.encode("utf-8")) > 128 * 1024:
            flash("Configuration must be smaller than 128 KB.", "error")
        else:
            try:
                hostname = (
                    f"{site['slug']}.{current_app.config['SITE_BASE_DOMAIN']}"
                    if current_app.config["SITE_BASE_DOMAIN"]
                    else None
                )
                validate_site_config(
                    config,
                    site["document_root"],
                    site["port"],
                    hostname,
                    current_app.config["SITE_GATEWAY_PORT"] if hostname else None,
                )
            except NginxConfigError as exc:
                flash(str(exc), "error")
                site = owned_site(site_id, manage=True)
                return render_template(
                    "config_editor.html",
                    title=f"Edit {site['name']}",
                    site=site,
                    submitted_config=config,
                )

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
                flash(f"Configuration was not saved: {message}", "error")
                return redirect(url_for("deployments.edit_config", site_id=site_id))

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
        site = owned_site(site_id, manage=True)
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
        return redirect(url_for("deployments.site_detail", site_id=site_id))
    database = get_db()
    database.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    database.commit()
    flash(f"{site['name']} was deleted.", "success")
    return redirect(url_for("deployments.dashboard"))


@bp.post("/repositories/<int:repository_id>/delete")
@login_required
def delete_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    refresh_manager = current_app.extensions["repository_refresh_manager"]
    with refresh_manager.repository_lock(repository_id):
        database = get_db()
        sites = database.execute(
            "SELECT id FROM sites WHERE repository_id = ?",
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
                return redirect(url_for("deployments.dashboard"))
        database.execute(
            "DELETE FROM repositories WHERE id = ?",
            (repository_id,),
        )
        database.commit()
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
    return redirect(url_for("deployments.dashboard"))
