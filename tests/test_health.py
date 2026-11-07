"""The System page's health checks: each says what's wrong and what to do."""

import os
import subprocess
import sys
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webmanager import health

from tests import test_app as base


def make_inputs(directory, **overrides):
    inputs = {
        "nginx_binary": "/usr/sbin/nginx", "gateway_port": 1, "nginx_root": str(directory),
        "data_dir": str(directory), "running_sites": [], "google_configured": True,
        "google_redirect_uri": "https://wm.example/auth/google/callback", "cookie_secure": True,
        "unrestricted": False, "request_host": "wm.example", "base_domain": "wm.example",
        "update_status": {"state": "current", "message": ""}, "updater_active": True,
        "auto_install": False, "apps_enabled": False, "apps_status": (False, ""),
        "peers": [], "replication": {"is_replica": False, "last_error": None, "data_last_error": None},
    }
    inputs.update(overrides)
    return inputs


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def inputs(self, **overrides):
        return make_inputs(self.path, **overrides)

    def listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    # -- web server --------------------------------------------------------
    def test_web_server_up_down_and_missing(self):
        port = self.listener()
        self.assertEqual(health.check_web_server(self.inputs(gateway_port=port))["status"], "ok")
        down = health.check_web_server(self.inputs(gateway_port=1))
        self.assertEqual(down["status"], "fail")
        self.assertIn("systemctl restart webmanager", down["fix"])
        missing = health.check_web_server(self.inputs(nginx_binary=None))
        self.assertEqual(missing["status"], "warn")
        self.assertIn("apt install nginx", missing["fix"])

    def test_a_running_site_that_does_not_answer_is_named(self):
        site = {"id": 1, "name": "Docs", "hostnames": ["docs.wm.example"]}
        port = self.listener()  # accepts the connection, never answers HTTP
        with patch.object(health.peer_http, "open_peer", side_effect=OSError("refused")):
            result = health.check_sites(self.inputs(gateway_port=port, running_sites=[site]))
        self.assertEqual(result["status"], "fail")
        self.assertIn("Docs (no answer)", result["detail"])

    def test_sites_that_answer_are_fine(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        site = {"id": 1, "name": "Docs", "hostnames": ["docs.wm.example"]}
        with patch.object(health.peer_http, "open_peer", return_value=Response()):
            result = health.check_sites(self.inputs(gateway_port=self.listener(), running_sites=[site]))
        self.assertEqual(result["status"], "ok")

    def test_no_running_sites_is_just_information(self):
        self.assertEqual(health.check_sites(self.inputs())["status"], "info")

    # -- traffic log --------------------------------------------------------------
    def test_traffic_log_states(self):
        sites = [{"id": 1, "name": "A", "hostnames": ["a.wm.example"]}]
        self.assertEqual(health.check_traffic_log(self.inputs(running_sites=sites))["status"], "warn")
        self.assertEqual(health.check_traffic_log(self.inputs())["status"], "info")
        log = self.path / "access.log"
        log.write_text('{"x":1}\n')
        self.assertEqual(health.check_traffic_log(self.inputs(running_sites=sites))["status"], "ok")
        if os.geteuid() != 0:
            log.chmod(0)
            self.assertEqual(health.check_traffic_log(self.inputs(running_sites=sites))["status"], "fail")

    # -- disk ------------------------------------------------------------------------
    def test_disk_thresholds(self):
        usage = lambda free_percent: type("U", (), {"total": 100 * 1024**3, "free": free_percent * 1024**3})()  # noqa: E731
        for free, expected in ((50, "ok"), (10, "warn"), (2, "fail")):
            with self.subTest(free=free), patch.object(health.shutil, "disk_usage", return_value=usage(free)):
                self.assertEqual(health.check_disk(self.inputs())["status"], expected)

    # -- sign-in --------------------------------------------------------------------------
    def test_sign_in_problems_explain_the_fix(self):
        ok = health.check_sign_in(self.inputs())
        self.assertEqual(ok["status"], "ok")

        loop = health.check_sign_in(self.inputs(google_redirect_uri="http://wm.example/cb", cookie_secure=True))
        self.assertEqual(loop["status"], "fail")
        self.assertIn("SESSION_COOKIE_SECURE=0", loop["fix"])

        insecure = health.check_sign_in(self.inputs(cookie_secure=False))
        self.assertEqual(insecure["status"], "fail")
        self.assertIn("SESSION_COOKIE_SECURE=1", insecure["fix"])

        other_host = health.check_sign_in(self.inputs(request_host="192.168.1.5"))
        self.assertEqual(other_host["status"], "ok", "an IP address is how people reach it by hand")
        wrong = health.check_sign_in(self.inputs(request_host="other.example"))
        self.assertEqual(wrong["status"], "fail")
        self.assertIn("wm.example", wrong["detail"])

        self.assertEqual(health.check_sign_in(self.inputs(google_configured=False))["status"], "fail")
        self.assertEqual(health.check_sign_in(self.inputs(unrestricted=True))["status"], "warn")

    # -- dns -----------------------------------------------------------------------------------
    def test_dns(self):
        with patch.object(health.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.9", 0))]):
            self.assertEqual(health.check_dns(self.inputs())["status"], "ok")
        with patch.object(health.socket, "getaddrinfo", side_effect=socket.gaierror("no such host")):
            result = health.check_dns(self.inputs())
        self.assertEqual(result["status"], "warn")
        self.assertIn("*.wm.example", result["fix"])
        self.assertEqual(health.check_dns(self.inputs(base_domain=""))["status"], "warn")

    # -- updates, apps, data, servers -------------------------------------------------------
    def test_updates(self):
        self.assertEqual(health.check_updates(self.inputs())["status"], "ok")
        self.assertEqual(health.check_updates(self.inputs(updater_active=False))["status"], "warn")
        failed = health.check_updates(self.inputs(update_status={"state": "error", "message": "tests failed"}))
        self.assertEqual(failed["status"], "fail")
        self.assertIn("tests failed", failed["detail"])
        available = health.check_updates(self.inputs(update_status={"state": "available"}, auto_install=True))
        self.assertEqual(available["status"], "info")
        self.assertIn("by itself", available["detail"])

    def test_apps(self):
        self.assertEqual(health.check_apps(self.inputs())["status"], "info")
        self.assertEqual(health.check_apps(self.inputs(apps_enabled=True, apps_status=(True, "docker 27")))["status"], "ok")
        down = health.check_apps(self.inputs(apps_enabled=True, apps_status=(False, "The container runtime isn't reachable")))
        self.assertEqual(down["status"], "fail")
        self.assertIn("enable-apps.sh", down["fix"])

    def test_data_folder(self):
        self.assertEqual(health.check_data_dir(self.inputs())["status"], "ok")
        self.assertEqual(health.check_data_dir(self.inputs(data_dir="/proc/nope"))["status"], "fail")

    def test_other_servers(self):
        self.assertEqual(health.check_servers(self.inputs())["status"], "info")
        peers = [{"hostname": "b", "reachable": False, "checked_at": 1}, {"hostname": "c", "reachable": True, "checked_at": 1}]
        result = health.check_servers(self.inputs(peers=peers))
        self.assertEqual(result["status"], "warn")
        self.assertIn("b", result["detail"])
        replica = {"is_replica": True, "last_error": "token rejected", "data_last_error": None}
        self.assertIn("token rejected", health.check_servers(self.inputs(replication=replica))["detail"])
        good = [{"hostname": "b", "reachable": True, "checked_at": 1}]
        self.assertEqual(health.check_servers(self.inputs(peers=good))["status"], "ok")

    # -- running them all ------------------------------------------------------------------------
    def test_a_crashing_or_hanging_check_cannot_break_the_report(self):
        def boom(_inputs):
            raise RuntimeError("bug")

        def hang(_inputs):
            time.sleep(10)

        with patch.object(health, "CHECKS", (health.check_git, boom, hang)), patch.object(health, "CHECK_TIMEOUT", 0.3):
            started = time.monotonic()
            report = health.run_checks(self.inputs())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(report["checks"]), 3)
        self.assertEqual(report["counts"]["warn"], 2)

    def test_problems_come_first(self):
        with patch.object(health, "CHECKS", (health.check_git, health.check_data_dir)):
            report = health.run_checks(self.inputs(data_dir="/proc/nope"))
        self.assertEqual(report["checks"][0]["status"], "fail")


class DoctorCommandTests(unittest.TestCase):
    def test_text_report_lists_problems_with_what_to_do(self):
        report = {
            "checks": [
                health.result("git", "Git", "fail", "Git isn't installed.", "sudo apt install git"),
                health.result("disk", "Free disk space", "ok", "40 GB free."),
            ],
            "counts": {"fail": 1, "warn": 0, "info": 0, "ok": 1},
        }
        text = health.format_report(report)
        self.assertIn("✕ Git: Git isn't installed.", text)
        self.assertIn("What to do: sudo apt install git", text)
        self.assertIn("1 problem(s)", text)
        self.assertNotIn("What to do: 40", text)

    def test_the_command_prints_a_report_without_starting_the_app(self):
        with tempfile.TemporaryDirectory() as data:
            root = Path(__file__).resolve().parent.parent
            done = subprocess.run(
                [sys.executable, str(root / "run.py"), "doctor"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "WEBMANAGER_DATA_DIR": data, "WEBMANAGER_PEERS": "", "WEBMANAGER_REPLICA_OF": ""},
            )
        self.assertIn("WebManager health check", done.stdout)
        self.assertIn(done.returncode, (0, 1))
        self.assertIn("WebManager's data folder", done.stdout)


class HealthEndpointTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()

    def test_admins_get_a_report_and_others_do_not(self):
        self.login_user(self.add_user("alice"))
        self.assertEqual(self.client.get("/admin/health.json").status_code, 403)

        self.login_user(self.add_user("root", is_admin=True))
        response = self.client.get("/admin/health.json")
        self.assertEqual(response.status_code, 200)
        report = response.get_json()
        self.assertEqual(
            {item["id"] for item in report["checks"]},
            {"web_server", "sites", "traffic_log", "sign_in", "dns", "disk", "data_dir", "git",
             "updates", "apps", "servers"},
        )
        for item in report["checks"]:
            self.assertIn(item["status"], {"ok", "info", "warn", "fail"})
            self.assertTrue(item["detail"])

    def test_system_page_has_the_panel(self):
        self.login_user(self.add_user("root", is_admin=True))
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        self.assertIn("data-health-url", page)
        self.assertIn("Health checks", page)


if __name__ == "__main__":
    unittest.main()
