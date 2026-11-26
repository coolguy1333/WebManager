"""Replica names: replica1.<dashboard domain> opens that replica from
anywhere, passed through by the primary."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from webmanager import peer_http, server_aliases
from webmanager.db import get_db

from tests import test_app as base

REPLICA = "http://192.168.10.116:8080"
OTHER = "http://192.168.10.117:8080"
TOKEN = "s3cret-s3cret-s3cret"


class _Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *_args):
        return self.body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class ReplicaNameTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        with self.app.app_context():
            self.domain = server_aliases.base_domain(get_db())
        self.assertEqual(self.domain, "webmanager.example")

    def db(self):
        return get_db()

    def test_numbers_are_handed_out_in_order_and_kept(self):
        with self.app.app_context():
            self.assertEqual(server_aliases.assign(self.db(), REPLICA), (1, True))
            self.assertEqual(server_aliases.assign(self.db(), OTHER), (2, True))
            self.assertEqual(server_aliases.assign(self.db(), REPLICA), (1, False))
            self.assertTrue(server_aliases.remove(self.db(), REPLICA))
            self.assertEqual(server_aliases.assign(self.db(), "http://192.168.10.118:8080"), (1, True))
            with self.assertRaises(ValueError):
                server_aliases.assign(self.db(), "http://evil;}")

    def test_names_live_under_the_dashboard_domain(self):
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
            self.assertEqual(server_aliases.routes(self.db()), [("replica1.webmanager.example", REPLICA)])
            self.assertEqual(
                server_aliases.public_urls(self.db(), "https"), {REPLICA: "https://replica1.webmanager.example"}
            )
            database = self.db()
            database.execute("UPDATE dashboard_domains SET name = '192.168.10.20' WHERE is_primary = 1")
            database.commit()
            self.assertEqual(server_aliases.routes(database), [], "no names under an IP address")

    def test_a_site_already_called_replica1_keeps_its_name(self):
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
            with patch.object(server_aliases, "_site_hostnames", return_value={"replica1.webmanager.example"}):
                self.assertEqual(server_aliases.routes(self.db()), [])

    def nginx_conf(self):
        self.app.config["NGINX_BINARY"] = "/bin/true"
        runtime = self.app.extensions["runtime_manager"]
        with self.app.app_context():
            database = get_db()
            database.execute("INSERT INTO domains (name, is_default) VALUES ('webmanager.example', 1) ON CONFLICT DO NOTHING")
            database.commit()
            runtime.sync_nginx_configs()
        return (Path(self.app.config["NGINX_ROOT"]) / "nginx.conf").read_text(encoding="utf-8")

    def test_the_primary_passes_the_name_on_to_the_replica(self):
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
        with patch.object(server_aliases, "is_this_server", return_value=False):
            conf = self.nginx_conf()
        block = conf[conf.index("server_name replica1.webmanager.example;"):]
        self.assertIn(f"proxy_pass {REPLICA};", block[:600])
        self.assertIn("X-Forwarded-Proto $http_x_forwarded_proto", block[:900])

    def test_a_replica_answers_its_name_as_its_own_dashboard(self):
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "http://192.168.10.20:8080"
        self.addCleanup(setattr, manager, "primary_url", "")
        conf = self.nginx_conf()
        self.assertIn("server_name webmanager.example replica1.webmanager.example;", conf)
        self.assertNotIn(f"proxy_pass {REPLICA}", conf)

    def test_a_name_that_points_at_this_server_is_answered_here(self):
        # A promoted replica keeps its old number: never pass it to itself.
        self.assertTrue(server_aliases.is_this_server("http://127.0.0.1:8080"))
        self.assertFalse(server_aliases.is_this_server("http://203.0.113.9:8080"))
        with self.app.app_context():
            server_aliases.assign(self.db(), "http://127.0.0.1:8080")
            own, passed_on = server_aliases.nginx_plan(self.db(), is_replica=False)
        self.assertEqual((own, passed_on), (["replica1.webmanager.example"], []))

    def test_the_primary_names_a_server_when_it_reports_being_a_replica(self):
        hub = self.app.extensions["mesh_hub"]
        hub.set_token(TOKEN)
        runtime = self.app.extensions["runtime_manager"]
        with patch.object(peer_http, "open_peer", return_value=_Answer({"role": "primary", "sites": {}})):
            hub._poll(OTHER)
        with patch.object(peer_http, "open_peer", return_value=_Answer({"role": "replica", "sites": {}})), \
                patch.object(runtime, "apply_nginx_configs") as apply, \
                patch.object(type(runtime), "nginx_binary", "/bin/true"):
            hub._poll(REPLICA)
            hub._poll(REPLICA)
        apply.assert_called_once()
        with self.app.app_context():
            self.assertEqual(server_aliases.load(self.db()), {1: REPLICA}, "only the replica gets a name")

    def test_the_servers_list_opens_it_at_its_name(self):
        hub = self.app.extensions["mesh_hub"]
        hub.set_token(TOKEN)
        hub.urls.append(REPLICA)
        self.addCleanup(hub.urls.remove, REPLICA)
        with hub._lock:
            hub._remote[REPLICA] = {"reachable": True, "checked_at": 1, "last_ok_at": 1,
                                    "data": {"hostname": "replica-box", "role": "replica", "sites": {}}}
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
        self.login_user(self.add_user("root", is_admin=True))
        page = self.client.get(
            "/admin/?section=updates", headers={"X-Forwarded-Proto": "https"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        ).get_data(as_text=True)
        self.assertIn('href="https://replica1.webmanager.example/admin/?section=updates"', page)
        self.assertIn('href="https://replica1.webmanager.example"', page)

    def test_removing_the_server_frees_its_name(self):
        hub = self.app.extensions["mesh_hub"]
        hub.set_token(TOKEN)
        hub.add_saved_url(REPLICA, persist=False)
        with self.app.app_context():
            server_aliases.assign(self.db(), REPLICA)
        self.login_user(self.add_user("root", is_admin=True))
        self.client.post("/admin/servers/remove", data={"_csrf_token": self.csrf(), "url": REPLICA})
        with self.app.app_context():
            self.assertEqual(server_aliases.load(self.db()), {})


class SignInAtReplicaNameTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user

    def setUp(self):
        self.setUp_base()
        with self.app.app_context():
            server_aliases.assign(get_db(), REPLICA)

    def test_signing_in_at_the_name_goes_through_the_primarys_public_address(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "http://192.168.10.20:8080"
        try:
            response = self.client.get(
                "/auth/via-primary", base_url="http://replica1.webmanager.example",
                headers={"X-Forwarded-Proto": "https"}, environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        finally:
            manager.primary_url = ""
        location = urlsplit(response.headers["Location"])
        self.assertEqual((location.scheme, location.netloc, location.path),
                         ("https", "webmanager.example", "/auth/handoff"))
        self.assertEqual(parse_qs(location.query)["return"], ["https://replica1.webmanager.example"])

    def test_the_primary_vouches_for_its_replica_names_only(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.add_user("alice")
        good = client.get("/auth/handoff", query_string={
            "return": "https://replica1.webmanager.example", "state": "x" * 32})
        self.assertEqual(good.status_code, 302)
        self.assertTrue(good.headers["Location"].startswith("https://replica1.webmanager.example/auth/handoff/complete?token="))
        bad = client.get("/auth/handoff", query_string={
            "return": "https://replica9.webmanager.example", "state": "x" * 32})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
