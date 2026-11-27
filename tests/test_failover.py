"""The large "failed over" warning: when it shows, what it says, hiding it
once checked, and the notification that goes with it."""

import json
import os
import time
import unittest
from unittest.mock import patch

from webmanager import failover, replication
from webmanager.watchdog import Watchdog

from tests import test_app as base

PRIMARY = "http://10.0.0.1:8080"
OTHER = "http://10.0.0.2:8080"


class FailoverWarningTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.admin_id = self.add_user("root", is_admin=True)
        self.login_user(self.admin_id)

    def page(self, client=None):
        return (client or self.client).get("/").get_data(as_text=True)

    def promote(self, **details):
        failover.write_promoted_marker(self.app, PRIMARY, "system page", **details)

    def test_no_warning_normally(self):
        self.assertNotIn("failover-banner", self.page())

    def test_a_server_that_took_over_says_so_on_every_page(self):
        synced = time.time() - 120
        self.promote(config_synced_at=synced, data_synced_at=synced)
        for path in ("/", "/?view=sources", "/admin/?section=updates", "/docs"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("Failed over: this server is now the primary", page, path)
        page = self.page()
        self.assertIn(PRIMARY, page)
        self.assertIn("from the System page", page)
        self.assertIn("Its copy of the settings is from", page)
        self.assertIn("Point your domain", page)
        self.assertIn("I've checked everything", page)

    def test_people_who_are_not_super_admins_do_not_see_it(self):
        self.promote()
        other = self.app.test_client()
        with other.session_transaction() as session:
            session["user_id"] = self.add_user("alice")
        page = self.page(other)
        self.assertNotIn("failover-banner", page)
        self.assertNotIn("Failed over", page)

    def test_hiding_it_once_checked_and_it_returns_on_the_next_takeover(self):
        self.promote()
        response = self.client.post(
            "/admin/failover/acknowledge", data={"_csrf_token": self.csrf()}, follow_redirects=True
        )
        self.assertIn("The failover warning is hidden", response.get_data(as_text=True))
        self.assertNotIn("failover-banner", self.page())

        later = time.time() + 5
        self.promote()
        os.utime(replication.promoted_marker(self.app), (later, later))
        self.assertIn("Failed over", self.page())

    def test_only_super_admins_can_hide_it(self):
        self.promote()
        self.login_user(self.add_user("alice"))
        response = self.client.post("/admin/failover/acknowledge", data={"_csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(failover.acknowledged_marker(self.app).exists())

    def test_a_marker_written_by_an_older_version_still_works(self):
        marker = replication.promoted_marker(self.app)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"Promoted from replica of {PRIMARY}\n", encoding="utf-8")
        details = failover.promotion(self.app)
        self.assertEqual(details["from"], PRIMARY)
        self.assertFalse(details["acknowledged"])
        self.assertIn(PRIMARY, self.page())

    def test_promoting_from_the_system_page_records_the_takeover(self):
        manager = self.app.extensions["replication_manager"]
        data_manager = self.app.extensions["data_replication_manager"]
        for each in (manager, data_manager):
            each.primary_url, each.token = PRIMARY, "s3cret-s3cret-s3cret"
        manager.last_sync_at = data_manager.last_sync_at = time.time() - 30
        response = self.client.post(
            "/admin/replication/promote", data={"_csrf_token": self.csrf()}, follow_redirects=True
        )
        page = response.get_data(as_text=True)
        self.assertIn("Failed over: this server is now the primary", page)
        recorded = json.loads(replication.promoted_marker(self.app).read_text(encoding="utf-8"))
        self.assertEqual(recorded["from"], PRIMARY)
        self.assertEqual(recorded["by"], "system page")
        self.assertIsInstance(recorded["config_synced_at"], int)

    def test_a_replica_whose_primary_stopped_answering(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = PRIMARY
        self.addCleanup(setattr, manager, "primary_url", "")
        manager.last_sync_at = time.time() - 20
        manager.last_error = "Could not reach primary: Connection refused."
        self.assertNotIn("failover-banner", self.page(), "a short blip is not an outage")

        manager.last_sync_at = time.time() - failover.PRIMARY_DOWN_AFTER_SECONDS - 5
        page = self.page()
        self.assertIn("The primary server isn't answering", page)
        self.assertIn("Connection refused", page)
        self.assertIn("Promote to primary", page)

        manager.last_error = None
        self.assertNotIn("failover-banner", self.page())

    def test_two_servers_acting_as_the_primary(self):
        hub = self.app.extensions["mesh_hub"]
        hub.urls.append(OTHER)
        self.addCleanup(hub.urls.remove, OTHER)
        with hub._lock:
            hub._remote[OTHER] = {
                "reachable": True, "checked_at": 1, "last_ok_at": 1,
                "data": {"hostname": "server-b", "role": "primary", "promoted_at": None},
            }
        self.assertNotIn("failover-banner", self.page(), "independent servers are each their own primary")

        with hub._lock:
            hub._remote[OTHER]["data"]["promoted_at"] = int(time.time())
        page = self.page()
        self.assertIn("Two servers are acting as the primary", page)
        self.assertIn("server-b took over more recently", page)


class FailoverNotificationTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def setUp(self):
        self.setUp_base()
        self.watchdog = Watchdog(self.app)

    def test_one_notification_per_situation(self):
        with patch.object(self.watchdog, "_notify") as notify:
            self.watchdog._watch_failover()
            notify.assert_not_called()

            failover.write_promoted_marker(self.app, PRIMARY, "webmanager-promote")
            self.watchdog._watch_failover()
            self.watchdog._watch_failover()
            self.assertEqual(notify.call_count, 1)
            self.assertIn("Failed over", notify.call_args[0][0])

    def test_promoting_from_the_system_page_notifies_at_once(self):
        manager = self.app.extensions["replication_manager"]
        data_manager = self.app.extensions["data_replication_manager"]
        for each in (manager, data_manager):
            each.primary_url, each.token = PRIMARY, "s3cret-s3cret-s3cret"
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = base.WebManagerTestCase.add_user(self, "root", is_admin=True)
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
        if token is None:
            client.get("/")
            with client.session_transaction() as session:
                token = session["_csrf_token"]
        with patch.object(self.app.extensions["watchdog"], "_notify") as notify:
            client.post("/admin/replication/promote", data={"_csrf_token": token})
        self.assertIn("Failed over", notify.call_args[0][0])

    def test_the_primary_coming_back_is_announced(self):
        manager = self.app.extensions["replication_manager"]
        manager.primary_url = PRIMARY
        self.addCleanup(setattr, manager, "primary_url", "")
        manager.last_sync_at = time.time() - 600
        manager.last_error = "Connection refused."
        with patch.object(self.watchdog, "_notify") as notify:
            self.watchdog._watch_failover()
            self.assertIn("isn't answering", notify.call_args[0][0])
            manager.last_error, manager.last_sync_at = None, time.time()
            self.watchdog._watch_failover()
            self.assertIn("answering again", notify.call_args[0][0])
            self.assertEqual(notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
