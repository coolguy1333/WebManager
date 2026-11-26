"""Updating WebManager on another server from this one's Servers list:
the button, the request between servers, and the other server's answer."""

import json
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from webmanager import mesh, peer_http, update_status

from tests import test_app as base

OURS, THEIRS, NEWER = "a" * 40, "b" * 40, "c" * 40
TOKEN = "s3cret-s3cret-s3cret"
PEER = "http://10.0.0.2:8080"


def write_status(app, **values):
    status = {
        "state": "current", "installed_commit": OURS, "available_commit": OURS,
        "update_available": False, "message": "WebManager is current.",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    status.update(values)
    Path(app.config["PROGRAM_UPDATE_STATUS_FILE"]).write_text(json.dumps(status), encoding="utf-8")


class _Answer:
    def __init__(self, body, status=200):
        self.body, self.status = json.dumps(body).encode(), status

    def read(self, *_args):
        return self.body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class AskedToUpdateTests(unittest.TestCase):
    """POST /mesh/update on the server being updated."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def setUp(self):
        self.setUp_base()
        self.app.config["UPDATER_ACTIVE"] = True
        self.hub = self.app.extensions["mesh_hub"]
        self.hub.set_token(TOKEN)
        self.addCleanup(setattr, update_status, "_live_status", None)
        update_status._live_status = None
        # This server runs THEIRS; it is asked to install OURS.
        write_status(self.app, installed_commit=THEIRS, available_commit=OURS, update_available=True, state="available")
        self.request_file = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"])

    def ask(self, commit=OURS, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/mesh/update", json={"commit": commit}, headers=headers)

    def test_it_needs_the_shared_token(self):
        self.assertEqual(self.ask(token=None).status_code, 401)
        self.assertEqual(self.ask(token="wrong-wrong-wrong-wrong").status_code, 401)
        self.hub.set_token("")
        self.assertEqual(self.ask().status_code, 401, "never open without a token")
        self.assertFalse(self.request_file.exists())

    def test_it_files_the_same_approval_as_the_install_button(self):
        response = self.ask()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(self.request_file.read_text(encoding="ascii").strip(), OURS)

    def test_it_refuses_anything_but_a_full_commit(self):
        for commit in ("", "abc", "A" * 40, OURS + "\n", "../../etc/passwd"):
            self.assertEqual(self.ask(commit).status_code, 400, commit)
        self.assertFalse(self.request_file.exists())

    def test_nothing_to_do_when_it_already_runs_that_version(self):
        response = self.ask(THEIRS)
        self.assertEqual(response.get_json()["state"], "current")
        self.assertFalse(self.request_file.exists())

    def test_an_update_already_underway_is_left_alone(self):
        write_status(self.app, state="installing", installed_commit=THEIRS, available_commit=OURS, update_available=True)
        response = self.ask()
        self.assertEqual(response.get_json()["state"], "installing")
        self.assertFalse(self.request_file.exists())

    def test_it_says_so_when_its_updater_is_switched_off(self):
        self.app.config["UPDATER_ACTIVE"] = False
        response = self.ask()
        self.assertEqual(response.status_code, 409)
        self.assertIn("updater service is switched off", response.get_json()["error"])
        self.assertFalse(self.request_file.exists())

    def test_it_explains_when_that_version_is_not_the_newest_on_its_branch(self):
        self.app.config["LIVE_UPDATE_CHECK"] = True
        newer = update_status._result("available", "", THEIRS, NEWER)
        with patch.object(mesh, "run_live_check", return_value=newer):
            response = self.ask()
        self.assertEqual(response.status_code, 409)
        self.assertIn("Update this server first", response.get_json()["error"])

        at_newest = update_status._result("current", "", NEWER, NEWER)
        with patch.object(mesh, "run_live_check", return_value=at_newest):
            response = self.ask()
        self.assertEqual(response.status_code, 409)
        self.assertIn("already runs the newest version", response.get_json()["error"])
        self.assertFalse(self.request_file.exists())

        same = update_status._result("available", "", THEIRS, OURS)
        with patch.object(mesh, "run_live_check", return_value=same):
            self.assertEqual(self.ask().status_code, 202)

    def test_a_failed_check_of_github_does_not_block_it(self):
        self.app.config["LIVE_UPDATE_CHECK"] = True
        failed = update_status._result("error", "Could not reach GitHub", THEIRS, None)
        with patch.object(mesh, "run_live_check", return_value=failed):
            self.assertEqual(self.ask().status_code, 202)

    def test_its_status_tells_others_how_its_update_is_going(self):
        write_status(self.app, state="testing", installed_commit=THEIRS)
        data = self.client.get("/mesh/status", headers={"Authorization": f"Bearer {TOKEN}"}).get_json()
        self.assertEqual(data["update_state"], "testing")
        self.assertEqual(data["version"], THEIRS[:12])


class AskingAnotherServerTests(unittest.TestCase):
    """MeshHub.request_update: what the admin is told."""

    def setUp(self):
        self.hub = mesh.MeshHub(None, [PEER], token=TOKEN)

    def test_a_request_it_accepts(self):
        answer = _Answer({"ok": True, "state": "requested", "message": "It is installing."}, 202)
        with patch.object(peer_http, "open_peer", return_value=answer) as opener, \
                patch.object(self.hub, "_poll"):
            ok, message = self.hub.request_update(PEER, OURS)
        self.assertTrue(ok)
        self.assertEqual(message, "It is installing.")
        sent = opener.call_args[0][0]
        self.assertEqual(sent.full_url, f"{PEER}/mesh/update")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(json.loads(sent.data), {"commit": OURS})

    def test_it_shows_as_updating_from_the_moment_it_is_asked(self):
        with self.hub._lock:
            self.hub._remote[PEER] = {"reachable": True, "data": {"version": THEIRS[:12], "update_state": "available"}}
        answer = _Answer({"ok": True, "state": "requested", "message": "It is installing."}, 202)
        with patch.object(peer_http, "open_peer", return_value=answer), patch.object(self.hub, "_poll"):
            self.hub.request_update(PEER, OURS)
        self.assertEqual(self.hub.entries()[0]["update_state"], "requested", "before its updater starts")

        for data, expected in (
            ({"version": THEIRS[:12], "update_state": "testing"}, "testing"),
            ({"version": OURS[:12], "update_state": "current"}, "current"),
        ):
            with self.hub._lock:
                self.hub._remote[PEER]["data"] = data
            self.assertEqual(self.hub.entries()[0]["update_state"], expected)
        with self.hub._lock:
            self.hub._remote[PEER]["data"] = {"version": THEIRS[:12], "update_state": "available"}
        self.assertEqual(self.hub.entries()[0]["update_state"], "available", "no longer pending once it reported")

    def test_its_refusal_is_passed_on(self):
        error = urllib.error.HTTPError(
            f"{PEER}/mesh/update", 409, "Conflict", {}, _Answer({"error": "Its updater service is switched off."})
        )
        with patch.object(peer_http, "open_peer", side_effect=error):
            ok, message = self.hub.request_update(PEER, OURS)
        self.assertFalse(ok)
        self.assertEqual(message, "Its updater service is switched off.")

    def test_a_server_too_old_to_be_updated_this_way(self):
        error = urllib.error.HTTPError(f"{PEER}/mesh/update", 404, "Not Found", {}, _Answer({}))
        with patch.object(peer_http, "open_peer", side_effect=error):
            ok, message = self.hub.request_update(PEER, OURS)
        self.assertFalse(ok)
        self.assertIn("too old to be managed from here", message)

    def test_a_server_that_does_not_answer(self):
        with patch.object(peer_http, "open_peer", side_effect=urllib.error.URLError(ConnectionRefusedError())):
            ok, message = self.hub.request_update(PEER, OURS)
        self.assertFalse(ok)
        self.assertIn("could not be reached", message)


class UpdateButtonTests(unittest.TestCase):
    """The Servers list on the System page, and the button's action."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.addCleanup(setattr, update_status, "_live_status", None)
        update_status._live_status = None
        write_status(self.app)
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

    def update_form(self):
        page = self.client.get("/admin/?section=updates").get_data(as_text=True)
        start = page.index("data-mesh-update")
        return page[page.rindex("<form", 0, start):page.index("</form>", start)]

    def test_a_server_on_another_version_gets_an_update_button(self):
        self.peer_reports(version=THEIRS[:12], update_state="current")
        form = self.update_form()
        self.assertNotIn(" hidden", form.split(">")[0])
        self.assertIn(OURS[:12], form)

    def test_no_button_when_it_already_matches_or_is_updating(self):
        for data in (
            {"version": OURS[:12], "update_state": "current"},
            {"version": THEIRS[:12], "update_state": "installing"},
        ):
            self.peer_reports(**data)
            self.assertIn(" hidden", self.update_form().split(">")[0], data)

    def test_pressing_it_asks_that_server_to_install_this_version(self):
        with patch.object(self.hub, "request_update", return_value=(True, "It is installing.")) as ask:
            response = self.client.post(
                "/admin/servers/update", data={"_csrf_token": self.csrf(), "url": PEER}, follow_redirects=True
            )
        ask.assert_called_once_with(PEER, OURS)
        self.assertIn(f"Updating {PEER} to {OURS[:12]}", response.get_data(as_text=True))

    def test_only_servers_on_the_list_and_only_admins(self):
        with patch.object(self.hub, "request_update") as ask:
            response = self.client.post(
                "/admin/servers/update",
                data={"_csrf_token": self.csrf(), "url": "http://elsewhere.example:8080"},
                follow_redirects=True,
            )
        ask.assert_not_called()
        self.assertIn("not in this server&#39;s list", response.get_data(as_text=True))

        self.login_user(self.add_user("alice"))
        with patch.object(self.hub, "request_update") as ask:
            response = self.client.post("/admin/servers/update", data={"_csrf_token": self.csrf(), "url": PEER})
        self.assertEqual(response.status_code, 403)
        ask.assert_not_called()

    def test_a_server_that_does_not_know_its_own_version_says_so(self):
        write_status(self.app, installed_commit=None)
        with patch.object(self.hub, "request_update") as ask:
            response = self.client.post(
                "/admin/servers/update", data={"_csrf_token": self.csrf(), "url": PEER}, follow_redirects=True
            )
        ask.assert_not_called()
        self.assertIn("doesn&#39;t know which WebManager version it runs", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
