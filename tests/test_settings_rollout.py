"""Settings saved on the primary for the whole group reach the replicas one
at a time: each saves and restarts before the next is asked, and nothing is
sent if saving failed on the primary."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_app as base
from webmanager import mesh, server_control, settings_rollout

TOKEN = "s3cret-s3cret-s3cret"
R1, R2 = "http://10.0.0.2:8080", "http://10.0.0.3:8080"


class FakeHub:
    def __init__(self):
        self.data = {R1: {}, R2: {}}
        self.sent = []
        self.answers = {}

    def remote_data(self, url):
        return dict(self.data.get(url) or {})

    def post_control(self, url, action, extra=None):
        self.sent.append((url, action, extra))
        return self.answers.get(url, (True, "It is saving the settings and restarting WebManager.", {"ok": True}))

    def entries(self):
        return [{"url": url, "role": "replica"} for url in (R2, R1)] + [{"url": "http://peer", "role": "primary"}]


class Case(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.status_file = Path(self.app.config["PROGRAM_UPDATE_STATUS_FILE"]).with_name("control-status.json")
        self.clock = 1000.0
        patcher = patch.object(settings_rollout.time, "time", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def own_status(self, state, at):
        self.status_file.write_text(json.dumps({"state": state, "action": "settings", "message": "m", "at": at}))

    def advance(self, hub):
        with self.app.app_context():
            settings_rollout.advance(self.app, hub)
            return settings_rollout.summary(self.app)


class RolloutTests(Case):
    def start(self):
        self.own_status("done", "2026-01-01 00:00:00")  # an earlier save
        with self.app.app_context():
            settings_rollout.start(self.app, {"WEBMANAGER_BACKUP_KEEP": "30"}, [R1, R2],
                                   server_control.read_status())

    def test_one_server_at_a_time_after_this_one(self):
        hub = FakeHub()
        self.start()
        # This server hasn't saved yet: nothing is sent.
        self.assertTrue(self.advance(hub)["waiting_here"])
        self.own_status("running", "2026-01-01 00:00:10")
        self.advance(hub)
        self.assertEqual(hub.sent, [])
        # Saved here: the first replica is asked.
        self.own_status("done", "2026-01-01 00:00:20")
        summary = self.advance(hub)
        self.assertEqual(hub.sent, [(R1, "settings", {"changes": {"WEBMANAGER_BACKUP_KEEP": "30"}})])
        self.assertEqual(summary["current"], R1)
        self.assertEqual(summary["queue"], [R2])
        # While it saves and restarts, the next one waits, however long it takes.
        for _ in range(5):
            self.clock += 60
            hub.data[R1] = {"settings_change": {"state": "running", "at": "x1"}}
            self.advance(hub)
        self.assertEqual(len(hub.sent), 1)
        # Done there: then (and only then) the second.
        hub.data[R1] = {"settings_change": {"state": "done", "at": "x2", "message": "Saved."}}
        summary = self.advance(hub)
        self.assertEqual([url for url, _a, _e in hub.sent], [R1, R2])
        self.assertEqual(summary["results"], [{"url": R1, "ok": True, "message": "Saved."}])
        hub.data[R2] = {"settings_change": {"state": "error", "at": "y", "message": "Put back."}}
        self.clock += 40
        summary = self.advance(hub)
        self.assertTrue(summary["finished"])
        self.assertEqual(summary["results"][1], {"url": R2, "ok": False, "message": "Put back."})

    def test_never_two_sends_closer_than_the_minimum(self):
        hub = FakeHub()
        hub.answers[R1] = (True, "Its settings were already like that.", {"ok": True, "unchanged": True})
        self.start()
        self.own_status("done", "2026-01-01 00:00:20")
        self.advance(hub)
        self.advance(hub)  # R1 needed nothing, but R2 still waits the minimum
        self.assertEqual(len(hub.sent), 1)
        self.clock += settings_rollout.MIN_SECONDS_PER_SERVER
        self.advance(hub)
        self.assertEqual(len(hub.sent), 2)

    def test_a_replica_that_never_reports_back_is_skipped_after_the_timeout(self):
        hub = FakeHub()
        self.start()
        self.own_status("done", "2026-01-01 00:00:20")
        self.advance(hub)
        self.clock += settings_rollout.REPLICA_TIMEOUT_SECONDS + 1
        summary = self.advance(hub)
        self.assertFalse(summary["results"][0]["ok"])
        self.assertEqual(summary["current"], R2)

    def test_an_unreachable_replica_is_reported_and_the_rest_continue(self):
        hub = FakeHub()
        hub.answers[R1] = (False, "It could not be reached: no route.", {})
        self.start()
        self.own_status("done", "2026-01-01 00:00:20")
        summary = self.advance(hub)
        self.assertEqual(summary["results"], [{"url": R1, "ok": False, "message": "It could not be reached: no route."}])
        self.clock += settings_rollout.MIN_SECONDS_PER_SERVER
        self.assertEqual(self.advance(hub)["current"], R2)

    def test_nothing_is_sent_when_it_failed_here(self):
        hub = FakeHub()
        self.start()
        self.own_status("error", "2026-01-01 00:00:20")
        summary = self.advance(hub)
        self.assertEqual(hub.sent, [])
        self.assertTrue(summary["finished"])
        self.assertIn("left as they are", summary["aborted"])

    def test_replicas_go_in_replica_number_order(self):
        from webmanager import server_aliases
        from webmanager.db import get_db

        with self.app.app_context():
            server_aliases.assign(get_db(), R1)
            server_aliases.assign(get_db(), R2)
            self.assertEqual(settings_rollout.replicas_in_order(self.app, FakeHub()), [R1, R2])


class SaveForTheGroupTests(Case):
    def setUp(self):
        super().setUp()
        self.app.config["CONTROL_ACTIVE"] = True
        self.login_user(self.add_user("root", "root@example.com", is_admin=True))
        hub = self.app.extensions["mesh_hub"]
        hub.token = TOKEN
        patcher = patch.object(settings_rollout, "replicas_in_order", return_value=[R1, R2])
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **extra):
        data = {"_csrf_token": self.csrf(), "WEBMANAGER_BACKUP_KEEP": "30", "WEBMANAGER_BACKUPS_ENABLED": "1",
                "WEBMANAGER_WATCHDOG_ENABLED": "1", "WEBMANAGER_AUTO_REFRESH_ENABLED": "1", **extra}
        return self.client.post("/admin/settings", data=data, follow_redirects=True).get_data(as_text=True)

    def test_the_page_offers_it_on_a_primary_with_replicas(self):
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        self.assertIn('name="also_replicas"', page)
        self.assertIn("Also the 2 other servers in this group", page)

    def test_ticked_it_plans_the_rollout(self):
        page = self.save(also_replicas="1")
        self.assertIn("one at a time", page)
        with self.app.app_context():
            state = settings_rollout.load(self.app)
        self.assertEqual(state["changes"], {"WEBMANAGER_BACKUP_KEEP": "30"})
        self.assertEqual(sorted(state["queue"]), [R1, R2])
        self.assertIn("Sending the change to the other servers", page)

    def test_unticked_only_this_server(self):
        self.save()
        with self.app.app_context():
            self.assertIsNone(settings_rollout.load(self.app))


class ReceivingTests(Case):
    def setUp(self):
        super().setUp()
        self.app.config["CONTROL_ACTIVE"] = True
        hub = self.app.extensions["mesh_hub"]
        hub.token = TOKEN
        hub._token_hash = mesh._hash_token(TOKEN)
        self.request_file = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).parent / "settings"

    def send(self, changes, token=TOKEN):
        return self.client.post("/mesh/control", json={"action": "settings", "changes": changes},
                                headers={"Authorization": f"Bearer {token}"})

    def test_a_replica_files_the_change_for_its_root_helper(self):
        response = self.send({"WEBMANAGER_BACKUP_KEEP": "30", "WEBMANAGER_BACKUPS_ENABLED": "1"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(self.request_file.read_text()), {"WEBMANAGER_BACKUP_KEEP": "30"})

    def test_nothing_to_change(self):
        response = self.send({"WEBMANAGER_BACKUPS_ENABLED": "1"})
        self.assertTrue(response.get_json()["unchanged"])
        self.assertFalse(self.request_file.exists())

    def test_refused_values_and_settings(self):
        for changes in ({"WEBMANAGER_GOOGLE_ALLOWED_EMAILS": "x@y.co&reboot"},
                        {"WEBMANAGER_REPLICA_OF": "http://evil"}, "not a dict"):
            with self.subTest(changes=changes):
                self.assertEqual(self.send(changes).status_code, 400)
        self.assertFalse(self.request_file.exists())

    def test_its_own_switch_still_wins(self):
        self.app.config["DASHBOARD_SETTINGS"] = False
        response = self.send({"WEBMANAGER_BACKUP_KEEP": "30"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("WEBMANAGER_DASHBOARD_SETTINGS=0", response.get_json()["error"])

    def test_token_only(self):
        self.assertEqual(self.send({"WEBMANAGER_BACKUP_KEEP": "30"}, token="wrong-token-wrong").status_code, 401)

    def test_its_status_reports_the_last_settings_change(self):
        self.status_file.write_text(json.dumps({"state": "done", "action": "settings", "message": "Saved.", "at": "t"}))
        data = self.client.get("/mesh/status", headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
        self.assertEqual(data["settings_change"], {"state": "done", "at": "t", "message": "Saved."})
        self.assertIsNone(data["apps_change"])


if __name__ == "__main__":
    unittest.main()
