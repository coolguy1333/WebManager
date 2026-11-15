"""Backups, the activity log, notifications, deploy hooks and the watchdog."""

import json
import os
import sqlite3
import stat
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webmanager import activity, backups, hooks, notifications, watchdog
from webmanager.db import get_db, get_setting

from tests import test_app as base


class Fixture(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository
    add_site = base.WebManagerTestCase.add_site

    def setUp(self):
        self.setUp_base()
        self.root = Path(self.temp_directory.name)
        self.app.config["BACKUP_DIR"] = str(self.root / "backups")
        self.app.config["SETTINGS_FILE"] = str(self.root / "webmanager.env")
        self.app.config["WATCHDOG_STATE_FILE"] = str(self.root / "watchdog-state.json")
        self.admin_id = self.add_user("root", "root@example.com", is_admin=True)
        self.login_user(self.admin_id)

    def post(self, path, **data):
        return self.client.post(path, data={"_csrf_token": self.csrf(), **data})

    def entries(self):
        with self.app.app_context():
            return [dict(row) for row in get_db().execute("SELECT * FROM activity ORDER BY id")]

    def site(self, status="running"):
        folder = self.root / "repositories" / "1" / "1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text("hi", encoding="utf-8")
        repository_id = self.add_repository(self.admin_id, folder)
        return repository_id, self.add_site(
            self.admin_id, repository_id, folder, status=status, runtime_backend="nginx"
        )


# ---------------------------------------------------------------------------
class ActivityTests(Fixture):
    def test_changes_are_logged_with_who_what_and_the_outcome(self):
        self.post("/admin/limits", max_sites="5", max_sources="3")
        self.post("/admin/domains", name="not a domain")

        saved, refused = self.entries()[-2:]
        self.assertEqual(saved["actor"], "root@example.com")
        self.assertEqual(saved["user_id"], self.admin_id)
        self.assertIn("Default limits saved", saved["detail"])
        self.assertEqual(saved["ok"], 1)
        self.assertEqual(refused["ok"], 0)
        self.assertIn("valid domain", refused["detail"])

    def test_reading_pages_and_signed_out_requests_are_not_logged(self):
        self.client.get("/")
        self.client.get("/admin/?section=updates")
        with self.client.session_transaction() as session:
            session.clear()
        self.client.post("/admin/limits", data={"max_sites": "1"})
        self.assertEqual(self.entries(), [])

    def test_known_actions_get_plain_names_and_unknown_ones_a_readable_fallback(self):
        self.assertEqual(activity._label("deployments.stop_site"), "Stopped a site")
        self.assertEqual(activity._label("admin.some_new_thing"), "Some new thing")

    def test_every_action_in_the_dashboard_has_a_plain_name(self):
        endpoints = {
            rule.endpoint for rule in self.app.url_map.iter_rules()
            if "POST" in rule.methods and rule.endpoint.split(".")[0] in {"deployments", "admin", "auth"}
        }
        self.assertEqual(sorted(endpoints - set(activity.LABELS)), [])
        self.assertEqual(sorted(set(activity.LABELS) - endpoints), [], "labels for actions that no longer exist")

    def test_only_admins_can_read_it_and_it_can_be_searched(self):
        with self.app.app_context():
            activity.record(get_db(), "Stopped a site", "Docs", user=None)
            activity.record(get_db(), "Made the daily backup", "webmanager-x.tar.gz", user=None)
        page = self.client.get("/admin/activity").get_data(as_text=True)
        self.assertIn("Stopped a site", page)
        self.assertIn("Made the daily backup", page)
        found = self.client.get("/admin/activity?q=backup").get_data(as_text=True)
        self.assertIn("Made the daily backup", found)
        self.assertNotIn("Stopped a site", found)

        self.login_user(self.add_user("alice"))
        self.assertEqual(self.client.get("/admin/activity").status_code, 403)

    def test_the_log_does_not_grow_forever(self):
        with self.app.app_context(), patch.object(activity, "KEEP_ROWS", 5):
            for index in range(12):
                activity.record(get_db(), f"Thing {index}")
            count = get_db().execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        self.assertEqual(count, 5)

    def test_what_webmanager_does_itself_is_recorded_too(self):
        activity.record_system(self.app, "Restarted the site web server", "It had stopped.", ok=True)
        entry = self.entries()[-1]
        self.assertIsNone(entry["user_id"])
        self.assertEqual(entry["actor"], "WebManager")


# ---------------------------------------------------------------------------
class BackupTests(Fixture):
    def test_a_backup_holds_the_database_keys_and_settings_and_is_private(self):
        (Path(self.app.instance_path) / "secret.key").write_text("k" * 64, encoding="utf-8")
        Path(self.app.config["SETTINGS_FILE"]).write_text("WEBMANAGER_PORT=5000\n", encoding="utf-8")

        path = backups.create_backup(self.app, "manual")

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with tarfile.open(path) as archive:
            names = set(archive.getnames())
            manifest = json.load(archive.extractfile("manifest.json"))
        self.assertLessEqual({"webmanager.sqlite3", "manifest.json", "settings.env"}, names)
        self.assertEqual(manifest["data_dir"], self.app.instance_path)
        self.assertEqual([item["name"] for item in backups.list_backups(self.app)], [path.name])

    def test_only_the_newest_are_kept(self):
        self.app.config["BACKUP_KEEP"] = 2
        directory = Path(self.app.config["BACKUP_DIR"])
        directory.mkdir()
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            (directory / f"webmanager-{stamp}-daily.tar.gz").write_bytes(b"x")
        backups.create_backup(self.app, "manual")
        names = [item["name"] for item in backups.list_backups(self.app)]
        self.assertEqual(len(names), 2)
        self.assertNotIn("webmanager-20260101-000000-daily.tar.gz", names)

    def test_the_schedule_makes_one_a_day(self):
        scheduler = backups.BackupScheduler(self.app)
        self.assertTrue(scheduler.run_once())
        self.assertFalse(scheduler.run_once(), "one already exists from today")
        newest = Path(self.app.config["BACKUP_DIR"]) / backups.list_backups(self.app)[0]["name"]
        old = time.time() - 25 * 3600
        os.utime(newest, (old, old))
        time.sleep(1.1)  # a new second, so the new file gets a new name
        self.assertTrue(scheduler.run_once())
        self.assertIn("Made the daily backup", [entry["action"] for entry in self.entries()])

    def test_a_failed_scheduled_backup_is_recorded_and_announced(self):
        sent = []
        self.app.extensions["notifier"].send = lambda *args, **kwargs: sent.append(args)
        with patch.object(backups, "create_backup", side_effect=backups.BackupError("disk full")):
            self.assertFalse(backups.BackupScheduler(self.app).run_once())
        self.assertEqual(self.entries()[-1]["ok"], 0)
        self.assertEqual(sent[0][0], "Backup failed")

    def test_admins_can_make_and_download_one_and_nobody_else_can(self):
        self.post("/admin/backups")
        name = backups.list_backups(self.app)[0]["name"]
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        self.assertIn(name, page)
        response = self.client.get(f"/admin/backups/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[:2], b"\x1f\x8b")
        self.assertIn("Downloaded a backup", [entry["action"] for entry in self.entries()])
        for bad in ("../secret.key", "webmanager.sqlite3", "webmanager-20260101-000000.tar.gz"):
            self.assertEqual(self.client.get(f"/admin/backups/{bad}").status_code, 404, bad)

        self.login_user(self.add_user("alice"))
        self.assertEqual(self.client.get(f"/admin/backups/{name}").status_code, 403)
        self.assertEqual(self.post("/admin/backups").status_code, 403)

    def test_restore_puts_the_database_back_and_keeps_the_one_it_replaced(self):
        self.add_user("before-backup")
        path = backups.create_backup(self.app, "manual")
        self.add_user("after-backup")
        data_dir = Path(self.app.instance_path)
        database_path = Path(self.app.config["DATABASE"])

        outcome = backups.restore_backup(path, data_dir, database_path)

        names = {row[0] for row in sqlite3.connect(database_path).execute("SELECT username FROM users")}
        self.assertIn("before-backup", names)
        self.assertNotIn("after-backup", names)
        kept = {row[0] for row in sqlite3.connect(outcome["previous"]).execute("SELECT username FROM users")}
        self.assertIn("after-backup", kept)

    def test_restore_on_another_machine_points_paths_at_the_new_data_folder(self):
        _repository_id, _site_id = self.site()
        path = backups.create_backup(self.app, "manual")
        with tempfile.TemporaryDirectory() as elsewhere:
            new_data = Path(elsewhere) / "data"
            backups.restore_backup(path, new_data, new_data / "webmanager.sqlite3")
            row = sqlite3.connect(new_data / "webmanager.sqlite3").execute(
                "SELECT document_root FROM sites"
            ).fetchone()
        self.assertTrue(row[0].startswith(str(new_data)) or self.app.instance_path not in row[0], row[0])

    def test_restore_refuses_junk_and_changes_nothing(self):
        database_path = Path(self.app.config["DATABASE"])
        before = database_path.read_bytes()
        junk = self.root / "webmanager-20260101-000000.tar.gz"
        junk.write_bytes(b"not a tarball")
        with self.assertRaises(backups.BackupError):
            backups.restore_backup(junk, Path(self.app.instance_path), database_path)
        with tarfile.open(junk, "w:gz") as archive:
            evil = self.root / "evil"
            evil.write_text("x")
            archive.add(evil, arcname="../../etc/passwd")
        with self.assertRaises(backups.BackupError):
            backups.restore_backup(junk, Path(self.app.instance_path), database_path)
        self.assertEqual(database_path.read_bytes(), before)

    def test_sources_whose_files_are_gone_are_cloned_again(self):
        repository_id, _site_id = self.site()
        gone = self.root / "repositories" / "9" / "9"
        with self.app.app_context():
            get_db().execute("UPDATE repositories SET local_path = ? WHERE id = ?", (str(gone), repository_id))
            get_db().commit()

        def fake_clone(url, target, branch=None, **_kwargs):
            target.mkdir(parents=True)

        with patch("webmanager.git_service.clone_repository", side_effect=fake_clone) as clone, \
                patch("webmanager.git_service.repository_commit", return_value="a" * 40), \
                patch.object(self.app.extensions["runtime_manager"], "restore_sites") as restore:
            self.assertEqual(backups.reclone_missing_sources(self.app), 1)
        clone.assert_called_once()
        restore.assert_called_once()
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT current_commit FROM repositories").fetchone()[0], "a" * 40
            )


# ---------------------------------------------------------------------------
class NotificationTests(Fixture):
    def test_each_service_gets_the_shape_it_expects(self):
        discord = notifications.build_payload("https://discord.com/api/webhooks/1/x", "Docs is down", "no answer", "error", "srv")
        self.assertEqual(list(discord), ["content"])
        self.assertIn("Docs is down", discord["content"])
        slack = notifications.build_payload("https://hooks.slack.com/services/T/B/x", "T", "b", "ok", "srv")
        self.assertEqual(list(slack), ["text"])
        generic = notifications.build_payload("https://example.com/hook", "T", "b", "info", "srv")
        self.assertEqual((generic["title"], generic["text"], generic["level"], generic["server"]), ("T", "b", "info", "srv"))

    def test_addresses_are_checked(self):
        self.assertEqual(notifications.validate_url("  "), "")
        self.assertEqual(notifications.validate_url(" https://example.com/h "), "https://example.com/h")
        for bad in ("ftp://example.com/x", "example.com/hook", "https://user:pw@example.com/x", "javascript:alert(1)"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                notifications.validate_url(bad)

    def test_saving_testing_and_turning_off(self):
        self.post("/admin/notifications", webhook_url="https://example.com/hook")
        with self.app.app_context():
            self.assertEqual(get_setting(get_db(), notifications.SETTING), "https://example.com/hook")
        with patch.object(notifications, "deliver", return_value=(True, "Delivered (HTTP 204).")) as deliver:
            response = self.post("/admin/notifications/test")
        deliver.assert_called_once()
        self.assertIn(b"Test sent", self.client.get(response.headers["Location"]).data)

        self.post("/admin/notifications", webhook_url="nope")
        with self.app.app_context():
            self.assertEqual(get_setting(get_db(), notifications.SETTING), "https://example.com/hook")
        self.post("/admin/notifications", webhook_url="")
        with self.app.app_context():
            self.assertIsNone(get_setting(get_db(), notifications.SETTING))

    def test_only_admins_can_change_them(self):
        self.login_user(self.add_user("alice"))
        self.assertEqual(self.post("/admin/notifications", webhook_url="https://example.com/h").status_code, 403)
        self.assertEqual(self.post("/admin/notifications/test").status_code, 403)

    def test_nothing_is_sent_when_no_address_is_set(self):
        with patch.object(notifications, "deliver") as deliver:
            self.app.extensions["notifier"].send("T", "b")
        deliver.assert_not_called()

    def test_a_failing_webhook_is_reported_not_raised(self):
        delivered, outcome = notifications.deliver("http://127.0.0.1:9/hook", "T", "b")
        self.assertFalse(delivered)
        self.assertIn("Could not reach", outcome)


# ---------------------------------------------------------------------------
class DeployHookTests(Fixture):
    def token(self, repository_id):
        with self.app.app_context():
            return get_db().execute("SELECT hook_token FROM repositories WHERE id = ?", (repository_id,)).fetchone()[0]

    def test_create_call_replace_and_remove(self):
        repository_id, _ = self.site()
        self.assertIsNone(self.token(repository_id))
        self.post(f"/repositories/{repository_id}/hook", action="create")
        token = self.token(repository_id)
        self.assertRegex(token, r"^[0-9a-f]{40}$")
        self.assertIn(f"/hooks/deploy/{token}", self.client.get("/?view=sources").get_data(as_text=True))

        hooks._recent.clear()
        manager = self.app.extensions["repository_refresh_manager"]
        result = type("R", (), {"status": "current", "message": "Already up to date."})()
        with self.client.session_transaction() as session:
            session.clear()  # the Git host calling it is not signed in
        with patch.object(manager, "refresh", return_value=result) as refresh:
            first = self.client.post(f"/hooks/deploy/{token}", json={"ref": "refs/heads/main"})
            second = self.client.post(f"/hooks/deploy/{token}")
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        refresh.assert_called_once_with(repository_id)  # the second call is within the cooldown
        self.assertIn("Deploy hook checked a source", [entry["action"] for entry in self.entries()])

        self.login_user(self.admin_id)
        self.post(f"/repositories/{repository_id}/hook", action="create")
        self.assertNotEqual(self.token(repository_id), token)
        self.assertEqual(self.client.post(f"/hooks/deploy/{token}").status_code, 404)
        self.post(f"/repositories/{repository_id}/hook", action="remove")
        self.assertIsNone(self.token(repository_id))

    def test_unknown_or_malformed_addresses_are_404(self):
        for token in ("0" * 40, "short", "g" * 40):
            self.assertEqual(self.client.post(f"/hooks/deploy/{token}").status_code, 404)
        self.assertEqual(self.client.get("/hooks/deploy/" + "0" * 40).status_code, 405)

    def test_someone_who_cannot_manage_the_source_cannot_touch_its_hook(self):
        repository_id, _ = self.site()
        self.login_user(self.add_user("alice"))
        self.assertEqual(self.post(f"/repositories/{repository_id}/hook", action="create").status_code, 404)
        self.assertIsNone(self.token(repository_id))


# ---------------------------------------------------------------------------
class WatchdogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.app.extensions["notifier"].send = lambda title, text, level="info": self.sent.append((title, level))
        self.dog = watchdog.Watchdog(self.app)
        self.runtime = self.app.extensions["runtime_manager"]
        patcher = patch.object(type(self.runtime), "nginx_binary", new="/usr/sbin/nginx")
        patcher.start()
        self.addCleanup(patcher.stop)

    def round(self, code, gateway=True):
        with patch.object(watchdog, "_port_open", return_value=gateway), \
                patch.object(watchdog.health, "probe_site", return_value=code), \
                patch.object(watchdog.shutil, "disk_usage", return_value=type("U", (), {"total": 100, "free": 50})()):
            self.dog.run_once()

    def test_a_site_is_down_after_two_misses_and_announced_once_then_recovers(self):
        _, site_id = self.site()
        self.round(200)
        self.round(None)
        self.assertEqual(self.sent, [], "one miss is not an outage")
        self.round(502)
        self.round(None)
        self.assertEqual(self.sent, [("Demo is down", "error")])
        self.round(200)
        self.assertEqual(self.sent[-1], ("Demo is back", "ok"))
        actions = [entry["action"] for entry in self.entries()]
        self.assertIn("Site stopped answering", actions)
        self.assertIn("Site is answering again", actions)

        with self.app.app_context():
            summary = watchdog.uptime_summary(get_db(), site_id)
        self.assertEqual((summary["checks"], summary["failures"]), (5, 3))
        self.assertEqual(summary["percent"], 40.0)

    def test_a_dead_web_server_is_started_again(self):
        self.site()
        with patch.object(self.runtime, "restore_sites") as restore, patch.object(self.runtime, "restore_gateway") as gateway, \
                patch.object(watchdog.time, "sleep"):
            self.round(200, gateway=False)
        restore.assert_called_once()
        gateway.assert_called_once()
        self.assertEqual(self.entries()[-1]["action"], "Restarted the site web server")
        self.assertTrue(self.sent)

    def test_an_app_that_stops_answering_is_started_again(self):
        _, site_id = self.site()
        with self.app.app_context():
            get_db().execute("UPDATE sites SET kind = 'app' WHERE id = ?", (site_id,))
            get_db().commit()
        with patch.object(self.runtime, "start_app_async") as start:
            self.round(None)
            self.round(None)
            self.round(None)
        start.assert_called_once_with(site_id)

    def test_stopped_sites_are_not_watched(self):
        self.site(status="stopped")
        self.round(None)
        self.round(None)
        self.assertEqual(self.sent, [])

    def test_source_news_is_announced_once_and_not_after_a_restart(self):
        repository_id, _ = self.site()
        self.round(200)  # learns the current state quietly
        with self.app.app_context():
            get_db().execute("UPDATE repositories SET pending_commit = ? WHERE id = ?", ("c" * 40, repository_id))
            get_db().commit()
        self.round(200)
        self.round(200)
        self.assertEqual(self.sent, [("An update for demo is waiting", "info")])

        again = watchdog.Watchdog(self.app)  # WebManager restarted
        self.dog = again
        self.round(200)
        self.assertEqual(len(self.sent), 1)

        with self.app.app_context():
            get_db().execute(
                "UPDATE repositories SET pending_commit = NULL, update_state = 'failed', update_error = 'boom' WHERE id = ?",
                (repository_id,),
            )
            get_db().commit()
        self.round(200)
        self.assertEqual(self.sent[-1], ("Update check failed for demo", "error"))

    def test_a_webmanager_update_is_announced_after_the_restart_it_causes(self):
        with patch.object(watchdog.update_status, "read_update_status", return_value={"state": "current", "installed_commit": "a" * 40}):
            self.round(200)
        self.assertEqual(self.sent, [])
        restarted = watchdog.Watchdog(self.app)
        self.dog = restarted
        with patch.object(watchdog.update_status, "read_update_status", return_value={"state": "current", "installed_commit": "b" * 40}):
            self.round(200)
        self.assertEqual(self.sent, [("WebManager was updated", "ok")])

    def test_a_nearly_full_disk_is_announced_once_a_day(self):
        full = type("U", (), {"total": 1000, "free": 20})()
        for _ in range(3):
            with patch.object(watchdog.shutil, "disk_usage", return_value=full), patch.object(watchdog, "_port_open", return_value=True):
                self.dog.run_once()
        self.assertEqual(self.sent, [("Disk is almost full", "warning")])

    def test_the_site_page_shows_uptime(self):
        _, site_id = self.site()
        self.round(200)
        self.assertIn("100% uptime", self.client.get(f"/sites/{site_id}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
