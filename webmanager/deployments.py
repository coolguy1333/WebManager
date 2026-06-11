import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .access_control import (
    RESOURCE_MANAGE_ALL,
    RESOURCE_VIEW_ALL,
    can_manage_resource,
    can_view_resource,
    has_permission,
)
from .db import get_db
from .git_service import (
    GitError,
    clone_repository,
    display_repo_url,
    find_index_folders,
    repo_name_from_url,
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
from .services import RuntimeErrorDetail, allocate_port


bp = Blueprint("deployments", __name__)
SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return SLUG_RE.sub("-", value.lower()).strip("-")[:48] or "site"


def request_hostname() -> str:
    hostname = urlsplit(request.host_url).hostname or "localhost"
    return f"[{hostname}]" if ":" in hostname else hostname


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
    site = get_db().execute(
        """
        SELECT sites.*, repositories.name AS repository_name, repositories.url AS repository_url
        FROM sites
        JOIN repositories ON repositories.id = sites.repository_id
        WHERE sites.id = ?
        """,
        (site_id,),
    ).fetchone()
    allowed = (
        can_manage_resource(site["user_id"])
        if site is not None and manage
        else site is not None and can_view_resource(site["user_id"])
    )
    if not allowed:
        abort(404)
    return site


@bp.get("/")
@login_required
def dashboard():
    database = get_db()
    show_all = has_permission(RESOURCE_VIEW_ALL) or has_permission(RESOURCE_MANAGE_ALL)
    site_where = "" if show_all else "WHERE sites.user_id = ?"
    parameters = () if show_all else (g.user["id"],)
    sites = database.execute(
        f"""
        SELECT sites.*, repositories.name AS repository_name,
               users.display_name AS owner_name, users.email AS owner_email
        FROM sites JOIN repositories ON repositories.id = sites.repository_id
        JOIN users ON users.id = sites.user_id
        {site_where}
        ORDER BY sites.created_at DESC
        """,
        parameters,
    ).fetchall()
    repository_where = "" if show_all else "WHERE repositories.user_id = ?"
    repositories = database.execute(
        f"""
        SELECT repositories.*, COUNT(sites.id) AS site_count,
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
        refresh_min=MIN_REFRESH_MINUTES,
        refresh_max=MAX_REFRESH_MINUTES,
        show_all=show_all,
        manage_all=has_permission(RESOURCE_MANAGE_ALL),
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
        candidates = find_index_folders(target)
        database.execute(
            """
            UPDATE repositories
            SET status = 'ready', error = NULL,
                last_refreshed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (repository_id,),
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
    return render_template(
        "select_folder.html",
        title="Select site folder",
        repository=repository,
        candidates=candidates,
        suggested_name=suggested_name,
        port_min=current_app.config["SITE_PORT_MIN"],
        port_max=current_app.config["SITE_PORT_MAX"],
    )


@bp.post("/repositories/<int:repository_id>/refresh")
@login_required
def refresh_repository(repository_id):
    validate_csrf()
    owned_repository(repository_id, manage=True)
    result = current_app.extensions["repository_refresh_manager"].refresh(repository_id)
    if result.status == "success":
        flash(result.message, "success")
    elif result.status == "busy":
        flash(result.message, "warning")
    else:
        flash(f"Refresh failed: {result.message}", "error")
    return redirect(url_for("deployments.select_folder", repository_id=repository_id))


@bp.post("/repositories/<int:repository_id>/schedule")
@login_required
def schedule_repository_refresh(repository_id):
    validate_csrf()
    owned_repository(repository_id, manage=True)
    raw_minutes = request.form.get("auto_refresh_minutes", "").strip()

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
        SET auto_refresh_minutes = ?, next_refresh_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (minutes, next_run, repository_id),
    )
    database.commit()
    if minutes:
        flash(f"Automatic refresh set to every {minutes} minutes.", "success")
    else:
        flash("Automatic repository refresh disabled.", "success")
    return redirect(url_for("deployments.dashboard"))


@bp.post("/repositories/<int:repository_id>/deploy")
@login_required
def deploy_repository(repository_id):
    validate_csrf()
    repository = owned_repository(repository_id, manage=True)
    owner_id = repository["user_id"]
    database = get_db()
    name = request.form.get("site_name", "").strip()
    folder = request.form.get("folder", "")
    spa_fallback = request.form.get("spa_fallback") == "on"
    raw_port = request.form.get("port", "").strip()

    if not name or len(name) > 80:
        flash("Site name is required and must be 80 characters or fewer.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))

    try:
        selected = resolve_folder(Path(repository["local_path"]), folder)
        index_files = {path.name.lower(): path.name for path in selected.iterdir() if path.is_file()}
        index_file = index_files.get("index.html") or index_files.get("index.htm")
        if not index_file:
            raise GitError("The selected folder does not contain an index page.")
        requested_port = int(raw_port) if raw_port else None
        port = allocate_port(
            database,
            current_app.config["SITE_PORT_MIN"],
            current_app.config["SITE_PORT_MAX"],
            requested_port,
        )
    except (GitError, RuntimeErrorDetail, ValueError) as exc:
        message = str(exc) if str(exc) else "Port must be a number."
        flash(message, "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))

    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while database.execute(
        "SELECT 1 FROM sites WHERE user_id = ? AND slug = ?",
        (owner_id, slug),
    ).fetchone():
        slug = f"{base_slug[:43]}-{suffix}"
        suffix += 1

    config = build_site_config(name, selected, index_file, port, spa_fallback)
    try:
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
                name,
                slug,
                folder,
                str(selected),
                index_file,
                port,
                int(spa_fallback),
                config,
            ),
        )
        database.commit()
    except sqlite3.IntegrityError:
        flash("That port or site name was allocated by another request. Please try again.", "error")
        return redirect(url_for("deployments.select_folder", repository_id=repository_id))

    site_id = cursor.lastrowid
    try:
        backend = current_app.extensions["runtime_manager"].start_site(site_id)
    except RuntimeErrorDetail as exc:
        flash(f"Site created, but hosting failed: {exc}", "warning")
    else:
        label = "Nginx" if backend == "nginx" else "the built-in static server"
        flash(f"Site deployed on port {port} using {label}.", "success")
    return redirect(url_for("deployments.site_detail", site_id=site_id))


@bp.get("/sites/<int:site_id>")
@login_required
def site_detail(site_id):
    site = owned_site(site_id)
    return render_template(
        "site_detail.html",
        title=site["name"],
        site=site,
        host=request_hostname(),
        nginx_available=bool(current_app.extensions["runtime_manager"].nginx_binary),
        can_manage=can_manage_resource(site["user_id"]),
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
                validate_site_config(config, site["document_root"], site["port"])
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
        if repository_path_is_managed(repository["local_path"]):
            shutil.rmtree(repository["local_path"], ignore_errors=True)
            flash(f"Repository {repository['name']} and its sites were deleted.", "success")
        else:
            flash(
                f"Repository record {repository['name']} was deleted, but its unsafe stored path was not removed.",
                "warning",
            )
    return redirect(url_for("deployments.dashboard"))
