"""Servers in a replica group never install updates by themselves at the
same time: the primary goes first, each replica once the primary runs the
new version, and a primary waits while a replica is updating."""

import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import test_app as base

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = (ROOT / "deploy" / "debian" / "update.sh").read_text(encoding="utf-8")
TOKEN = "s3cret-s3cret-s3cret"
NEW = "c" * 40


def wait_check_script() -> str:
    """The Python the updater runs to decide whether to wait (mesh_wait_reason)."""
    body = UPDATE_SH.split("mesh_wait_reason() {", 1)[1]
    return re.search(r"<<'PY'[^\n]*\n(.*?)\nPY\n", body, re.S).group(1)


class FakeServer:
    """Answers /mesh/status with whatever `answer` holds (or 500)."""

    def __init__(self):
        server = self
        self.answer = {}
        self.authorizations = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.authorizations.append(self.headers.get("Authorization"))
                body = json.dumps(server.answer).encode()
                self.send_response(200 if server.answer is not None else 500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UpdaterWaitCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / "webmanager.env"
        self.primary = FakeServer()
        self.local = FakeServer()

    def tearDown(self):
        self.primary.close()
        self.local.close()
        self.tmp.cleanup()

    def reason(self, settings, promoted=False):
        self.env.write_text("".join(f"{key}={value}\n" for key, value in settings.items()), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-I", "-", str(self.env), NEW, "1" if promoted else "0", self.local.url],
            input=wait_check_script(), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def replica(self):
        return {"WEBMANAGER_PEER_TOKEN": TOKEN, "WEBMANAGER_REPLICA_OF": self.primary.url + "/"}

    def test_a_server_outside_a_group_never_waits(self):
        self.assertEqual(self.reason({"WEBMANAGER_REPLICA_OF": self.primary.url}), "")

    def test_a_replica_waits_for_the_primary_to_install_the_version_first(self):
        self.primary.answer = {"version": "a" * 12, "update_state": "available"}
        self.assertIn("waits for its primary to install this version first", self.reason(self.replica()))
        self.assertEqual(self.primary.authorizations[-1], f"Bearer {TOKEN}")

    def test_a_replica_waits_while_the_primary_updates(self):
        for state in ("testing", "backing_up", "installing"):
            with self.subTest(state=state):
                self.primary.answer = {"version": NEW[:12], "update_state": state}
                self.assertIn("finish updating", self.reason(self.replica()))

    def test_a_replica_waits_while_the_primary_cannot_be_reached(self):
        self.primary.close()
        self.assertIn("can be reached", self.reason(self.replica()))

    def test_a_replica_goes_once_the_primary_runs_the_version(self):
        self.primary.answer = {"version": NEW[:12], "update_state": "current"}
        self.assertEqual(self.reason(self.replica()), "")

    def test_a_promoted_replica_acts_as_a_primary(self):
        self.primary.answer = {"version": "a" * 12, "update_state": "current"}
        self.local.answer = {"peers_updating": 0}
        self.assertEqual(self.reason(self.replica(), promoted=True), "")

    def test_a_primary_waits_while_a_replica_updates(self):
        self.local.answer = {"peers_updating": 1}
        self.assertIn("waits for another server in its group to finish updating",
                      self.reason({"WEBMANAGER_PEER_TOKEN": TOKEN}))
        self.local.answer = {"peers_updating": 2}
        self.assertIn("waits for 2 other servers", self.reason({"WEBMANAGER_PEER_TOKEN": TOKEN}))

    def test_a_primary_goes_when_no_replica_updates(self):
        self.local.answer = {"peers_updating": 0}
        self.assertEqual(self.reason({"WEBMANAGER_PEER_TOKEN": TOKEN}), "")

    def test_a_primary_whose_dashboard_is_down_does_not_wait(self):
        self.local.close()
        self.assertEqual(self.reason({"WEBMANAGER_PEER_TOKEN": TOKEN}), "")


class UpdaterWiringTests(unittest.TestCase):
    def test_only_automatic_installs_wait(self):
        automatic = UPDATE_SH.split('if [[ ! -e $AUTO_FILE ]]; then\n        write_status "available"', 1)[1]
        automatic = automatic.split("AUTO_INSTALL=1", 1)[0]
        self.assertIn('WAIT_REASON=$(mesh_wait_reason "$NEW_COMMIT")', automatic)
        self.assertIn("exit 0", automatic)


class PeersUpdatingStatusTests(unittest.TestCase):
    setUp = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def entries(self, *states):
        return [
            {"url": f"http://10.0.0.{n}:8080", "reachable": reachable, "update_state": state}
            for n, (state, reachable) in enumerate(states, start=2)
        ]

    def test_mesh_status_lists_reachable_peers_that_are_updating(self):
        from webmanager import mesh

        hub = self.app.extensions["mesh_hub"]
        with patch.object(hub, "entries", return_value=self.entries(
            ("installing", True), ("current", True), ("testing", False), ("backing_up", True),
        )), self.app.app_context():
            status = mesh.local_status(self.app)
        self.assertEqual(status["peers_updating"], 2)


if __name__ == "__main__":
    unittest.main()
