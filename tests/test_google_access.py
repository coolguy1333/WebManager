"""Google sign-in never creates accounts for strangers, insecure sign-in
cookies are flagged, super admins can edit and delete accounts, and server
alerts are only shown to super admins."""

import html
import io
import os
import unittest
import urllib.error
from unittest.mock import patch

from tests import test_app as base
from webmanager import peer_http
from webmanager.auth import REFUSED_MESSAGE
from webmanager.db import get_db

HTTPS_CF = {"Cf-Visitor": '{"scheme":"https"}'}


class Case(unittest.TestCase):
    setUp = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf
    google_claims = base.WebManagerTestCase.google_claims
    google_callback = base.WebManagerTestCase.google_callback

    def users(self):
        with self.app.app_context():
            return get_db().execute("SELECT * FROM users ORDER BY id").fetchall()

    def signed_in_as(self):
        with self.client.session_transaction() as session:
            return session.get("user_id")

    def login_page_text(self):
        return html.unescape(self.client.get("/auth/login").get_data(as_text=True))


class GoogleSignInWithoutAllowListsTests(Case):
    def test_a_stranger_is_refused_and_gets_no_account(self):
        owner = self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        response = self.google_callback(self.google_claims(sub="stranger", email="stranger@example.net"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])
        self.assertEqual([user["id"] for user in self.users()], [owner])
        self.assertIsNone(self.signed_in_as())
        self.assertIn(REFUSED_MESSAGE, self.login_page_text())

    def test_the_message_names_the_setting(self):
        self.assertEqual(
            REFUSED_MESSAGE,
            "This Google account isn't allowed. Ask the admin to add it to WEBMANAGER_GOOGLE_ALLOWED_EMAILS.",
        )

    def test_an_existing_account_still_signs_in_by_google_identity(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        alice = self.add_user("alice", "old-address@example.com", "google-subject-123")
        response = self.google_callback()
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.signed_in_as(), alice)
        self.assertEqual(len(self.users()), 2)

    def test_an_existing_account_still_signs_in_by_email(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        alice = self.add_user("alice", "alice@example.com")
        response = self.google_callback()
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.signed_in_as(), alice)
        self.assertEqual(self.users()[1]["google_sub"], "google-subject-123")

    def test_the_first_account_on_an_empty_database_is_created(self):
        self.assertEqual(self.users(), [])
        response = self.google_callback()
        self.assertEqual(response.headers["Location"], "/")
        (user,) = self.users()
        self.assertEqual(user["email"], "alice@example.com")
        self.assertEqual(user["is_admin"], 1)
        # ...and only the first: the next stranger is refused.
        self.client.post("/auth/logout", data={"_csrf_token": self.csrf()})
        self.google_callback(self.google_claims(sub="second", email="second@example.com"))
        self.assertEqual(len(self.users()), 1)

    def test_a_disabled_account_is_still_refused(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        self.add_user("alice", "alice@example.com", "google-subject-123", is_active=False)
        self.google_callback()
        self.assertIsNone(self.signed_in_as())
        self.assertIn("disabled", self.login_page_text())


class GoogleSignInWithAllowListsTests(Case):
    def test_a_listed_email_gets_an_account(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        self.app.config["GOOGLE_ALLOWED_EMAILS"] = "someone@example.org, Alice@Example.com"
        response = self.google_callback()
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.users()[-1]["email"], "alice@example.com")
        self.assertEqual(self.users()[-1]["is_admin"], 0)

    def test_an_unlisted_email_is_refused_with_the_message(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        self.app.config["GOOGLE_ALLOWED_EMAILS"] = "someone@example.org"
        self.google_callback()
        self.assertEqual(len(self.users()), 1)
        self.assertIn(REFUSED_MESSAGE, self.login_page_text())

    def test_an_unlisted_existing_account_is_refused_too(self):
        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        self.add_user("alice", "alice@example.com", "google-subject-123")
        self.app.config["GOOGLE_ALLOWED_EMAILS"] = "owner@example.com"
        self.google_callback()
        self.assertIsNone(self.signed_in_as())


class GoogleSignInOnReplicaTests(Case):
    def setUp(self):
        super().setUp()
        self.manager = self.app.extensions["replication_manager"]
        self.manager.primary_url = "https://primary.example"
        self.manager.token = "s3cret-s3cret-s3cret"

    def tearDown(self):
        self.manager.primary_url = ""
        self.manager.token = ""
        super().tearDown()

    def test_a_refusal_from_the_primary_shows_its_message(self):
        refusal = urllib.error.HTTPError(
            "https://primary.example/replication/find-or-create-user", 400, "Bad Request", {},
            io.BytesIO(('{"error": "%s"}' % REFUSED_MESSAGE).encode()),
        )
        with patch.object(peer_http, "open_peer", side_effect=refusal):
            self.google_callback()
        self.assertIsNone(self.signed_in_as())
        page = self.login_page_text()
        self.assertIn(REFUSED_MESSAGE, page)
        self.assertNotIn("could not be reached", page)


class PrimaryEndpointTests(Case):
    def test_the_primary_refuses_strangers_asked_for_by_a_replica(self):
        from webmanager.mesh import _hash_token

        self.add_user("owner", "owner@example.com", "owner-sub", is_admin=True)
        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret-s3cret-s3cret"
        hub._token_hash = _hash_token(hub.token)
        response = self.client.post(
            "/replication/find-or-create-user",
            json=self.google_claims(sub="stranger", email="stranger@example.net"),
            headers={"Authorization": "Bearer s3cret-s3cret-s3cret"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], REFUSED_MESSAGE)
        self.assertEqual(len(self.users()), 1)


class StartupWarningTests(unittest.TestCase):
    def build(self, **environment):
        case = Case()
        with patch.dict(os.environ, environment):
            try:
                with self.assertLogs("webmanager", "WARNING") as logs:
                    case.setUp()
            except AssertionError:
                return []
            finally:
                if hasattr(case, "temp_directory"):
                    case.tearDown()
        return logs.output

    def test_google_without_allow_lists_is_logged(self):
        logs = "\n".join(self.build())
        self.assertIn("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", logs)
        self.assertIn("only accounts that already exist", logs)

    def test_https_without_secure_cookies_is_logged(self):
        # The test app serves sites as https and has an https callback.
        logs = "\n".join(self.build())
        self.assertIn("WEBMANAGER_SESSION_COOKIE_SECURE=0", logs)
        self.assertIn("WEBMANAGER_SITE_PUBLIC_SCHEME=https", logs)

    def test_nothing_is_logged_when_both_are_set_up(self):
        logs = "\n".join(self.build(
            WEBMANAGER_GOOGLE_ALLOWED_EMAILS="alice@example.com",
            WEBMANAGER_SESSION_COOKIE_SECURE="1",
        ))
        self.assertNotIn("ALLOWED_EMAILS", logs)
        self.assertNotIn("COOKIE_SECURE", logs)


class InsecureCookieWarningTests(Case):
    def setUp(self):
        super().setUp()
        self.login_user(self.add_user("root", is_admin=True))
        self.app.config["SITE_PUBLIC_SCHEME"] = "http"
        self.app.config["GOOGLE_REDIRECT_URI"] = "http://192.168.10.20:8080/auth/google/callback"

    def page(self, headers=None):
        return html.unescape(self.client.get("/admin/?section=updates", headers=headers or {}).get_data(as_text=True))

    def test_not_shown_when_nothing_is_https(self):
        self.assertNotIn('id="insecure-cookie-warning"', self.page())

    def test_https_site_scheme_is_flagged_with_the_fix(self):
        self.app.config["SITE_PUBLIC_SCHEME"] = "https"
        page = self.page()
        self.assertIn('id="insecure-cookie-warning"', page)
        self.assertIn("WEBMANAGER_SESSION_COOKIE_SECURE=1", page)
        self.assertIn("WEBMANAGER_SITE_PUBLIC_SCHEME=https", page)
        self.assertIn("sudo systemctl restart webmanager", page)

    def test_https_callback_address_is_flagged(self):
        self.app.config["GOOGLE_REDIRECT_URI"] = "https://webmgr.example/auth/google/callback"
        self.assertIn("WEBMANAGER_GOOGLE_REDIRECT_URI) starts with https", self.page())

    def test_opened_over_https_is_flagged(self):
        self.assertIn("this page was opened over https", self.page(HTTPS_CF))

    def test_not_shown_with_secure_cookies(self):
        self.app.config["SITE_PUBLIC_SCHEME"] = "https"
        self.app.config["SESSION_COOKIE_SECURE"] = True
        self.assertNotIn('id="insecure-cookie-warning"', self.page(HTTPS_CF))


class AccountEditingTests(Case):
    def setUp(self):
        super().setUp()
        self.root = self.add_user("root", "root@example.com", "root-sub", is_admin=True)
        self.login_user(self.root)

    def edit(self, user_id, **fields):
        data = {"_csrf_token": self.csrf(), "is_active": "on", **fields}
        return self.client.post(f"/admin/users/{user_id}", data=data, follow_redirects=True)

    def test_a_super_admin_can_change_name_username_and_email(self):
        bob = self.add_user("bob", "bob@example.com")
        self.edit(bob, username="robert", display_name="  Robert   Smith ", email="Robert@Example.com")
        user = self.users()[-1]
        self.assertEqual((user["username"], user["display_name"], user["email"]),
                         ("robert", "Robert Smith", "robert@example.com"))

    def test_a_google_account_keeps_its_google_email(self):
        bob = self.add_user("bob", "bob@example.com", "bob-sub")
        self.edit(bob, username="bob", display_name="Bob", email="other@example.com")
        self.assertEqual(self.users()[-1]["email"], "bob@example.com")

    def test_an_edited_name_survives_the_next_sign_in(self):
        alice = self.add_user("alice", "alice@example.com", "google-subject-123")
        self.edit(alice, username="alice", display_name="Ally")
        self.client.post("/auth/logout", data={"_csrf_token": self.csrf()})
        self.google_callback()
        self.assertEqual(self.users()[-1]["display_name"], "Ally")

    def test_bad_or_taken_values_are_refused(self):
        bob = self.add_user("bob", "bob@example.com")
        for fields, message in (
            ({"username": "x"}, "Usernames are 3-32"),
            ({"username": "root"}, "already taken"),
            ({"username": "bob", "email": "not-an-email"}, "valid email"),
            ({"username": "bob", "email": "root@example.com"}, "already uses"),
        ):
            with self.subTest(fields=fields):
                page = html.unescape(self.edit(bob, **fields).get_data(as_text=True))
                self.assertIn(message, page)
        self.assertEqual(self.users()[-1]["username"], "bob")
        self.assertEqual(self.users()[-1]["email"], "bob@example.com")

    def test_people_managers_who_are_not_super_admins_cannot_rename(self):
        manager = self.add_user("manager", "manager@example.com")
        with self.app.app_context():
            database = get_db()
            group = database.execute("INSERT INTO groups (name) VALUES ('People')").lastrowid
            database.execute(
                "INSERT INTO group_permissions (group_id, permission_code) VALUES (?, 'users.manage')", (group,)
            )
            database.execute("INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (manager, group))
            database.commit()
        bob = self.add_user("bob", "bob@example.com")
        self.login_user(manager)
        self.edit(bob, username="robert")
        self.assertEqual(self.users()[-1]["username"], "bob")
        self.assertNotIn("Delete account", self.client.get("/admin/?section=people").get_data(as_text=True))


class AccountDeletionTests(Case):
    def setUp(self):
        super().setUp()
        self.root = self.add_user("root", "root@example.com", "root-sub", is_admin=True)
        self.login_user(self.root)

    def delete(self, user_id):
        response = self.client.post(
            f"/admin/users/{user_id}/delete", data={"_csrf_token": self.csrf()}, follow_redirects=True
        )
        return response, html.unescape(response.get_data(as_text=True))

    def test_a_super_admin_can_delete_an_account(self):
        bob = self.add_user("bob", "bob@example.com")
        self.assertIn("Delete account", self.client.get("/admin/?section=people").get_data(as_text=True))
        _response, page = self.delete(bob)
        self.assertIn("Deleted the account of bob", page)
        self.assertEqual([user["id"] for user in self.users()], [self.root])

    def test_a_deleted_person_is_signed_out_and_cannot_sign_in_again(self):
        bob = self.add_user("bob", "bob@example.com", "bob-sub")
        other = self.app.test_client()
        with other.session_transaction() as session:
            session["user_id"] = bob
        self.delete(bob)
        self.assertIn("/auth/login", other.get("/").headers["Location"])
        self.google_callback(self.google_claims(sub="bob-sub", email="bob@example.com"))
        self.assertEqual(len(self.users()), 1)

    def test_not_your_own_account(self):
        _response, page = self.delete(self.root)
        self.assertIn("can't delete your own account", page)
        self.assertEqual(len(self.users()), 1)

    def test_not_while_they_own_sites_or_sources(self):
        bob = self.add_user("bob", "bob@example.com")
        with self.app.app_context():
            database = get_db()
            database.execute(
                "INSERT INTO repositories (user_id, name, url, local_path) VALUES (?, 'r', 'u', '/x')",
                (bob,),
            )
            database.commit()
        _response, page = self.delete(bob)
        self.assertIn("still owns 1 Git source", page)
        self.assertEqual(len(self.users()), 2)

    def test_only_super_admins(self):
        bob = self.add_user("bob", "bob@example.com")
        carol = self.add_user("carol", "carol@example.com")
        self.login_user(carol)
        response, _page = self.delete(bob)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.users()), 3)

    def test_unknown_account(self):
        response, _page = self.delete(999)
        self.assertEqual(response.status_code, 404)


class ServerAlertsForAdminsOnlyTests(Case):
    def test_scheduled_checks_notice_is_for_super_admins(self):
        self.app.config["AUTO_REFRESH_ENABLED"] = False
        text = "Scheduled update checks are turned off"
        self.login_user(self.add_user("bob", "bob@example.com"))
        self.assertNotIn(text, self.client.get("/?view=sources").get_data(as_text=True))
        self.login_user(self.add_user("root", "root@example.com", is_admin=True))
        self.assertIn(text, self.client.get("/?view=sources").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
