"""Joining servers without editing settings files: the System page's
"Turn on server sharing" / "Add a server", and servers announcing themselves."""

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from webmanager import create_app, mesh

from tests import test_app as base


class NormalizePeerUrlTests(unittest.TestCase):
    def test_accepts_bare_addresses(self):
        cases = {
            "http://192.168.10.30:8080": "http://192.168.10.30:8080",
            "https://b.example/": "https://b.example",
            "192.168.10.30:8080": "http://192.168.10.30:8080",
            "  HTTP://b.example  ": "http://b.example",
            "http://[fd00::5]:8080": "http://[fd00::5]:8080",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(mesh.normalize_peer_url(given), expected.replace("HTTP", "http"))

    def test_rejects_anything_that_is_not_just_a_server_address(self):
        for given in (
            "", "ftp://b.example", "http://user:pw@b.example", "http://b.example/admin",
            "http://b.example?x=1", "http://b.example:99999", "javascript:alert(1)", "http://",
        ):
            with self.subTest(given=given):
                self.assertIsNone(mesh.normalize_peer_url(given))


class ServerSharingTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.state = Path(self.temp_directory.name) / "mesh-state"
        self.app.config["MESH_STATE_DIR"] = str(self.state)
        self.hub = self.app.extensions["mesh_hub"]
        self.admin_id = self.add_user("root", is_admin=True)
        self.login_user(self.admin_id)

    def post(self, path, **data):
        return self.client.post(path, data={"_csrf_token": self.csrf(), **data})

    def test_turning_sharing_on_creates_a_private_token_that_works_at_once(self):
        response = self.post("/admin/servers/sharing")

        self.assertIn(response.status_code, (302, 303))
        token = (self.state / "peer-token").read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(token), mesh.MIN_TOKEN_LENGTH)
        self.assertEqual(stat.S_IMODE(os.stat(self.state / "peer-token").st_mode), 0o600)
        self.assertEqual(self.hub.token, token)
        self.assertTrue(
            self.hub.authorize_sensitive(f"Bearer {token}", "10.0.0.5"),
            "peers must be able to use the token without a restart",
        )

        self.post("/admin/servers/sharing")  # a second click keeps the same token
        self.assertEqual((self.state / "peer-token").read_text(encoding="utf-8").strip(), token)

    def test_the_join_command_is_shown_once_sharing_is_on(self):
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        self.assertIn("Turn on server sharing", page)
        self.assertNotIn("--peer-token", page)

        self.post("/admin/servers/sharing")
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        token = self.hub.token
        self.assertIn(f"--peer-token {token}", page)
        self.assertIn("--replica-of http://localhost", page)
        self.assertIn("--announce", page)

    def test_only_a_super_admin_can_change_sharing_or_the_server_list(self):
        self.login_user(self.add_user("alice"))
        for path, data in (
            ("/admin/servers/sharing", {}),
            ("/admin/servers/add", {"url": "http://b.example:8080"}),
            ("/admin/servers/remove", {"url": "http://b.example:8080"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, **data).status_code, 403)
        self.assertFalse((self.state / "peer-token").exists())

    def test_added_servers_are_saved_listed_and_removable(self):
        self.post("/admin/servers/sharing")
        with patch.object(mesh.MeshHub, "probe", return_value=(True, None, None)):
            self.post("/admin/servers/add", url="192.168.10.30:8080")

        self.assertIn("http://192.168.10.30:8080", self.hub.urls)
        entry = [e for e in self.hub.entries() if e["url"] == "http://192.168.10.30:8080"][0]
        self.assertTrue(entry["removable"])
        self.assertEqual(mesh.load_saved_peers(self.app), ["http://192.168.10.30:8080"])

        self.post("/admin/servers/remove", url="http://192.168.10.30:8080")
        self.assertNotIn("http://192.168.10.30:8080", self.hub.urls)
        self.assertEqual(mesh.load_saved_peers(self.app), [])

    def test_a_server_that_is_not_answering_is_still_added_with_an_explanation(self):
        self.post("/admin/servers/sharing")
        with patch.object(mesh.MeshHub, "probe", return_value=(False, "Connection refused.", "Check the port.")):
            response = self.post("/admin/servers/add", url="http://192.168.10.31:8080")
        self.assertIn("http://192.168.10.31:8080", self.hub.urls)
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("isn&#39;t answering yet", page)

    def test_servers_from_the_settings_file_cannot_be_removed_here(self):
        self.hub.urls.append("https://from-settings.example")
        self.post("/admin/servers/remove", url="https://from-settings.example")
        self.assertIn("https://from-settings.example", self.hub.urls)

    def test_bad_addresses_and_missing_token_are_refused(self):
        self.post("/admin/servers/add", url="http://b.example:8080")  # sharing still off
        self.assertNotIn("http://b.example:8080", self.hub.urls)
        self.post("/admin/servers/sharing")
        self.post("/admin/servers/add", url="http://b.example/admin")
        self.assertEqual(mesh.load_saved_peers(self.app), [])

    def test_token_and_servers_survive_a_restart(self):
        self.post("/admin/servers/sharing")
        with patch.object(mesh.MeshHub, "probe", return_value=(True, None, None)):
            self.post("/admin/servers/add", url="http://192.168.10.30:8080")
        token = self.hub.token

        root = Path(self.temp_directory.name)
        again = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": str(root / "again.sqlite3"),
                "REPOSITORY_ROOT": str(root / "repositories"),
                "NGINX_ROOT": str(root / "nginx"),
                "LOG_ROOT": str(root / "logs"),
                "MESH_STATE_DIR": str(self.state),
            }
        )
        hub = again.extensions["mesh_hub"]
        self.assertEqual(hub.token, token)
        self.assertEqual(hub.urls, ["http://192.168.10.30:8080"])
        self.assertEqual(again.config["MESH_TOKEN"], token)


class RegisterEndpointTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    TOKEN = "shared-secret-0123456789"

    def setUp(self):
        self.setUp_base()
        self.state = Path(self.temp_directory.name) / "mesh-state"
        self.app.config["MESH_STATE_DIR"] = str(self.state)
        self.hub = self.app.extensions["mesh_hub"]
        self.hub.set_token(self.TOKEN)

    def register(self, body, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/mesh/register", json=body, headers=headers)

    def test_needs_the_shared_token(self):
        self.assertEqual(self.register({"url": "http://b.example:8080"}, token=None).status_code, 401)
        self.assertEqual(self.register({"url": "http://b.example:8080"}, token="wrong-token-0123456789").status_code, 401)
        self.assertNotIn("http://b.example:8080", self.hub.urls)

    def test_a_server_that_answers_is_listed(self):
        with patch.object(mesh.MeshHub, "probe", return_value=(True, None, None)) as probe:
            response = self.register({"url": "http://192.168.10.40:8080/"})
        self.assertEqual(response.status_code, 200)
        probe.assert_called_once_with("http://192.168.10.40:8080")
        self.assertIn("http://192.168.10.40:8080", self.hub.urls)
        self.assertEqual(mesh.load_saved_peers(self.app), ["http://192.168.10.40:8080"])

    def test_an_address_that_does_not_answer_is_not_kept(self):
        with patch.object(mesh.MeshHub, "probe", return_value=(False, "Connection refused.", None)):
            response = self.register({"url": "http://192.168.10.41:8080"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("http://192.168.10.41:8080", self.hub.urls)

    def test_junk_is_rejected(self):
        for body in ({}, {"url": "ftp://x"}, {"url": "http://b.example/path"}, [], "nope"):
            with self.subTest(body=body):
                self.assertEqual(self.register(body).status_code, 400)


if __name__ == "__main__":
    unittest.main()
