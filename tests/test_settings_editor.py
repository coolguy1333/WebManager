"""Editing WebManager's settings from the System page: only the listed
settings, checked by the web app and again by the root helper, which writes
the settings file, restarts WebManager and puts the old file back if it
doesn't come up."""

import html
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests import test_app as base
from webmanager import replication, settings_editor

ROOT = Path(__file__).resolve().parent.parent
EDITOR = ROOT / "webmanager" / "settings_editor.py"
ENV_TEXT = """# WebManager settings
WEBMANAGER_PORT=5000
WEBMANAGER_GOOGLE_CLIENT_SECRET=keep-me
WEBMANAGER_SESSION_COOKIE_SECURE=0
WEBMANAGER_GOOGLE_ALLOWED_EMAILS=
# trailing comment
"""


class NormalizeTests(unittest.TestCase):
    def test_values_are_normalized(self):
        cases = {
            ("WEBMANAGER_SESSION_COOKIE_SECURE", "on"): "1",
            ("WEBMANAGER_SESSION_COOKIE_SECURE", ""): "0",
            ("WEBMANAGER_SITE_PUBLIC_SCHEME", "HTTPS"): "https",
            ("WEBMANAGER_BACKUP_KEEP", " 30 "): "30",
            ("WEBMANAGER_APP_CPUS", "0.50"): "0.5",
            ("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "Me@Gmail.com, b@x.org;me@gmail.com"): "me@gmail.com,b@x.org",
            ("WEBMANAGER_GOOGLE_ALLOWED_DOMAINS", "Example.COM"): "example.com",
            ("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", ""): "",
        }
        for (key, raw), expected in cases.items():
            with self.subTest(key=key, raw=raw):
                self.assertEqual(settings_editor.normalize(key, raw), expected)

    def test_bad_values_are_refused(self):
        for key, raw in (
            ("WEBMANAGER_SITE_PUBLIC_SCHEME", "ftp"),
            ("WEBMANAGER_BACKUP_KEEP", "0"),
            ("WEBMANAGER_BACKUP_KEEP", "lots"),
            ("WEBMANAGER_SESSION_COOKIE_SECURE", "maybe"),
            ("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "not-an-email"),
            ("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "a@b.co\nWEBMANAGER_REPLICA_OF=http://evil"),
            ("WEBMANAGER_GOOGLE_ALLOWED_DOMAINS", "exa mple..com"),
        ):
            with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                settings_editor.normalize(key, raw)

    def test_secrets_addresses_and_the_group_cannot_be_changed(self):
        for key in (
            "WEBMANAGER_GOOGLE_CLIENT_SECRET", "WEBMANAGER_PEER_TOKEN", "WEBMANAGER_REPLICA_OF",
            "WEBMANAGER_HOST", "WEBMANAGER_PORT", "WEBMANAGER_DASHBOARD_SETTINGS",
            "WEBMANAGER_DASHBOARD_APP_CONTROL", "WEBMANAGER_APPS_ENABLED", "PATH",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                settings_editor.validate({key: "1"})


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / "webmanager.env"
        self.env.write_text(ENV_TEXT, encoding="utf-8")
        os.chmod(self.env, 0o640)

    def tearDown(self):
        self.tmp.cleanup()

    def test_changes_keep_everything_else(self):
        changed = settings_editor.apply_to_file(str(self.env), {
            "WEBMANAGER_SESSION_COOKIE_SECURE": "1",
            "WEBMANAGER_GOOGLE_ALLOWED_EMAILS": "a@b.co",
            "WEBMANAGER_BACKUP_KEEP": "30",
        })
        self.assertEqual(changed, [
            "WEBMANAGER_SESSION_COOKIE_SECURE", "WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "WEBMANAGER_BACKUP_KEEP",
        ])
        self.assertEqual(self.env.read_text(encoding="utf-8"), ENV_TEXT
                         .replace("COOKIE_SECURE=0", "COOKIE_SECURE=1")
                         .replace("ALLOWED_EMAILS=\n", "ALLOWED_EMAILS=a@b.co\n")
                         + "WEBMANAGER_BACKUP_KEEP=30\n")
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o640)

    def test_a_duplicate_line_cannot_override_the_new_value(self):
        self.env.write_text("WEBMANAGER_BACKUP_KEEP=5\nWEBMANAGER_BACKUP_KEEP=7\n", encoding="utf-8")
        settings_editor.apply_to_file(str(self.env), {"WEBMANAGER_BACKUP_KEEP": "9"})
        self.assertEqual(self.env.read_text(encoding="utf-8"), "WEBMANAGER_BACKUP_KEEP=9\n")

    def run_script(self, request_text):
        request = Path(self.tmp.name) / "request.json"
        request.write_text(request_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", str(EDITOR), "apply", str(request), str(self.env)],
            capture_output=True, text=True, timeout=30,
        )

    def test_the_root_helper_script_applies_a_request(self):
        result = self.run_script(json.dumps({"WEBMANAGER_SESSION_COOKIE_SECURE": "1"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "WEBMANAGER_SESSION_COOKIE_SECURE")
        self.assertIn("WEBMANAGER_SESSION_COOKIE_SECURE=1", self.env.read_text(encoding="utf-8"))

    def test_the_root_helper_script_refuses_and_changes_nothing(self):
        for request in (
            json.dumps({"WEBMANAGER_REPLICA_OF": "http://evil.example"}),
            json.dumps({"WEBMANAGER_SESSION_COOKIE_SECURE": "1", "WEBMANAGER_PEER_TOKEN": "x" * 20}),
            "not json",
            json.dumps(["WEBMANAGER_SESSION_COOKIE_SECURE"]),
            json.dumps({"WEBMANAGER_GOOGLE_ALLOWED_EMAILS": "x" * 20000}),
        ):
            with self.subTest(request=request[:60]):
                result = self.run_script(request)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(self.env.read_text(encoding="utf-8"), ENV_TEXT)


class Health:
    """Stands in for WebManager's /healthz."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ControlHelperTests(unittest.TestCase):
    """Runs deploy/debian/control.sh with its paths moved into a temporary
    folder and stand-ins for systemctl and journalctl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        (self.state / "requests").mkdir(parents=True)
        self.env = root / "webmanager.env"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.calls = root / "systemctl.calls"
        (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\nexit 0\n')
        (bin_dir / "journalctl").write_text("#!/bin/sh\necho 'Traceback: bad setting'\n")
        for tool in ("systemctl", "journalctl"):
            os.chmod(bin_dir / tool, 0o755)
        script = (ROOT / "deploy" / "debian" / "control.sh").read_text(encoding="utf-8")
        script = (
            script.replace("STATE_DIR=/var/lib/webmanager-updater", f"STATE_DIR={self.state}")
            .replace("ENV_FILE=/etc/webmanager/webmanager.env", f"ENV_FILE={self.env}")
            .replace("SETTINGS_EDITOR=/opt/webmanager/webmanager/settings_editor.py", f"SETTINGS_EDITOR={EDITOR}")
            .replace('    chown root:webmanager "$temporary"\n', "")
            .replace("for _ in {1..45}; do", "for _ in {1..2}; do")
        )
        self.script = root / "control.sh"
        self.script.write_text(script, encoding="utf-8")
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        self.health = Health()

    def tearDown(self):
        self.health.close()
        self.tmp.cleanup()

    def run_helper(self, request, port):
        self.env.write_text(ENV_TEXT.replace("WEBMANAGER_PORT=5000", f"WEBMANAGER_PORT={port}"), encoding="utf-8")
        self.before = self.env.read_text(encoding="utf-8")
        (self.state / "requests" / "settings").write_text(json.dumps(request), encoding="utf-8")
        result = subprocess.run(["bash", str(self.script)], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PATH": self.path})
        status = json.loads((self.state / "control-status.json").read_text(encoding="utf-8"))
        return result, status

    def test_saves_restarts_and_reports(self):
        result, status = self.run_helper({"WEBMANAGER_SESSION_COOKIE_SECURE": "1"}, self.health.port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["action"], "settings")
        self.assertIn("Saved WEBMANAGER_SESSION_COOKIE_SECURE", status["message"])
        self.assertIn("WEBMANAGER_SESSION_COOKIE_SECURE=1", self.env.read_text(encoding="utf-8"))
        self.assertIn("restart webmanager", self.calls.read_text())
        self.assertFalse((self.state / "requests" / "settings").exists())

    def test_puts_the_old_settings_back_when_webmanager_does_not_come_up(self):
        self.health.close()
        result, status = self.run_helper({"WEBMANAGER_SESSION_COOKIE_SECURE": "1"}, self.health.port)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(status["state"], "error")
        self.assertIn("previous settings were put back", status["message"])
        self.assertIn("Traceback: bad setting", status["detail"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), self.before)
        self.assertEqual(self.calls.read_text().count("restart webmanager"), 2)

    def test_a_refused_setting_changes_nothing_and_restarts_nothing(self):
        result, status = self.run_helper({"WEBMANAGER_REPLICA_OF": "http://evil.example"}, self.health.port)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(status["state"], "error")
        self.assertIn("can't be changed from the dashboard", status["message"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), self.before)
        self.assertNotIn("restart", self.calls.read_text() if self.calls.exists() else "")

    def test_switched_off_on_the_server(self):
        self.env.write_text("WEBMANAGER_DASHBOARD_SETTINGS=0\n", encoding="utf-8")
        (self.state / "requests" / "settings").write_text('{"WEBMANAGER_BACKUP_KEEP": "3"}', encoding="utf-8")
        subprocess.run(["bash", str(self.script)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": self.path})
        status = json.loads((self.state / "control-status.json").read_text(encoding="utf-8"))
        self.assertIn("WEBMANAGER_DASHBOARD_SETTINGS=0", status["message"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), "WEBMANAGER_DASHBOARD_SETTINGS=0\n")

    def test_the_path_unit_watches_settings_requests(self):
        unit = (ROOT / "deploy" / "debian" / "webmanager-control.path").read_text(encoding="utf-8")
        self.assertIn("PathChanged=/var/lib/webmanager-updater/requests/settings", unit)


class SystemPageSettingsTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.app.config["CONTROL_ACTIVE"] = True
        self.root = self.add_user("root", "root@example.com", is_admin=True)
        self.login_user(self.root)
        self.request_file = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).parent / "settings"

    def form(self, **overrides):
        """The form as the page sends it, with the current values."""
        with self.app.app_context():
            values = settings_editor.current_values(self.app.config)
        data = {"_csrf_token": self.csrf()}
        for setting in settings_editor.SETTINGS:
            if setting["apps"]:
                continue
            value = values[setting["key"]]
            if setting["kind"] != "flag" or value == "1":
                data[setting["key"]] = value
        for key, value in overrides.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    def save(self, **overrides):
        response = self.client.post("/admin/settings", data=self.form(**overrides), follow_redirects=True)
        return response, html.unescape(response.get_data(as_text=True))

    def test_the_card_shows_current_values(self):
        page = html.unescape(self.client.get("/admin/?section=updates").get_data(as_text=True))
        self.assertIn('id="settings"', page)
        self.assertIn('name="WEBMANAGER_SITE_PUBLIC_SCHEME"', page)
        self.assertIn('name="WEBMANAGER_GOOGLE_ALLOWED_EMAILS"', page)
        self.assertNotIn('name="WEBMANAGER_GOOGLE_CLIENT_SECRET"', page)
        self.assertNotIn('name="WEBMANAGER_APP_CPUS"', page)  # app hosting is off

    def test_only_changed_settings_are_filed(self):
        _response, page = self.save(
            WEBMANAGER_SESSION_COOKIE_SECURE="1", WEBMANAGER_GOOGLE_ALLOWED_EMAILS="Root@Example.com, b@x.org",
        )
        self.assertIn("WebManager restarts in a moment", page)
        self.assertEqual(json.loads(self.request_file.read_text(encoding="utf-8")), {
            "WEBMANAGER_SESSION_COOKIE_SECURE": "1",
            "WEBMANAGER_GOOGLE_ALLOWED_EMAILS": "root@example.com,b@x.org",
        })
        self.assertIn("Waiting for the server to pick up the request", page)

    def test_unticking_a_box_turns_it_off(self):
        self.save(WEBMANAGER_BACKUPS_ENABLED=None)
        self.assertEqual(json.loads(self.request_file.read_text(encoding="utf-8")),
                         {"WEBMANAGER_BACKUPS_ENABLED": "0"})

    def test_nothing_changed(self):
        _response, page = self.save()
        self.assertIn("Nothing changed.", page)
        self.assertFalse(self.request_file.exists())

    def test_bad_values_are_refused(self):
        _response, page = self.save(WEBMANAGER_BACKUP_KEEP="0")
        self.assertIn("Backups to keep: must be between 1 and 365", page)
        self.assertFalse(self.request_file.exists())

    def test_you_cannot_lock_yourself_out(self):
        _response, page = self.save(WEBMANAGER_GOOGLE_ALLOWED_EMAILS="someone@else.org")
        self.assertIn("Your own account (root@example.com) isn't in the allowed emails", page)
        self.assertFalse(self.request_file.exists())
        self.save(WEBMANAGER_GOOGLE_ALLOWED_DOMAINS="example.com", WEBMANAGER_GOOGLE_ALLOWED_EMAILS="someone@else.org")
        self.assertTrue(self.request_file.exists())

    def test_switched_off_on_the_server(self):
        self.app.config["DASHBOARD_SETTINGS"] = False
        _response, page = self.save(WEBMANAGER_SESSION_COOKIE_SECURE="1")
        self.assertIn("WEBMANAGER_DASHBOARD_SETTINGS=0", page)
        self.assertFalse(self.request_file.exists())

    def test_only_super_admins(self):
        self.login_user(self.add_user("bob", "bob@example.com"))
        response = self.client.post("/admin/settings", data=self.form(WEBMANAGER_SESSION_COOKIE_SECURE="1"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.request_file.exists())

    def test_the_result_is_shown(self):
        Path(self.app.config["PROGRAM_UPDATE_STATUS_FILE"]).with_name("control-status.json").write_text(json.dumps({
            "state": "error", "action": "settings",
            "message": "WebManager didn't start with the new settings (X), so the previous settings were put back.",
        }), encoding="utf-8")
        page = html.unescape(self.client.get("/admin/?section=updates").get_data(as_text=True))
        self.assertIn("The settings weren't saved", page)
        self.assertIn("previous settings were put back", page)
        self.assertNotIn("The last change didn't work", page)  # not shown as an app hosting change

    def test_a_replica_saves_its_own_settings(self):
        self.assertIn("admin.save_settings", replication._LOCAL_ONLY_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
