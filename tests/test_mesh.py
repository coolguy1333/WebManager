"""Mesh federation tests: the /mesh/status endpoint, MeshHub, and the admin
Servers panel. See webmanager/mesh.py for the leaderless peer-federation
design (mirrors coolguy1333/Uptime-Monitor's lib/peers.js)."""

import tempfile
import unittest
from pathlib import Path

from webmanager import create_app, mesh

from tests import test_app as base


class MeshHubUnitTests(unittest.TestCase):
    """Pure-Python tests for MeshHub that don't need a Flask app."""

    def test_entries_report_one_row_per_configured_peer_in_order(self):
        hub = mesh.MeshHub(None, ["https://b.example", "https://a.example"], token="")
        entries = hub.entries()
        self.assertEqual([entry["url"] for entry in entries], ["https://b.example", "https://a.example"])
        self.assertFalse(entries[0]["reachable"])
        self.assertIsNone(entries[0]["checked_at"])

    def test_entries_keep_last_known_data_after_a_peer_goes_unreachable(self):
        hub = mesh.MeshHub(None, ["https://peer.example"], token="")
        hub._remote["https://peer.example"] = {
            "reachable": True,
            "error": None,
            "checked_at": 1.0,
            "data": {"hostname": "peer1", "sites": {"total": 2, "running": 2}},
        }
        # A later failed poll keeps the previous "data" but flips reachable.
        previous = hub._remote["https://peer.example"]
        hub._remote["https://peer.example"] = {
            "reachable": False,
            "error": "Connection refused",
            "checked_at": 2.0,
            "data": previous["data"],
        }
        entry = hub.entries()[0]
        self.assertFalse(entry["reachable"])
        self.assertEqual(entry["error"], "Connection refused")
        self.assertEqual(entry["hostname"], "peer1")
        self.assertEqual(entry["sites"], {"total": 2, "running": 2})

    def test_authorize_incoming_is_public_without_a_token(self):
        hub = mesh.MeshHub(None, [], token="")
        self.assertTrue(hub.authorize_incoming("", "1.2.3.4"))
        self.assertTrue(hub.authorize_incoming("Bearer anything", "1.2.3.4"))

    def test_authorize_incoming_requires_the_exact_token(self):
        hub = mesh.MeshHub(None, [], token="s3cret")
        self.assertFalse(hub.authorize_incoming("", "1.2.3.4"))
        self.assertFalse(hub.authorize_incoming("Bearer wrong", "1.2.3.4"))
        self.assertTrue(hub.authorize_incoming("Bearer s3cret", "1.2.3.4"))

    def test_authorize_incoming_rate_limits_repeated_failures_per_ip(self):
        hub = mesh.MeshHub(None, [], token="s3cret")
        for _ in range(11):
            self.assertFalse(hub.authorize_incoming("Bearer wrong", "9.9.9.9"))
        # Locked out even with the right token now, until the window resets.
        self.assertFalse(hub.authorize_incoming("Bearer s3cret", "9.9.9.9"))
        # A different IP is unaffected.
        self.assertTrue(hub.authorize_incoming("Bearer s3cret", "1.1.1.1"))

    def test_authorize_incoming_sweeps_expired_failures_once_the_table_grows(self):
        hub = mesh.MeshHub(None, [], token="s3cret")
        # Fill past the sweep threshold with already-expired entries.
        past = mesh.time.time() - 1
        hub._fails = {f"1.2.3.{i}": {"count": 1, "reset": past} for i in range(mesh.MAX_TRACKED_FAILURES + 1)}
        self.assertTrue(hub.authorize_incoming("Bearer s3cret", "9.9.9.9"))
        self.assertLess(len(hub._fails), mesh.MAX_TRACKED_FAILURES)

    def test_urls_are_deduplicated_and_empty_entries_dropped(self):
        hub = mesh.MeshHub(None, ["https://a.example", "https://a.example", "", "https://b.example"], token="")
        self.assertEqual(hub.urls, ["https://a.example", "https://b.example"])


