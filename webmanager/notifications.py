"""Tell people when something needs them: a site stopped answering, an update
failed, a backup couldn't be made.

One webhook address, set on the System page. Discord and Slack webhook
addresses get the message in the shape each expects; anything else gets a
small JSON document (``title``, ``text``, ``level``, ``server``), which ntfy,
Mattermost, Teams workflows and home-grown receivers can all read.
"""

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import peer_http
from .db import get_db, get_setting

SETTING = "notify_webhook_url"
TIMEOUT_SECONDS = 8
ICONS = {"error": "🔴", "warning": "🟠", "ok": "🟢", "info": "🔵"}


def validate_url(value: str) -> str:
    """A cleaned-up webhook address, or ValueError with what's wrong."""
    url = (value or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Enter a webhook address that starts with https:// (or http://).")
    if parts.username or parts.password:
        raise ValueError("Put any token in the address's path, not before an @.")
    if len(url) > 1000:
        raise ValueError("That address is too long.")
    return url


def build_payload(url: str, title: str, text: str, level: str, server: str) -> dict:
    host = (urlsplit(url).hostname or "").lower()
    line = f"{ICONS.get(level, ICONS['info'])} **{title}** ({server})\n{text}".strip()
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return {"content": line[:1900]}
    if host.endswith("slack.com"):
        return {"text": line.replace("**", "*")[:3000]}
    return {"title": title, "text": text, "level": level, "server": server, "content": line[:1900]}


def deliver(url: str, title: str, text: str, level: str = "info") -> tuple[bool, str]:
    """Send one message now. Returns (delivered, what happened)."""
    payload = build_payload(url, title, text, level, socket.gethostname())
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "WebManager"}, method="POST",
    )
    try:
        # Redirects are not followed: the address is used exactly as entered.
        with peer_http.open_peer(request, timeout=TIMEOUT_SECONDS) as response:
            return True, f"Delivered (HTTP {response.status})."
    except urllib.error.HTTPError as exc:
        return False, f"The webhook answered HTTP {exc.code}. Check that the address is complete and still valid."
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        message, _hint = peer_http.explain_failure(exc, TIMEOUT_SECONDS)
        reasons = {
            "Connection refused": "nothing is listening at that address and port",
            "Timed out": f"it didn't answer within {TIMEOUT_SECONDS} seconds",
            "Name not found": "that hostname could not be found",
        }
        reason = reasons.get(message, message)
        return False, f"Could not reach the webhook: {reason}. Check the address, and that this server can reach it."


class Notifier:
    def __init__(self, app):
        self.app = app

    def url(self) -> str:
        try:
            with self.app.app_context():
                return get_setting(get_db(), SETTING, "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def send(self, title: str, text: str, level: str = "info"):
        """Queue a message; returns at once. Does nothing when no webhook is set."""
        url = self.url()
        if not url:
            return

        def worker():
            delivered, outcome = deliver(url, title, text, level)
            if not delivered:
                self.app.logger.warning("Notification %r was not delivered: %s", title, outcome)

        threading.Thread(target=worker, name="webmanager-notify", daemon=True).start()
