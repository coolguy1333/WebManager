import re
import secrets
import sqlite3
from urllib.parse import urlsplit, urlunsplit

import click
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from .access_control import has_permission
from .db import get_db
from .domains import dashboard_hostnames
from .security import csrf_token, validate_csrf


bp = Blueprint("auth", __name__, url_prefix="/auth")
oauth = OAuth()
GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
USERNAME_CLEANER = re.compile(r"[^a-z0-9_.-]+")


def init_app(app):
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    app.cli.add_command(link_google_user_command)


def google_is_configured():
    return bool(
        current_app.config["GOOGLE_CLIENT_ID"]
        and current_app.config["GOOGLE_CLIENT_SECRET"]
    )


def _configured_values(name):
    return {
        value.strip().lower()
        for value in current_app.config.get(name, "").split(",")
        if value.strip()
    }


def _safe_next(value):
    if not value:
        return None
    if "\\" in value or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _unique_username(database, email):
    base = USERNAME_CLEANER.sub("-", email.split("@", 1)[0].lower()).strip("-._")
    if len(base) < 3:
        base = f"{base or 'user'}-google"
    base = base[:32]
    username = base
    suffix = 2
    while database.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone():
        suffix_text = f"-{suffix}"
        username = f"{base[:32 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return username


def _identity_is_allowed(claims):
    email = claims["email"].lower()
    hosted_domain = str(claims.get("hd", "")).lower()
    allowed_emails = _configured_values("GOOGLE_ALLOWED_EMAILS")
    allowed_domains = _configured_values("GOOGLE_ALLOWED_DOMAINS")

    if not allowed_emails and not allowed_domains:
        return True
    return email in allowed_emails or hosted_domain in allowed_domains


