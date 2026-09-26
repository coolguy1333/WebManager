import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import admin, auth, data_replication, db, deployments, domains, mesh, replication
from .data_replication import DataReplicationManager
from .repository_refresh import RepositoryRefreshManager
from .replication import ReplicationError, ReplicationManager
from .services import RuntimeManager


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _env_flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def _load_or_create_secret(instance_path: Path) -> str:
    secret_path = instance_path / "secret.key"
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()

    secret = secrets.token_hex(32)
    secret_path.write_text(secret, encoding="utf-8")
    if os.name != "nt":
        secret_path.chmod(0o600)
    return secret


def create_app(test_config=None):
    default_instance_path = Path(__file__).resolve().parent.parent / "instance"
    configured_instance_path = (
        (test_config or {}).get("INSTANCE_PATH")
        or os.environ.get("WEBMANAGER_DATA_DIR")
        or default_instance_path
    )
    instance_path = Path(configured_instance_path).expanduser().resolve()
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=str(instance_path),
    )
    instance_path = Path(app.instance_path)
    instance_path.mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=None,
        DATABASE=str(instance_path / "webmanager.sqlite3"),
        REPOSITORY_ROOT=str(instance_path / "repositories"),
        NGINX_ROOT=str(instance_path / "nginx"),
        LOG_ROOT=str(instance_path / "logs"),
        HOST=os.environ.get("WEBMANAGER_HOST", "127.0.0.1"),
        PORT=int(os.environ.get("WEBMANAGER_PORT", "5000")),
        DEBUG=_env_flag("WEBMANAGER_DEBUG"),
        SITE_PORT_MIN=int(os.environ.get("WEBMANAGER_SITE_PORT_MIN", "8100")),
        SITE_PORT_MAX=int(os.environ.get("WEBMANAGER_SITE_PORT_MAX", "8999")),
        SITE_GATEWAY_PORT=int(os.environ.get("WEBMANAGER_SITE_GATEWAY_PORT", "8090")),
        SITE_BASE_DOMAIN=os.environ.get("WEBMANAGER_SITE_BASE_DOMAIN", "").strip().lower(),
        SITE_PUBLIC_SCHEME=os.environ.get("WEBMANAGER_SITE_PUBLIC_SCHEME", "http").strip().lower(),
        NGINX_BINARY=os.environ.get("WEBMANAGER_NGINX_BINARY", "nginx"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("WEBMANAGER_SESSION_COOKIE_SECURE"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TRUST_PROXY=_env_flag("WEBMANAGER_TRUST_PROXY"),
        GOOGLE_CLIENT_ID=os.environ.get("WEBMANAGER_GOOGLE_CLIENT_ID", "").strip(),
        GOOGLE_CLIENT_SECRET=os.environ.get("WEBMANAGER_GOOGLE_CLIENT_SECRET", "").strip(),
        GOOGLE_REDIRECT_URI=os.environ.get("WEBMANAGER_GOOGLE_REDIRECT_URI", "").strip(),
        GOOGLE_ALLOWED_DOMAINS=os.environ.get("WEBMANAGER_GOOGLE_ALLOWED_DOMAINS", "").strip(),
        GOOGLE_ALLOWED_EMAILS=os.environ.get("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "").strip(),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        APPS_ENABLED=_env_flag("WEBMANAGER_APPS_ENABLED"),
        CONTAINER_RUNTIME=os.environ.get("WEBMANAGER_CONTAINER_RUNTIME", "").strip(),
        APP_DEFAULT_MEMORY_MB=int(os.environ.get("WEBMANAGER_APP_DEFAULT_MEMORY_MB", "256")),
        APP_CPUS=float(os.environ.get("WEBMANAGER_APP_CPUS", "0.5")),
        APP_START_TIMEOUT=int(os.environ.get("WEBMANAGER_APP_START_TIMEOUT", "60")),
        MAX_REPOSITORY_BYTES=int(os.environ.get("WEBMANAGER_MAX_REPOSITORY_MB", "1024")) * 1024 * 1024,
        AUTO_REFRESH_ENABLED=_env_flag("WEBMANAGER_AUTO_REFRESH_ENABLED", "1"),
        AUTO_REFRESH_POLL_SECONDS=int(
            os.environ.get("WEBMANAGER_AUTO_REFRESH_POLL_SECONDS", "30")
        ),
        PROGRAM_UPDATE_STATUS_FILE=os.environ.get(
            "WEBMANAGER_PROGRAM_UPDATE_STATUS_FILE",
            "/var/lib/webmanager-updater/status.json",
        ),
        PROGRAM_UPDATE_REQUEST_FILE=os.environ.get(
            "WEBMANAGER_PROGRAM_UPDATE_REQUEST_FILE",
            "/var/lib/webmanager-updater/requests/install.commit",
        ),
        PROGRAM_UPDATE_CHECK_REQUEST_FILE=os.environ.get(
            "WEBMANAGER_PROGRAM_UPDATE_CHECK_REQUEST_FILE",
            "/var/lib/webmanager-updater/requests/check",
        ),
        MESH_PEERS=os.environ.get("WEBMANAGER_PEERS", "").strip(),
        MESH_TOKEN=os.environ.get("WEBMANAGER_PEER_TOKEN", "").strip(),
        REPLICA_OF=os.environ.get("WEBMANAGER_REPLICA_OF", "").strip().rstrip("/"),
    )

    if test_config:
        app.config.update(test_config)

    site_domain = str(app.config["SITE_BASE_DOMAIN"]).strip().lower().rstrip(".")
    if site_domain and not DOMAIN_RE.fullmatch(site_domain):
        raise RuntimeError("WEBMANAGER_SITE_BASE_DOMAIN must be a valid DNS name.")
    app.config["SITE_BASE_DOMAIN"] = site_domain
    if app.config["SITE_PUBLIC_SCHEME"] not in {"http", "https"}:
        raise RuntimeError("WEBMANAGER_SITE_PUBLIC_SCHEME must be http or https.")
    if app.config["SITE_GATEWAY_PORT"] in range(
        app.config["SITE_PORT_MIN"],
        app.config["SITE_PORT_MAX"] + 1,
    ):
        raise RuntimeError(
            "WEBMANAGER_SITE_GATEWAY_PORT must be outside the site port range."
        )

    if app.config["REPLICA_OF"] and not app.config["MESH_TOKEN"]:
        raise RuntimeError(
            "WEBMANAGER_REPLICA_OF requires WEBMANAGER_PEER_TOKEN to be set "
            "(the same shared secret configured on the primary)."
        )

    if not app.config["SECRET_KEY"]:
        if app.config["REPLICA_OF"]:
            secret_path = instance_path / "secret.key"
            try:
                app.config["SECRET_KEY"] = replication.fetch_secret_key(
                    app.config["REPLICA_OF"], app.config["MESH_TOKEN"]
                )
            except ReplicationError as exc:
                if secret_path.exists():
                    # Already paired once before; keep working with the last
                    # known key until the primary is reachable again.
                    app.config["SECRET_KEY"] = secret_path.read_text(encoding="utf-8").strip()
                else:
                    raise RuntimeError(
                        "This is a new replica (WEBMANAGER_REPLICA_OF is set) and its "
                        f"primary could not be reached to fetch the shared secret key: {exc} "
                        "The primary must be reachable the first time a replica starts."
                    ) from exc
            else:
                secret_path.write_text(app.config["SECRET_KEY"], encoding="utf-8")
                if os.name != "nt":
                    secret_path.chmod(0o600)
        else:
            app.config["SECRET_KEY"] = _load_or_create_secret(instance_path)

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    for key in ("REPOSITORY_ROOT", "NGINX_ROOT", "LOG_ROOT"):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    auth.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(deployments.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(mesh.bp)
    app.register_blueprint(replication.bp)
    app.register_blueprint(data_replication.bp)
    replication_manager = ReplicationManager(app, app.config["REPLICA_OF"], app.config["MESH_TOKEN"])
    app.extensions["replication_manager"] = replication_manager
    data_replication_manager = DataReplicationManager(app, app.config["REPLICA_OF"], app.config["MESH_TOKEN"])
    app.extensions["data_replication_manager"] = data_replication_manager
    replication.register_write_forwarding(app)

    @app.template_filter("ago")
    def relative_time(value):
        """Render stored UTC timestamps ("YYYY-MM-DD HH:MM:SS") as relative text."""
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value).removesuffix(" UTC").replace("Z", ""))
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
        future = seconds < 0
        seconds = abs(seconds)
        for limit, size, unit in (
            (60, 1, "second"),
            (3600, 60, "minute"),
            (86400, 3600, "hour"),
            (86400 * 30, 86400, "day"),
            (86400 * 365, 86400 * 30, "month"),
        ):
            if seconds < limit:
                amount = max(1, seconds // size) if unit != "second" else seconds
                break
        else:
            amount, unit = seconds // (86400 * 365), "year"
        if unit == "second" and amount < 10:
            return "just now"
        label = f"{amount} {unit}{'' if amount == 1 else 's'}"
        return f"in {label}" if future else f"{label} ago"

    @app.context_processor
    def navigation_badges():
        from flask import g

        from .update_status import read_update_status

        user = getattr(g, "user", None)
        if user is None:
            return {"nav_badges": {}}
        try:
            database = db.get_db()
            if user["is_admin"]:
                pending = database.execute(
                    "SELECT COUNT(*) FROM repositories WHERE pending_commit IS NOT NULL"
                ).fetchone()[0]
            else:
                pending = database.execute(
                    "SELECT COUNT(*) FROM repositories WHERE pending_commit IS NOT NULL AND user_id = ?",
                    (user["id"],),
                ).fetchone()[0]
            system = bool(user["is_admin"]) and read_update_status().get("state") == "available"
        except Exception:  # never break page rendering over a badge
            return {"nav_badges": {}}
        return {"nav_badges": {"sources": pending, "system": system}}

    @app.context_processor
    def versioned_static_assets():
        def static_asset(filename):
            asset_path = Path(app.static_folder) / filename
            try:
                details = asset_path.stat()
                version = f"{details.st_mtime_ns:x}-{details.st_size:x}"
            except OSError:
                version = "missing"
            return url_for("static", filename=filename, v=version)

        return {"static_asset": static_asset}

    @app.context_processor
    def replication_role():
        manager = app.extensions.get("replication_manager")
        return {"is_replica": bool(manager and manager.is_replica)}

    with app.app_context():
        db.init_db()

    runtime = RuntimeManager(app)
    app.extensions["runtime_manager"] = runtime
    runtime.migrate_site_configs()
    refresh_manager = RepositoryRefreshManager(app)
    app.extensions["repository_refresh_manager"] = refresh_manager

    peer_urls = [url.strip().rstrip("/") for url in app.config["MESH_PEERS"].split(",") if url.strip()]
    invalid_peer_urls = [url for url in peer_urls if not url.startswith(("http://", "https://"))]
    if invalid_peer_urls:
        peer_urls = [url for url in peer_urls if url not in invalid_peer_urls]
        app.logger.warning(
            "Ignoring invalid entries in WEBMANAGER_PEERS (must start with http:// "
            "or https://): %s",
            ", ".join(invalid_peer_urls),
        )
    if peer_urls:
        try:
            with app.app_context():
                self_host = domains.dashboard_hostname()
        except Exception:  # pragma: no cover - DB not ready yet is not fatal here
            self_host = ""
        if self_host:
            from urllib.parse import urlsplit

            peer_urls = [url for url in peer_urls if urlsplit(url).hostname != self_host]
    mesh_hub = mesh.MeshHub(app, peer_urls, app.config["MESH_TOKEN"])
    app.extensions["mesh_hub"] = mesh_hub

    if not app.config.get("TESTING"):
        if replication_manager.is_replica:
            # A replica mirrors config and data on their own schedules; it
            # doesn't start apps or run scheduled Git checks itself (see
            # webmanager/replication.py and data_replication.py).
            app.logger.info(
                "This server is a replica of %s. It will forward writes "
                "there and mirror its database and site/app data.",
                replication_manager.primary_url,
            )
            replication_manager.start()
            data_replication_manager.start()
            runtime.restore_gateway()
        else:
            runtime.restore_sites()
            runtime.restore_gateway()
            if app.config["AUTO_REFRESH_ENABLED"]:
                refresh_manager.start()
        if peer_urls:
            if not app.config["MESH_TOKEN"]:
                app.logger.warning(
                    "WEBMANAGER_PEERS is set without WEBMANAGER_PEER_TOKEN - "
                    "/mesh/status is public and unauthenticated. Set "
                    "WEBMANAGER_PEER_TOKEN to restrict it to your own servers."
                )
            mesh_hub.start()

    @app.errorhandler(404)
    def not_found(_error):
        return render_template(
            "error.html",
            title="Page not found",
            message="The page may have moved, or you may not have access to it.",
            error_code=404,
        ), 404

    @app.errorhandler(400)
    def bad_request(error):
        return render_template(
            "error.html",
            title="Invalid request",
            message=getattr(error, "description", "The request could not be processed."),
            error_code=400,
        ), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html",
            title="Access denied",
            message="You do not have permission to open that page.",
            error_code=403,
        ), 403

    @app.errorhandler(401)
    def unauthorized(_error):
        return render_template(
            "error.html",
            title="Please sign in",
            message="Your session has ended or you are not signed in.",
            error_code=401,
        ), 401

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return render_template(
            "error.html",
            title="That action isn't available here",
            message="The page was opened in a way it doesn't support. Go back and try again from the dashboard.",
            error_code=405,
        ), 405

    @app.errorhandler(500)
    def server_error(_error):
        return render_template(
            "error.html",
            title="Something went wrong",
            message="WebManager hit an unexpected error. It has been logged; try again in a moment.",
            error_code=500,
        ), 500

    @app.errorhandler(413)
    def too_large(_error):
        return render_template(
            "error.html",
            title="Request too large",
            message="The submitted request was larger than the allowed limit.",
            error_code=413,
        ), 413

    @app.get("/healthz")
    def health():
        db.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.after_request
    def security_headers(response):
        if request.method == "POST" and response.status_code in {301, 302}:
            response.status_code = 303
        if request.endpoint == "static":
            if request.args.get("v"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        if request.endpoint != "static" and "Cache-Control" not in response.headers:
            # Pages contain CSRF tokens and account data; keep them out of
            # shared and back/forward caches.
            response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https: data:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        return response

    return app
