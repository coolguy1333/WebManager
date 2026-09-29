"""Primary/replica replication tests: secret-key sync, database snapshot
pull/validation, and write forwarding. See webmanager/replication.py."""

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webmanager import create_app, peer_http, replication

from tests import test_app as base


def _valid_sqlite_bytes(extra_users=0) -> bytes:
    """A minimal, valid sqlite file with a "users" table (and no others),
    good enough to pass ReplicationManager's integrity/sanity check."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        for _ in range(extra_users):
            connection.execute("INSERT INTO users DEFAULT VALUES")
        connection.commit()
        connection.close()
        return path.read_bytes()


class PeerHttpTests(unittest.TestCase):
    def test_redirects_are_returned_not_followed(self):
        import http.server
        import threading
        import urllib.error
        import urllib.request

        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/done")
                self.send_header("Set-Cookie", "flash=1")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/go", data=b"x", method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            peer_http.open_peer(request, timeout=5)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(caught.exception.headers["Set-Cookie"], "flash=1")
        self.assertEqual(hits, ["/go"])


class _FakeHeaders(list):
    """list of (key, value) tuples that also supports .items(), matching
    the http.client.HTTPMessage interface urllib responses expose."""

    def items(self):
        return list(self)

    def get(self, key, default=None):
        for name, value in self:
            if name.lower() == key.lower():
                return value
        return default


class _FakeHTTPResponse(io.BytesIO):
    """Minimal stand-in for http.client.HTTPResponse / urlopen's context
    manager result: readable body plus a .status and .headers."""

    def __init__(self, body: bytes, status: int = 200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = _FakeHeaders(headers or [])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class ReplicationManagerUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.database_path = Path(self.temp_directory.name) / "webmanager.sqlite3"
        self.database_path.write_bytes(_valid_sqlite_bytes())
        self.app = MagicMock()
        self.app.config = {"DATABASE": str(self.database_path)}
        self.app.instance_path = self.temp_directory.name
        self.app.logger = MagicMock()

    def manager(self, primary_url="https://primary.example", token="s3cret-s3cret-s3cret"):
        return replication.ReplicationManager(self.app, primary_url, token)

    def test_is_replica_reflects_whether_a_primary_url_is_set(self):
        self.assertTrue(self.manager().is_replica)
        self.assertFalse(self.manager(primary_url="").is_replica)

    def test_sync_once_replaces_the_local_database_on_success(self):
        new_bytes = _valid_sqlite_bytes(extra_users=1)
        manager = self.manager()
        with patch.object(peer_http, "open_peer", return_value=_FakeHTTPResponse(new_bytes)):
            self.assertTrue(manager.sync_once())
        self.assertIsNone(manager.last_error)
        self.assertIsNotNone(manager.last_sync_at)
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            connection.close()

    def _snapshot_from(self, statements):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.sqlite3"
            connection = sqlite3.connect(path)
            for statement in statements:
                connection.execute(statement)
            connection.commit()
            connection.close()
            return path.read_bytes()

    def test_sync_once_upgrades_a_snapshot_from_a_primary_on_an_older_version(self):
        # An older primary: no app columns on sites, no apps.host permission.
        old = self._snapshot_from(
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)",
                "INSERT INTO users (username, password_hash) VALUES ('admin', 'x')",
                "CREATE TABLE sites (id INTEGER PRIMARY KEY, user_id INTEGER, repository_id INTEGER, "
                "name TEXT, slug TEXT, folder TEXT, document_root TEXT, index_file TEXT, "
                "port INTEGER, spa_fallback INTEGER, nginx_config TEXT, status TEXT)",
                "INSERT INTO sites (user_id, repository_id, name, slug, folder, document_root, "
                "index_file, port, spa_fallback, nginx_config, status) "
                "VALUES (1, 1, 'Old', 'old', '.', '/x', 'index.html', 8100, 1, '', 'stopped')",
            ]
        )
        manager = self.manager()
        with patch.object(peer_http, "open_peer", return_value=_FakeHTTPResponse(old)):
            self.assertTrue(manager.sync_once(), manager.last_error)
        connection = sqlite3.connect(self.database_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sites)")}
            self.assertLessEqual({"kind", "app_env", "use_domain_root"}, columns)
            self.assertEqual(
                connection.execute("SELECT kind FROM sites WHERE slug = 'old'").fetchone()[0],
                "static",
            )
            self.assertIsNotNone(
                connection.execute("SELECT 1 FROM permissions WHERE code = 'apps.host'").fetchone()
            )
            # Server-specific defaults are never written into a mirrored copy.
            self.assertIsNone(connection.execute("SELECT 1 FROM dashboard_domains").fetchone())
        finally:
            connection.close()

    def test_sync_once_points_stored_paths_at_this_servers_data_directory(self):
        primary_dir = "/var/lib/webmanager"
        snapshot = self._snapshot_from(
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY)",
                "CREATE TABLE repositories (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, "
                "url TEXT, local_path TEXT, pending_path TEXT)",
                f"INSERT INTO repositories (user_id, name, url, local_path, pending_path) "
                f"VALUES (1, 'r', 'u', '{primary_dir}/repositories/1/1', NULL)",
                "CREATE TABLE sites (id INTEGER PRIMARY KEY, user_id INTEGER, repository_id INTEGER, "
                "name TEXT, slug TEXT, folder TEXT, document_root TEXT, index_file TEXT, "
                "port INTEGER, nginx_config TEXT)",
                f"INSERT INTO sites (user_id, repository_id, name, slug, folder, document_root, "
                f"index_file, port, nginx_config) "
                f"VALUES (1, 1, 's', 's', '.', '{primary_dir}/repositories/1/1', 'index.html', 8100, "
                f"'server {{ root \"{primary_dir}/repositories/1/1\"; }}')",
            ]
        )
        manager = self.manager()
        response = _FakeHTTPResponse(
            snapshot, headers=[(replication.DATA_DIR_HEADER, primary_dir)]
        )
        with patch.object(peer_http, "open_peer", return_value=response):
            self.assertTrue(manager.sync_once(), manager.last_error)
        own = self.temp_directory.name
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT local_path FROM repositories").fetchone()[0],
                f"{own}/repositories/1/1",
            )
            document_root, config = connection.execute(
                "SELECT document_root, nginx_config FROM sites"
            ).fetchone()
            self.assertEqual(document_root, f"{own}/repositories/1/1")
            self.assertIn(f'root "{own}/repositories/1/1"', config)
            self.assertNotIn(primary_dir, config)
        finally:
            connection.close()

    def test_sync_once_rejects_a_corrupt_download_and_keeps_the_old_database(self):
        manager = self.manager()
        original = self.database_path.read_bytes()
        with patch.object(peer_http, "open_peer", return_value=_FakeHTTPResponse(b"not a database")):
            self.assertFalse(manager.sync_once())
        self.assertIsNotNone(manager.last_error)
        self.assertEqual(self.database_path.read_bytes(), original)

    def test_sync_once_handles_an_unreachable_primary(self):
        manager = self.manager()
        with patch.object(
            peer_http, "open_peer",
            side_effect=replication.urllib.error.URLError("Connection refused"),
        ):
            self.assertFalse(manager.sync_once())
        self.assertIn("Connection refused", manager.last_error)

    def test_fetch_secret_key_returns_the_stripped_body(self):
        with patch.object(
            peer_http, "open_peer", return_value=_FakeHTTPResponse(b"the-secret\n")
        ):
            key = replication.fetch_secret_key("https://primary.example", "s3cret-s3cret-s3cret")
        self.assertEqual(key, "the-secret")

    def test_fetch_secret_key_explains_a_404_from_the_wrong_port(self):
        error = replication.urllib.error.HTTPError(
            "http://primary.example/mesh/secret-key", 404, "Not Found", {}, None
        )
        with patch.object(peer_http, "open_peer", side_effect=error):
            with self.assertRaises(replication.ReplicationError) as caught:
                replication.fetch_secret_key("http://primary.example", "t" * 16)
        self.assertIn("isn't reaching a WebManager", str(caught.exception))
        self.assertIn("8080", str(caught.exception))

    def test_fetch_secret_key_raises_on_network_failure(self):
        with patch.object(
            peer_http, "open_peer",
            side_effect=replication.urllib.error.URLError("no route"),
        ):
            with self.assertRaises(replication.ReplicationError):
                replication.fetch_secret_key("https://primary.example", "s3cret-s3cret-s3cret")


class ReplicationEndpointTests(unittest.TestCase):
    """/replication/db-snapshot and /mesh/secret-key, using the full app."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def setUp(self):
        self.setUp_base()

    def test_db_snapshot_requires_a_token_even_when_mesh_status_is_public(self):
        # /mesh/status is intentionally public with no token configured;
        # /replication/db-snapshot must never be, since it's a full dump.
        response = self.client.get("/replication/db-snapshot")
        self.assertEqual(response.status_code, 401)

    def test_secret_key_endpoint_requires_a_token_even_when_mesh_status_is_public(self):
        response = self.client.get("/mesh/secret-key")
        self.assertEqual(response.status_code, 401)

    def test_db_snapshot_and_secret_key_are_served_with_the_right_token(self):
        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret-s3cret-s3cret"
        hub._token_hash = __import__("webmanager.mesh", fromlist=["_hash_token"])._hash_token("s3cret-s3cret-s3cret")
        try:
            snapshot = self.client.get(
                "/replication/db-snapshot", headers={"Authorization": "Bearer s3cret-s3cret-s3cret"}
            )
            self.assertEqual(snapshot.status_code, 200)
            connection = sqlite3.connect(":memory:")
            connection.close()  # sanity: sqlite module available
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
                handle.write(snapshot.data)
                handle.flush()
                downloaded = sqlite3.connect(handle.name)
                try:
                    row = downloaded.execute("PRAGMA integrity_check").fetchone()
                    self.assertEqual(row[0], "ok")
                    downloaded.execute("SELECT COUNT(*) FROM users").fetchone()
                finally:
                    downloaded.close()

            secret = self.client.get("/mesh/secret-key", headers={"Authorization": "Bearer s3cret-s3cret-s3cret"})
            self.assertEqual(secret.status_code, 200)
            self.assertEqual(secret.data.decode("utf-8"), self.app.config["SECRET_KEY"])
        finally:
            hub.token = ""
            hub._token_hash = None


class WriteForwardingTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()

    def test_replica_forwards_a_write_to_the_primary_and_relays_the_response(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            user_id = self.add_user("alice", is_admin=True)
            self.login_user(user_id)
            primary_response = _FakeHTTPResponse(
                b'{"ok": true}',
                status=200,
                headers=[("Content-Type", "application/json"), ("Set-Cookie", "example=1")],
            )
            with patch.object(peer_http, "open_peer", return_value=primary_response) as urlopen:
                response = self.client.post(
                    "/admin/sources/check-all", data={"_csrf_token": self.csrf()}
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'{"ok": true}')
            self.assertEqual(response.headers.get("Set-Cookie"), "example=1")
            self.assertTrue(urlopen.called)
            forwarded_request = urlopen.call_args[0][0]
            self.assertEqual(forwarded_request.full_url, "https://primary.example/admin/sources/check-all")
            self.assertEqual(forwarded_request.get_method(), "POST")
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_replica_does_not_forward_its_own_program_update_actions(self):
        # WebManager's self-update is a per-machine install; forwarding it
        # would update the primary's checkout while telling this admin it
        # happened here.
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            with patch.object(peer_http, "open_peer") as urlopen:
                response = self.client.post(
                    "/admin/updates/check", data={"_csrf_token": self.csrf()}
                )
            self.assertIn(response.status_code, (302, 303))
            urlopen.assert_not_called()
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_replica_does_not_forward_reads(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            with patch.object(peer_http, "open_peer") as urlopen:
                response = self.client.get("/mesh/status")
            self.assertEqual(response.status_code, 200)
            urlopen.assert_not_called()
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_replica_returns_502_when_primary_is_unreachable(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            user_id = self.add_user("alice", is_admin=True)
            self.login_user(user_id)
            with patch.object(
                peer_http, "open_peer",
                side_effect=replication.urllib.error.URLError("Connection refused"),
            ):
                response = self.client.post(
                    "/admin/sources/check-all", data={"_csrf_token": self.csrf()}
                )
            self.assertEqual(response.status_code, 502)
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_browser_users_get_a_readable_page_when_the_primary_is_down(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            user_id = self.add_user("alice", is_admin=True)
            self.login_user(user_id)
            with patch.object(
                peer_http, "open_peer",
                side_effect=replication.urllib.error.URLError("Connection refused"),
            ):
                response = self.client.post(
                    "/admin/sources/check-all",
                    data={"_csrf_token": self.csrf()},
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
            self.assertEqual(response.status_code, 502)
            self.assertIn(b"Primary server unavailable", response.data)
            self.assertIn(b"nothing was saved", response.data)
            self.assertNotIn(b'{"error"', response.data)
        finally:
            manager.primary_url = ""
            manager.token = ""


class ReplicationAdminPanelTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()

    def test_sidebar_shows_a_replica_indicator_on_every_page(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        try:
            response = self.client.get("/")
            self.assertIn(b"Replica", response.data)
        finally:
            manager.primary_url = ""

    def test_sidebar_has_no_replica_indicator_on_a_primary(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        response = self.client.get("/")
        self.assertNotIn(b"\xc2\xb7 Replica", response.data)

    def test_system_page_shows_primary_by_default(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        response = self.client.get("/admin/?section=updates")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This server is the primary", response.data)

    def test_system_page_shows_replica_status_and_sync_button(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        manager.last_error = "Connection refused"
        try:
            response = self.client.get("/admin/?section=updates")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"mirrors", response.data)
            self.assertIn(b"https://primary.example", response.data)
            self.assertIn(b"Connection refused", response.data)
            self.assertIn(b"Sync config now", response.data)
            self.assertIn(b"Sync data now", response.data)
        finally:
            manager.primary_url = ""
            manager.token = ""
            manager.last_error = None

    def test_sync_now_is_not_forwarded_and_reports_the_outcome(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            with patch.object(peer_http, "open_peer") as urlopen:
                urlopen.side_effect = replication.urllib.error.URLError("no route")
                response = self.client.post(
                    "/admin/replication/sync",
                    data={"_csrf_token": self.csrf()},
                    follow_redirects=True,
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Could not sync from the primary", response.data)
            # It ran locally (called urlopen itself) rather than being
            # forwarded to the primary as a generic write would be.
            urlopen.assert_called_once()
            forwarded_request = urlopen.call_args[0][0]
            self.assertIn("/replication/db-snapshot", forwarded_request.full_url)
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_sync_now_requires_admin(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.post(
            "/admin/replication/sync", data={"_csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 403)


class PromoteToPrimaryTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()

    def test_promote_stops_mirroring_and_reports_not_yet_durable(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["replication_manager"]
        data_manager = self.app.extensions["data_replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        data_manager.primary_url = "https://primary.example"
        data_manager.token = "s3cret-s3cret-s3cret"

        response = self.client.post(
            "/admin/replication/promote",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"now the primary", response.data)
        self.assertIn(b"WEBMANAGER_REPLICA_OF", response.data)
        self.assertFalse(manager.is_replica)
        self.assertFalse(data_manager.is_replica)
        self.assertEqual(self.app.config["REPLICA_OF"], "")

        # A write no longer gets forwarded anywhere - it just runs locally.
        with patch.object(peer_http, "open_peer") as urlopen:
            self.client.post("/admin/updates/check", data={"_csrf_token": self.csrf()})
        urlopen.assert_not_called()

    def test_promote_is_a_no_op_on_an_existing_primary(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        response = self.client.post(
            "/admin/replication/promote",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"already a primary", response.data)

    def test_promote_requires_admin(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.post(
            "/admin/replication/promote", data={"_csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 403)


class GoogleSignInOnReplicaTests(unittest.TestCase):
    """A replica must never write its own database - including the user
    row Google sign-in creates/touches on every login (see auth.py's
    google_callback and replication.find_or_create_user_via_primary)."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    google_claims = base.WebManagerTestCase.google_claims
    google_callback = base.WebManagerTestCase.google_callback

    def setUp(self):
        self.setUp_base()
        self.app.config["GOOGLE_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
        self.app.config["GOOGLE_CLIENT_SECRET"] = "test-client-secret"

    def _user_count(self):
        from webmanager.db import get_db

        with self.app.app_context():
            return get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def test_new_sign_in_on_a_replica_asks_the_primary_instead_of_writing_locally(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            before = self._user_count()
            primary_response = _FakeHTTPResponse(b'{"user_id": 77}')
            with patch.object(
                peer_http, "open_peer", return_value=primary_response
            ) as urlopen:
                response = self.google_callback()
            self.assertEqual(response.status_code, 302)
            forwarded_request = urlopen.call_args[0][0]
            self.assertEqual(
                forwarded_request.full_url, "https://primary.example/replication/find-or-create-user"
            )
            # No local row was created; the replica only used the id primary gave back.
            self.assertEqual(self._user_count(), before)
            with self.client.session_transaction() as session:
                self.assertEqual(session["user_id"], 77)
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_sign_in_on_a_replica_fails_cleanly_when_primary_is_unreachable(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            before = self._user_count()
            with patch.object(
                peer_http, "open_peer",
                side_effect=replication.urllib.error.URLError("no route"),
            ):
                response = self.google_callback()
            self.assertEqual(response.status_code, 302)
            self.assertIn("/auth/login", response.headers["Location"])
            self.assertEqual(self._user_count(), before)
            with self.client.session_transaction() as session:
                self.assertNotIn("user_id", session)
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_sign_in_on_a_primary_still_writes_locally_as_before(self):
        before = self._user_count()
        response = self.google_callback()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._user_count(), before + 1)


class FindOrCreateUserEndpointTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    google_claims = base.WebManagerTestCase.google_claims

    def setUp(self):
        self.setUp_base()

    def test_requires_a_token(self):
        response = self.client.post(
            "/replication/find-or-create-user", json=self.google_claims()
        )
        self.assertEqual(response.status_code, 401)

    def test_rejects_missing_claims(self):
        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret-s3cret-s3cret"
        from webmanager.mesh import _hash_token

        hub._token_hash = _hash_token("s3cret-s3cret-s3cret")
        try:
            response = self.client.post(
                "/replication/find-or-create-user",
                json={"sub": "x"},  # missing email
                headers={"Authorization": "Bearer s3cret-s3cret-s3cret"},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            hub.token = ""
            hub._token_hash = None

    def test_creates_a_user_and_returns_its_id_with_the_right_token(self):
        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret-s3cret-s3cret"
        from webmanager.mesh import _hash_token

        hub._token_hash = _hash_token("s3cret-s3cret-s3cret")
        try:
            response = self.client.post(
                "/replication/find-or-create-user",
                json=self.google_claims(),
                headers={"Authorization": "Bearer s3cret-s3cret-s3cret"},
            )
            self.assertEqual(response.status_code, 200)
            user_id = response.get_json()["user_id"]
            from webmanager.db import get_db

            with self.app.app_context():
                row = get_db().execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
            self.assertEqual(row["email"], "alice@example.com")
        finally:
            hub.token = ""
            hub._token_hash = None


class ReplicaStartupTests(unittest.TestCase):
    def test_replica_requires_a_peer_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(RuntimeError):
                create_app(
                    {
                        "TESTING": True,
                        "DATABASE": str(root / "test.sqlite3"),
                        "REPOSITORY_ROOT": str(root / "repositories"),
                        "NGINX_ROOT": str(root / "nginx"),
                        "LOG_ROOT": str(root / "logs"),
                        "REPLICA_OF": "https://primary.example",
                    }
                )

    def test_replica_role_is_set_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "shared-secret",
                    "DATABASE": str(root / "test.sqlite3"),
                    "REPOSITORY_ROOT": str(root / "repositories"),
                    "NGINX_ROOT": str(root / "nginx"),
                    "LOG_ROOT": str(root / "logs"),
                    "REPLICA_OF": "https://primary.example",
                    "MESH_TOKEN": "s3cret-s3cret-s3cret",
                }
            )
            self.assertTrue(app.extensions["replication_manager"].is_replica)
            self.assertEqual(app.extensions["replication_manager"].primary_url, "https://primary.example")


if __name__ == "__main__":
    unittest.main()
