import re
import secrets
import sqlite3
from urllib.parse import urlencode, urlsplit, urlunsplit

import click
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from . import activity
from .access_control import has_permission
from .db import get_db
from .domains import dashboard_hostnames
from .security import csrf_token, safe_local_path, validate_csrf


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
    return safe_local_path(value)


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


REFUSED_MESSAGE = (
    "This Google account isn't allowed. Ask the admin to add it to "
    "WEBMANAGER_GOOGLE_ALLOWED_EMAILS."
)


def allow_lists_configured():
    return bool(
        _configured_values("GOOGLE_ALLOWED_EMAILS")
        or _configured_values("GOOGLE_ALLOWED_DOMAINS")
    )


def _identity_is_listed(claims):
    email = claims["email"].lower()
    hosted_domain = str(claims.get("hd", "")).lower()
    return (
        email in _configured_values("GOOGLE_ALLOWED_EMAILS")
        or hosted_domain in _configured_values("GOOGLE_ALLOWED_DOMAINS")
    )


def _identity_is_allowed(claims, *, account_exists, first_account=False):
    """Whether this Google identity may sign in.

    With WEBMANAGER_GOOGLE_ALLOWED_EMAILS or _DOMAINS set, only listed
    identities. With both empty, only accounts that already exist, plus the
    very first account on an empty database (so setup works): a stranger
    never gets a new account."""
    if allow_lists_configured():
        return _identity_is_listed(claims)
    return account_exists or first_account


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
        if not _identity_is_allowed(claims, account_exists=True):
            raise ValueError(REFUSED_MESSAGE)
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
            SET email = ?, display_name = COALESCE(NULLIF(display_name, ''), ?), picture_url = ?,
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
        if not _identity_is_allowed(claims, account_exists=True):
            raise ValueError(REFUSED_MESSAGE)
        if not email_user["is_active"]:
            raise ValueError("This WebManager account has been disabled.")
        if email_user["google_sub"] and email_user["google_sub"] != subject:
            raise ValueError("This email is already linked to another Google identity.")
        database.execute(
            """
            UPDATE users
            SET google_sub = ?, display_name = COALESCE(NULLIF(display_name, ''), ?), picture_url = ?,
                auth_provider = 'google', last_login_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (subject, display_name, picture_url, email_user["id"]),
        )
        database.commit()
        return email_user["id"]

    database.execute("BEGIN IMMEDIATE")
    first_account = (
        database.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
    )
    if not _identity_is_allowed(claims, account_exists=False, first_account=first_account):
        database.rollback()
        raise ValueError(REFUSED_MESSAGE)
    username = _unique_username(database, email)
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
        # A plan can let its people host apps.
        from .plans import plan_for

        g.plan = plan_for(get_db(), {"id": user_id})
        if g.plan is not None and g.plan["can_host_apps"]:
            g.permissions.add("apps.host")


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
    manager = current_app.extensions.get("replication_manager")
    return render_template(
        "auth/login.html",
        title="Sign in",
        next_url=_safe_next(request.args.get("next")),
        primary_url=manager.primary_url if manager is not None and manager.is_replica else None,
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
        # The full check (existing account, first account) happens where the
        # account is written: here, or on the primary for a replica.
        if allow_lists_configured() and not _identity_is_listed(claims):
            raise ValueError(REFUSED_MESSAGE)

        replication_manager = current_app.extensions["replication_manager"]
        if replication_manager.is_replica:
            # This server must never write its own database (the next sync
            # would overwrite a locally-created user); ask the primary to
            # do it instead.
            from .replication import ReplicationError, find_or_create_user_via_primary

            try:
                user_id = find_or_create_user_via_primary(replication_manager, claims)
            except ReplicationError as exc:
                current_app.logger.warning("Sign-in could not reach the primary: %s", exc)
                session.pop("google_oidc_nonce", None)
                session.pop("post_login_next", None)
                flash("Sign-in isn't available right now (the primary server could not be reached). Try again shortly.", "error")
                return redirect(url_for("auth.login"))
        else:
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
    activity.record_sign_in(current_app, user_id)
    return redirect(destination)


# ---------------------------------------------------------------------------
# Signing in on a replica through its primary
#
# Google only sends people back to addresses registered with it, which is the
# primary's. So a replica needs no Google setup of its own: it sends the
# browser to the primary, the primary signs the person in as usual and sends
# them back with a short-lived note signed with the key both servers share.
# ---------------------------------------------------------------------------
HANDOFF_SALT = "webmanager-sign-in-through-primary"
HANDOFF_SECONDS = 120
HANDOFF_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


def _handoff_signer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=HANDOFF_SALT)


@bp.get("/via-primary")
def via_primary():
    """On a replica: start signing in through the primary."""
    if g.user is not None:
        return redirect(url_for("deployments.dashboard"))
    manager = current_app.extensions["replication_manager"]
    if not manager.is_replica:
        return redirect(url_for("auth.login"))
    from . import server_aliases
    from .domains import dashboard_hostname

    state = secrets.token_urlsafe(24)
    scheme = server_aliases.request_scheme()
    own_address = f"{scheme}://{request.host}"
    primary_address = manager.primary_url
    if server_aliases.is_alias_host(get_db(), request.host) and dashboard_hostname():
        # Opened as replicaN.<dashboard domain>, perhaps from outside the
        # local network: go through the primary's public address, not the
        # local one this server syncs from.
        primary_address = f"{scheme}://{dashboard_hostname()}"
    session["handoff_state"] = state
    session["handoff_address"] = own_address
    session["post_login_next"] = _safe_next(request.args.get("next"))
    return redirect(
        f"{primary_address}/auth/handoff?" + urlencode({"return": own_address, "state": state})
    )


@bp.get("/handoff")
def handoff():
    """On the primary: sign the person in here, then send them back to the
    server they came from, which must be one this server already lists."""
    from .mesh import normalize_peer_url

    target = normalize_peer_url(request.args.get("return", ""))
    state = request.args.get("state", "")
    if target is None or not HANDOFF_STATE_RE.fullmatch(state):
        abort(400, "This sign-in link is not valid. Start again from the other server.")
    from . import server_aliases

    replica_addresses = {
        f"{scheme}://{hostname}" for hostname in server_aliases.hostnames(get_db()) for scheme in ("http", "https")
    }
    if target not in current_app.extensions["mesh_hub"].urls and target not in replica_addresses:
        abort(
            400,
            f"{target} is not in this server's list of servers, so it can't sign people in there. "
            "Add it under System → Servers (Add by address), then try again.",
        )
    if g.user is None:
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    token = _handoff_signer().dumps({"user": g.user["id"], "for": target, "state": state})
    return redirect(f"{target}/auth/handoff/complete?" + urlencode({"token": token}))


@bp.get("/handoff/complete")
def handoff_complete():
    """Back on the replica: check the primary's note and sign the person in."""
    from itsdangerous import BadData

    expected_state = session.pop("handoff_state", None)
    expected_address = session.pop("handoff_address", None)
    destination = session.pop("post_login_next", None) or url_for("deployments.dashboard")
    try:
        note = _handoff_signer().loads(request.args.get("token", ""), max_age=HANDOFF_SECONDS)
        valid = (
            isinstance(note, dict)
            and expected_state is not None
            and secrets.compare_digest(str(note.get("state", "")), expected_state)
            and note.get("for") == expected_address
        )
    except BadData:
        valid = False
    user = None
    if valid:
        user = get_db().execute(
            "SELECT id, is_active FROM users WHERE id = ?", (note.get("user"),)
        ).fetchone()
        if user is None:
            # Signed in on the primary moments ago for the first time: the
            # copy of its database here may not have the account yet.
            current_app.extensions["replication_manager"].sync_once()
            user = get_db().execute(
                "SELECT id, is_active FROM users WHERE id = ?", (note.get("user"),)
            ).fetchone()
    if user is None or not user["is_active"]:
        flash("Signing in through the primary didn't work. Please try again.", "error")
        return redirect(url_for("auth.login"))
    session.clear()
    session["user_id"] = user["id"]
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
