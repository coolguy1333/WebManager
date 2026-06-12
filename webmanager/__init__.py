import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import admin, auth, db, deployments
from .repository_refresh import RepositoryRefreshManager
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

    if not app.config["SECRET_KEY"]:
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

    with app.app_context():
        db.init_db()

    runtime = RuntimeManager(app)
    app.extensions["runtime_manager"] = runtime
    runtime.migrate_site_configs()
    refresh_manager = RepositoryRefreshManager(app)
    app.extensions["repository_refresh_manager"] = refresh_manager
    if not app.config.get("TESTING"):
        runtime.restore_sites()
        runtime.restore_gateway()
        if app.config["AUTO_REFRESH_ENABLED"]:
            refresh_manager.start()

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
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https: data:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        return response

    return app
