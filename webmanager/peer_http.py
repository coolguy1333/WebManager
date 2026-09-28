"""HTTP calls to other WebManager servers.

Redirects are never followed: a peer request carries the shared token (and a
forwarded write carries the user's session cookie), so a redirect must not
send them anywhere else, and a forwarded write's own redirect has to reach
the browser intact - along with the cookies set on that response.
"""

import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_peer(request, timeout):
    """Like urllib.request.urlopen, but 3xx answers come back as HTTPError
    (whose headers and body the caller can relay) instead of being followed."""
    return _OPENER.open(request, timeout=timeout)
