"""The System page's WebManager-update panel: why a failed update failed,
retrying it, installing by themselves, and following the updater's source."""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from webmanager import update_status

from tests import test_app as base

OLD, NEW = "a" * 40, "b" * 40


class ProgramUpdatePanelTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.app.config["UPDATER_ACTIVE"] = True
        # Whatever this machine's environment says about where updates come from.
        self.app.config["UPDATE_REPOSITORY"] = ""
        self.app.config["UPDATE_BRANCH"] = ""
        self.addCleanup(setattr, update_status, "_live_status", None)
        update_status._live_status = None
        self.login_user(self.add_user("root", is_admin=True))

    def write_status(self, **values):
        status = {
            "state": "error", "installed_commit": OLD, "available_commit": NEW,
            "update_available": True, "message": "The update failed its tests.",
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        status.update(values)
        Path(self.app.config["PROGRAM_UPDATE_STATUS_FILE"]).write_text(json.dumps(status), encoding="utf-8")

    def page(self):
        return self.client.get("/admin/?section=updates").get_data(as_text=True)

    def post(self, path, **data):
        return self.client.post(path, data={"_csrf_token": self.csrf(), **data})

    # -- why it failed ---------------------------------------------------------
    def test_a_failed_update_shows_what_went_wrong_and_offers_try_again(self):
        self.write_status(detail="FAIL: test_x (tests.test_a.T.test_x)\n---\nFAILED (failures=1)")
        page = self.page()
        self.assertIn("What went wrong", page)
        self.assertIn("FAIL: test_x", page)
        self.assertIn("Try again", page)

    def test_try_again_requests_the_same_commit(self):
        self.write_status()
        response = self.post("/admin/updates/install", commit=NEW)
        self.assertIn(response.status_code, (302, 303))
        request = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"])
        self.assertEqual(request.read_text(encoding="ascii").strip(), NEW)

    def test_try_again_is_refused_for_a_different_commit(self):
        self.write_status()
        self.post("/admin/updates/install", commit="c" * 40)
        self.assertFalse(Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).exists())

    def test_github_still_having_the_failed_version_does_not_hide_the_failure(self):
        self.write_status(detail="FAIL: test_x")
        update_status._live_status = {
            "state": "available", "installed_commit": OLD, "available_commit": NEW,
            "update_available": True, "message": "An update is available.",
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self.app.app_context():
            status = update_status.read_update_status()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["detail"], "FAIL: test_x")

    def test_a_newer_version_on_github_replaces_the_failure(self):
        self.write_status()
        update_status._live_status = {
            "state": "available", "installed_commit": OLD, "available_commit": "c" * 40,
            "update_available": True, "message": "An update is available.",
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self.app.app_context():
            self.assertEqual(update_status.read_update_status()["state"], "available")

    # -- installing by themselves -------------------------------------------------
    def test_automatic_installation_is_a_switch_only_a_super_admin_can_flip(self):
        marker = update_status.Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).with_name("auto-install")
        self.assertFalse(marker.exists())
        self.assertIn("Turn on automatic installation", self.page())

        self.post("/admin/updates/auto", enabled="1")
        self.assertTrue(marker.exists())
        self.assertTrue(Path(self.app.config["PROGRAM_UPDATE_CHECK_REQUEST_FILE"]).exists(),
                        "the updater is asked to look straight away")
        self.assertIn("Turn off automatic installation", self.page())

        self.post("/admin/updates/auto", enabled="0")
        self.assertFalse(marker.exists())

        self.login_user(self.add_user("alice"))
        self.assertEqual(self.post("/admin/updates/auto", enabled="1").status_code, 403)
        self.assertFalse(marker.exists())

    def test_it_cannot_be_switched_on_while_the_updater_service_is_off(self):
        self.app.config["UPDATER_ACTIVE"] = False
        self.post("/admin/updates/auto", enabled="1")
        marker = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).with_name("auto-install")
        self.assertFalse(marker.exists())

    # -- one source of truth -----------------------------------------------------------
    def test_the_app_checks_the_repository_and_branch_the_updater_follows(self):
        self.write_status(
            state="current", repository="https://github.com/someone/fork.git", branch="release",
        )
        with self.app.app_context():
            self.assertEqual(
                update_status.update_source(),
                ("https://github.com/someone/fork.git", "release"),
            )

    def test_an_explicit_setting_beats_the_updater_record(self):
        self.write_status(state="current", repository="https://github.com/someone/fork.git", branch="release")
        self.app.config["UPDATE_REPOSITORY"] = "https://github.com/me/mine.git"
        self.app.config["UPDATE_BRANCH"] = "dev"
        with self.app.app_context():
            self.assertEqual(update_status.update_source(), ("https://github.com/me/mine.git", "dev"))

    def test_a_recorded_source_that_is_not_a_github_url_is_ignored(self):
        self.write_status(state="current", repository="https://evil.example/x.git", branch="main")
        with self.app.app_context():
            self.assertNotIn("evil.example", update_status.update_source()[0])


if __name__ == "__main__":
    unittest.main()
