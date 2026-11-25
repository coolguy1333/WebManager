"""Turning app hosting on/off from the dashboard, and changing another
server's settings from this one's Servers list."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from webmanager import mesh, peer_http, server_control

from tests import test_app as base
from tests.test_server_updates import PEER, TOKEN, _Answer


class AppHostingSwitchTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.app.config["CONTROL_ACTIVE"] = True
        self.login_user(self.add_user("root", is_admin=True))
        with self.app.app_context():
            self.request_file = server_control.apps_request_file()
            self.status_file = server_control.status_file()

    def page(self):
        return self.client.get("/admin/?section=updates").get_data(as_text=True)

    def switch(self, enabled):
        return self.client.post(
            "/admin/apps/hosting", data={"_csrf_token": self.csrf(), "enabled": "1" if enabled else "0"},
            follow_redirects=True,
        ).get_data(as_text=True)

    def test_the_system_page_offers_to_turn_it_on(self):
        page = self.page()
        self.assertIn('id="apps"', page)
        self.assertIn("Turn app hosting on", page)

    def test_turning_it_on_asks_the_root_helper(self):
        page = self.switch(True)
        self.assertIn("Turning app hosting on", page)
        self.assertEqual(self.request_file.read_text(encoding="ascii").strip(), "on")
        page = self.page()
        self.assertIn("Waiting for the server to pick up the request", page)
        self.assertIn('data-auto-refresh="5"', page, "the page follows the change by itself")
        self.assertNotIn("Turn app hosting on</button>", page, "no second request while one is under way")

    def test_turning_it_off_when_it_is_on(self):
        self.app.config["APPS_ENABLED"] = True
        self.assertIn("Turn app hosting off", self.page())
        self.switch(False)
        self.assertEqual(self.request_file.read_text(encoding="ascii").strip(), "off")

    def test_nothing_to_do_when_it_is_already_that_way(self):
        self.assertIn("already off", self.switch(False))
        self.assertFalse(self.request_file.exists())

    def test_the_server_can_keep_it_a_shell_only_decision(self):
        self.app.config["DASHBOARD_APP_CONTROL"] = False
        self.assertIn("WEBMANAGER_DASHBOARD_APP_CONTROL=0", self.page())
        self.assertIn("WEBMANAGER_DASHBOARD_APP_CONTROL=0", self.switch(True))
        self.assertFalse(self.request_file.exists())

    def test_it_says_so_when_the_root_helper_is_not_running(self):
        self.app.config["CONTROL_ACTIVE"] = False
        self.assertIn("webmanager-control.path", self.switch(True))
        self.assertFalse(self.request_file.exists())

    def test_progress_and_failures_from_the_root_helper_are_shown(self):
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(json.dumps({
            "state": "running", "action": "apps_on", "message": "Installing Docker.", "at": "2026-11-25 10:00:00",
        }), encoding="utf-8")
        page = self.page()
        self.assertIn("Turning app hosting on…", page)
        self.assertIn("Installing Docker.", page)

        self.status_file.write_text(json.dumps({
            "state": "error", "action": "apps_on", "message": "Turning app hosting on failed.",
            "detail": "E: Unable to locate package docker.io",
        }), encoding="utf-8")
        page = self.page()
        self.assertIn("The last change didn't work", page)
        self.assertIn("Unable to locate package docker.io", page)
        self.assertIn("Turn app hosting on", page, "it can be tried again")

    def test_only_super_admins(self):
        self.login_user(self.add_user("alice"))
        response = self.client.post("/admin/apps/hosting", data={"_csrf_token": self.csrf(), "enabled": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.request_file.exists())

    def test_a_replica_switches_its_own_app_hosting(self):
        from webmanager.replication import _LOCAL_ONLY_ENDPOINTS

        for endpoint in ("admin.set_app_hosting", "admin.control_server", "admin.acknowledge_failover"):
            self.assertIn(endpoint, _LOCAL_ONLY_ENDPOINTS)

    def test_the_root_helper_only_takes_one_plain_word(self):
        helper = (Path(base.__file__).resolve().parent.parent / "deploy" / "debian" / "control.sh").read_text()
        self.assertIn("-L $REQUEST_FILE", helper, "never follows a link")
        self.assertIn("on) ACTION=apps_on", helper)
        self.assertIn("off) ACTION=apps_off", helper)
        self.assertIn("WEBMANAGER_DASHBOARD_APP_CONTROL", helper)


class ControlledByAnotherServerTests(unittest.TestCase):
    """POST /mesh/control on the server being changed."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def setUp(self):
        self.setUp_base()
        self.app.config["CONTROL_ACTIVE"] = True
        self.app.config["UPDATER_ACTIVE"] = True
        self.app.extensions["mesh_hub"].set_token(TOKEN)
        with self.app.app_context():
            self.request_file = server_control.apps_request_file()

    def ask(self, action, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/mesh/control", json={"action": action}, headers=headers)

    def test_it_needs_the_shared_token_and_a_known_action(self):
        self.assertEqual(self.ask("apps_on", token=None).status_code, 401)
        self.assertEqual(self.ask("apps_on", token="wrong-wrong-wrong-wrong").status_code, 401)
        self.assertEqual(self.ask("rm -rf /").status_code, 400)
        self.assertFalse(self.request_file.exists())

    def test_app_hosting(self):
        response = self.ask("apps_on")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.request_file.read_text(encoding="ascii").strip(), "on")
        self.assertEqual(self.ask("apps_on").status_code, 409, "one change at a time")
        self.request_file.unlink()
        self.assertIn("already off", self.ask("apps_off").get_json()["message"])
        status = self.client.get("/mesh/status", headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
        self.assertIsNone(status["apps_change"])

    def test_automatic_updates(self):
        with self.app.app_context():
            from webmanager.update_status import auto_install_file

            auto_file = auto_install_file()
        self.assertTrue(self.ask("auto_updates_on").get_json()["ok"])
        self.assertTrue(auto_file.exists())
        self.assertTrue(self.client.get("/mesh/status").status_code in (200, 401))
        status = self.client.get("/mesh/status", headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
        self.assertTrue(status["auto_install"])
        self.ask("auto_updates_off")
        self.assertFalse(auto_file.exists())

    def test_sync_now_is_for_replicas(self):
        self.assertEqual(self.ask("sync_now").status_code, 409)
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = "https://primary.example"
        self.addCleanup(setattr, manager, "primary_url", "")
        data_manager = self.app.extensions["data_replication_manager"]
        with patch.object(manager, "sync_once", return_value=True), \
                patch.object(data_manager, "sync_once", return_value=True) as data_sync:
            response = self.ask("sync_now")
        self.assertTrue(response.get_json()["ok"])
        data_sync.assert_called()


class ManagePanelTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.hub = self.app.extensions["mesh_hub"]
        self.hub.set_token(TOKEN)
        self.hub.urls.append(PEER)
        self.addCleanup(self.hub.urls.remove, PEER)
        self.login_user(self.add_user("root", is_admin=True))

    def peer_reports(self, **data):
        with self.hub._lock:
            self.hub._remote[PEER] = {
                "reachable": True, "error": None, "hint": None, "checked_at": 1, "last_ok_at": 1,
                "data": {"hostname": "replica-1", "sites": {"total": 1, "running": 1}, **data},
            }

    def panel(self):
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        start = page.index('class="mesh-manage"')
        return page[start:page.index("</details>", start)]

    def test_a_replica_with_apps_off_can_have_them_turned_on(self):
        self.peer_reports(role="replica", apps_enabled=False, auto_install=False)
        panel = self.panel()
        self.assertIn('value="apps_on"', panel)
        self.assertIn("copy app data", panel)
        self.assertIn('value="auto_updates_on"', panel)
        self.assertIn('value="sync_now"', panel)
        self.assertIn(f"{PEER}/admin/?section=updates", panel)

    def test_a_change_under_way_or_failed_is_shown(self):
        self.peer_reports(role="replica", apps_enabled=False, apps_change="running")
        panel = self.panel()
        self.assertIn("Changing…", panel)
        self.assertNotIn('value="apps_on"', panel)
        self.peer_reports(role="replica", apps_enabled=False, apps_change="error",
                          apps_change_message="Docker could not be installed.")
        self.assertIn("Docker could not be installed.", self.panel())

    def test_an_independent_server_has_no_sync_button(self):
        self.peer_reports(role="primary", apps_enabled=True, auto_install=True)
        panel = self.panel()
        self.assertIn('value="apps_off"', panel)
        self.assertIn('value="auto_updates_off"', panel)
        self.assertNotIn("sync_now", panel)

    def test_pressing_a_button_asks_that_server(self):
        with patch.object(self.hub, "control", return_value=(True, "It is turning app hosting on.")) as control:
            response = self.client.post(
                "/admin/servers/control",
                data={"_csrf_token": self.csrf(), "url": PEER, "action": "apps_on"},
                follow_redirects=True,
            )
        control.assert_called_once_with(PEER, "apps_on")
        self.assertIn("It is turning app hosting on.", response.get_data(as_text=True))

        with patch.object(self.hub, "control") as control:
            self.client.post("/admin/servers/control", data={"_csrf_token": self.csrf(), "url": PEER, "action": "reboot"})
            self.client.post("/admin/servers/control",
                             data={"_csrf_token": self.csrf(), "url": "http://other.example:8080", "action": "apps_on"})
        control.assert_not_called()

    def test_the_hub_sends_the_action(self):
        answer = _Answer({"ok": True, "message": "done"}, 202)
        with patch.object(peer_http, "open_peer", return_value=answer) as opener, patch.object(self.hub, "_poll"):
            self.assertEqual(self.hub.control(PEER, "apps_on"), (True, "done"))
        sent = opener.call_args[0][0]
        self.assertEqual(sent.full_url, f"{PEER}/mesh/control")
        self.assertEqual(json.loads(sent.data), {"action": "apps_on"})
        self.assertIn("apps_on", mesh.CONTROL_ACTIONS)


if __name__ == "__main__":
    unittest.main()
