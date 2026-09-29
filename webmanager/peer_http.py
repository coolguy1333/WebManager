"""HTTP calls to other WebManager servers.

Redirects are never followed: a peer request carries the shared token (and a
forwarded write carries the user's session cookie), so a redirect must not
send them anywhere else, and a forwarded write's own redirect has to reach
the browser intact - along with the cookies set on that response.
"""

import socket
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_peer(request, timeout):
    """Like urllib.request.urlopen, but 3xx answers come back as HTTPError
    (whose headers and body the caller can relay) instead of being followed."""
    return _OPENER.open(request, timeout=timeout)


def explain_failure(exc, timeout=None) -> tuple[str, str]:
    """(technical message, plain-language hint) for a failed peer request."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        message = f"HTTP {code}"
        if code == 401:
            hint = (
                "It rejected the peer token: WEBMANAGER_PEER_TOKEN must be identical "
                "on both servers (16+ characters)."
            )
        elif code == 404:
            hint = (
                "HTTP 404: that address answered, but it isn't reaching a WebManager. "
                "Use the other server's dashboard address (for example http://SERVER-IP:8080, "
                "after re-running setup.sh there) and make sure it runs the latest WebManager."
            )
        elif 300 <= code < 400:
            hint = f"It redirected to {exc.headers.get('Location') or 'another address'}; use that final address instead."
        else:
            hint = f"It answered HTTP {code}."
        return message, hint
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
        waited = f" within {timeout} seconds" if timeout else ""
        return "Timed out", f"No answer{waited}. Check the network and any firewall."
    if isinstance(exc, ValueError):
        return "Unexpected response", "It answered, but not like a WebManager. Check the address."
    reason = str(getattr(exc, "reason", None) or exc)
    if "refused" in reason.lower():
        return "Connection refused", "Nothing is listening at that address and port. Check the port and that WebManager is running."
    if "name or service not known" in reason.lower() or "getaddrinfo" in reason.lower():
        return "Name not found", "That hostname could not be found. Check the spelling and your DNS."
    return reason, "Could not connect. Check the address, the network and any firewall."


def describe_failure(exc, timeout=None) -> str:
    """One readable sentence for logs and the System page."""
    message, hint = explain_failure(exc, timeout)
    return f"{message}. {hint}"
