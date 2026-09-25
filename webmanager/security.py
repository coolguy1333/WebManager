import hmac
import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, g, redirect, request, session, url_for


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("_csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, "Invalid CSRF token.")


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(**kwargs)

    return wrapped_view



def safe_local_path(value):
    """Return value only if it is a same-origin absolute path, else None.

    Rejects scheme-relative (//host), backslash (/\\host, treated as // by
    browsers), and control-character tricks that enable open redirects.
    """
    if not value or not isinstance(value, str):
        return None
    if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    return value