class MeshEndpointTests(unittest.TestCase):
    """/mesh/status and the admin Servers panel, using the full app."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository
    add_site = base.WebManagerTestCase.add_site

    def setUp(self):
        self.setUp_base()

    def test_status_endpoint_is_public_and_exposes_only_aggregate_fields(self):
        response = self.client.get("/mesh/status")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            set(data.keys()),
            {"hostname", "version", "sites", "apps_enabled", "apps", "cpu_percent", "memory_percent", "disk_percent"},
        )
        self.assertEqual(data["sites"], {"total": 0, "running": 0})
        self.assertFalse(data["apps_enabled"])
        self.assertIsNone(data["apps"])

    def test_status_endpoint_counts_sites_and_apps_separately(self):
        user_id = self.add_user("alice")
        site_root = Path(self.temp_directory.name) / "site"
        site_root.mkdir()
        (site_root / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        self.add_site(user_id, repository_id, site_root, status="running")
        response = self.client.get("/mesh/status")
        data = response.get_json()
        self.assertEqual(data["sites"], {"total": 1, "running": 1})

    def test_status_endpoint_requires_configured_token(self):
        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret"
        hub._token_hash = mesh._hash_token("s3cret")
        try:
            self.assertEqual(self.client.get("/mesh/status").status_code, 401)
            self.assertEqual(
                self.client.get(
                    "/mesh/status", headers={"Authorization": "Bearer wrong"}
                ).status_code,
                401,
            )
            self.assertEqual(
                self.client.get(
                    "/mesh/status", headers={"Authorization": "Bearer s3cret"}
                ).status_code,
                200,
            )
        finally:
            hub.token = ""
            hub._token_hash = None

    def test_admin_servers_panel_shows_empty_state(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        response = self.client.get("/admin/?section=updates")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No other servers configured", response.data)

    def test_admin_servers_panel_shows_a_configured_peer(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        hub = self.app.extensions["mesh_hub"]
        hub.urls = ["https://peer.example.com"]
        hub._remote["https://peer.example.com"] = {
            "reachable": True,
            "error": None,
            "checked_at": 1.0,
            "data": {
                "hostname": "peer1",
                "version": "abcdef123456",
                "sites": {"total": 3, "running": 2},
                "apps_enabled": False,
                "apps": None,
                "cpu_percent": 5.0,
                "memory_percent": 10.0,
                "disk_percent": 20.0,
            },
        }
        response = self.client.get("/admin/?section=updates")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"peer1", response.data)
        self.assertIn(b"2/3 running", response.data)

    def test_admin_servers_panel_flags_missing_token(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        self.app.extensions["mesh_hub"].urls = ["https://peer.example.com"]
        response = self.client.get("/admin/?section=updates")
        self.assertIn(b"WEBMANAGER_PEER_TOKEN", response.data)

    def test_non_admin_cannot_open_servers_panel(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.get("/admin/?section=updates")
        self.assertEqual(response.status_code, 403)


class MeshSelfExclusionTests(unittest.TestCase):
    """create_app() must not let a server poll itself."""

    def test_peer_list_drops_this_servers_own_dashboard_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test-secret",
                    "DATABASE": str(root / "test.sqlite3"),
                    "REPOSITORY_ROOT": str(root / "repositories"),
                    "NGINX_ROOT": str(root / "nginx"),
                    "LOG_ROOT": str(root / "logs"),
                    "GOOGLE_REDIRECT_URI": "https://webmanager.example/auth/google/callback",
                    "MESH_PEERS": "https://webmanager.example,https://sibling.example",
                    "MESH_TOKEN": "shared-secret",
                }
            )
            self.assertEqual(app.extensions["mesh_hub"].urls, ["https://sibling.example"])

    def test_peer_urls_without_a_scheme_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test-secret",
                    "DATABASE": str(root / "test.sqlite3"),
                    "REPOSITORY_ROOT": str(root / "repositories"),
                    "NGINX_ROOT": str(root / "nginx"),
                    "LOG_ROOT": str(root / "logs"),
                    "MESH_PEERS": "not-a-url,https://good.example",
                    "MESH_TOKEN": "shared-secret",
                }
            )
            self.assertEqual(app.extensions["mesh_hub"].urls, ["https://good.example"])


if __name__ == "__main__":
    unittest.main()