def _find_or_create_user(claims):
    database = get_db()
    subject = claims["sub"]
    email = claims["email"].lower()
    display_name = (claims.get("name") or email).strip()
    picture_url = (claims.get("picture") or "").strip() or None

    user = database.execute(
        "SELECT * FROM users WHERE google_sub = ?",
        (subject,),
    ).fetchone()
    if user is not None:
        if not user["is_active"]:
            raise ValueError("This WebManager account has been disabled.")
        conflict = database.execute(
            "SELECT id FROM users WHERE email = ? COLLATE NOCASE AND id != ?",
            (email, user["id"]),
        ).fetchone()
        saved_email = user["email"] if conflict else email
        database.execute(
            """
            UPDATE users
            SET email = ?, display_name = ?, picture_url = ?,
                auth_provider = 'google', last_login_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (saved_email, display_name, picture_url, user["id"]),
        )
        database.commit()
        return user["id"]

    email_user = database.execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
        (email,),
    ).fetchone()
    if email_user is not None:
        if not email_user["is_active"]:
            raise ValueError("This WebManager account has been disabled.")
        if email_user["google_sub"] and email_user["google_sub"] != subject:
            raise ValueError("This email is already linked to another Google identity.")
        database.execute(
            """
            UPDATE users
            SET google_sub = ?, display_name = ?, picture_url = ?,
                auth_provider = 'google', last_login_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (subject, display_name, picture_url, email_user["id"]),
        )
        database.commit()
        return email_user["id"]

    database.execute("BEGIN IMMEDIATE")
    username = _unique_username(database, email)
    first_account = (
        database.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
    )
    cursor = database.execute(
        """
        INSERT INTO users (
            username, password_hash, google_sub, email, display_name,
            picture_url, auth_provider, is_admin, last_login_at
        ) VALUES (?, 'google-sso-only', ?, ?, ?, ?, 'google', ?, CURRENT_TIMESTAMP)
        """,
        (username, subject, email, display_name, picture_url, int(first_account)),
    )
    database.commit()
    return cursor.lastrowid


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        g.permissions = set()
    else:
        g.user = get_db().execute(
            """
            SELECT id, username, email, display_name, picture_url,
                   is_admin, is_active, created_at
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if g.user is None or not g.user["is_active"]:
            session.clear()
            g.user = None
            g.permissions = set()
            return
        g.permissions = {
            row["permission_code"]
            for row in get_db().execute(
                """
                SELECT DISTINCT group_permissions.permission_code
                FROM user_groups
                JOIN group_permissions
                  ON group_permissions.group_id = user_groups.group_id
                WHERE user_groups.user_id = ?
                """,
                (user_id,),
            ).fetchall()
        }


@bp.app_context_processor
def inject_security_helpers():
    return {
        "csrf_token": csrf_token,
        "google_configured": google_is_configured(),
        "has_permission": has_permission,
    }


@bp.get("/login")
def login():
    if g.user is not None:
        return redirect(url_for("deployments.dashboard"))
    return render_template(
        "auth/login.html",
        title="Sign in",
        next_url=_safe_next(request.args.get("next")),
    )


@bp.get("/google")
def google_login():
    if g.user is not None:
        return redirect(url_for("deployments.dashboard"))
    if not google_is_configured():
        flash("Google sign-in has not been configured on this server.", "error")
        return redirect(url_for("auth.login"))

    session["post_login_next"] = _safe_next(request.args.get("next"))
    nonce = secrets.token_urlsafe(32)
    session["google_oidc_nonce"] = nonce
    configured_redirect = current_app.config["GOOGLE_REDIRECT_URI"]
    request_host = (request.host.split(":", 1)[0] or "").lower().rstrip(".")
    if request_host in dashboard_hostnames(get_db()):
        configured = urlsplit(configured_redirect)
        scheme = configured.scheme or request.scheme
        redirect_uri = urlunsplit(
            (scheme, request_host, url_for("auth.google_callback"), "", "")
        )
    else:
        redirect_uri = configured_redirect or url_for(
            "auth.google_callback",
            _external=True,
        )
    return oauth.google.authorize_redirect(redirect_uri, nonce=nonce, prompt="select_account")


@bp.get("/google/callback")
def google_callback():
    if not google_is_configured():
        flash("Google sign-in has not been configured on this server.", "error")
        return redirect(url_for("auth.login"))

    try:
        token = oauth.google.authorize_access_token()
        claims = token.get("userinfo")
        if claims is None:
            claims = oauth.google.parse_id_token(
                token,
                nonce=session.get("google_oidc_nonce"),
            )
        if not claims.get("sub") or not claims.get("email"):
            raise ValueError("Google did not return a usable identity.")
        if claims.get("email_verified") is not True:
            raise ValueError("Google has not verified this account's email address.")
        if not _identity_is_allowed(claims):
            raise ValueError("This Google account is not allowed to use WebManager.")

        user_id = _find_or_create_user(claims)
    except OAuthError as exc:
        current_app.logger.warning("Google OAuth exchange failed: %s", exc)
        session.pop("google_oidc_nonce", None)
        session.pop("post_login_next", None)
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("auth.login"))
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        current_app.logger.warning("Google sign-in failed: %s", exc)
        session.pop("google_oidc_nonce", None)
        session.pop("post_login_next", None)
        flash(str(exc) or "Google sign-in failed.", "error")
        return redirect(url_for("auth.login"))

    destination = session.get("post_login_next") or url_for("deployments.dashboard")
    session.clear()
    session["user_id"] = user_id
    session.permanent = True
    return redirect(destination)


@bp.get("/register")
def register():
    flash("WebManager accounts are created through Google sign-in.", "info")
    return redirect(url_for("auth.login"))


@bp.post("/logout")
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("auth.login"))


@click.command("link-google-user")
@click.option("--username", required=True, help="Existing WebManager username.")
@click.option("--email", required=True, help="Verified Google account email.")
def link_google_user_command(username, email):
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise click.ClickException("Enter a valid Google account email address.")

    database = get_db()
    user = database.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    if user is None:
        raise click.ClickException(f"User {username!r} does not exist.")

    conflict = database.execute(
        "SELECT id FROM users WHERE email = ? COLLATE NOCASE AND id != ?",
        (email, user["id"]),
    ).fetchone()
    if conflict:
        raise click.ClickException("That email is already linked to another user.")

    database.execute(
        """
        UPDATE users
        SET email = ?, auth_provider = 'google_pending'
        WHERE id = ?
        """,
        (email, user["id"]),
    )
    database.commit()
    click.echo(f"Linked {username} to {email}. The link completes at the next Google sign-in.")
