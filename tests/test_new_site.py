"""Deploying is one page per step: paste a repository on New site, then
WebManager works out what's in it (an app, a website, or both) and picks the
best match; one name and one address, and it's deployed."""

import json
import re
import unittest
from unittest.mock import patch

from tests import test_app as base
from tests import test_apps
from webmanager.db import get_db
from webmanager.services import RuntimeManager



class NewSitePageTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.user = self.add_user("alice", "alice@example.com")
        self.login_user(self.user)

    def test_one_field_and_the_connected_repositories(self):
        root = base.Path(self.temp_directory.name) / "repo" / "public"
        root.mkdir(parents=True)
        repository_id = self.add_repository(self.user, root)
        page = self.client.get("/new").get_data(as_text=True)
        self.assertIn('name="repository_url"', page)
        self.assertIn("works out whether it's a website or an app", page)
        self.assertIn(f"/repositories/{repository_id}/select", page)

    def test_the_new_site_buttons_go_there(self):
        page = self.client.get("/?view=sites").get_data(as_text=True)
        self.assertIn('href="/new"', page)


class ChoicesTests(unittest.TestCase):
    setUp_base = test_apps.AppHostingTests.setUp_base
    tearDown = test_apps.AppHostingTests.tearDown
    csrf = test_apps.AppHostingTests.csrf
    add_user = test_apps.AppHostingTests.add_user
    login_user = test_apps.AppHostingTests.login_user
    add_repository = test_apps.AppHostingTests.add_repository
    owner = test_apps.AppHostingTests.owner
    add_domain = test_apps.AppHostingTests.add_domain
    setUp = test_apps.AppHostingTests.setUp

    def choices(self, repository_id):
        page = self.client.get(f"/repositories/{repository_id}/select").get_data(as_text=True)
        found = re.findall(r'<input type="radio" name="choice" value="([^"]+)"([^>]*)>', page)
        return page, [(value, "checked" in attrs, "disabled" in attrs) for value, attrs in found]

    def test_an_app_and_a_website_in_one_repository_the_app_is_the_best_match(self):
        self.add_domain("example.com")
        (self.root / "public").mkdir()
        (self.root / "public" / "index.html").write_text("hi", encoding="utf-8")
        _user, repository_id = self.owner()
        page, choices = self.choices(repository_id)
        self.assertEqual(choices, [("app:.", True, False), ("site:public", False, False)])
        self.assertIn("Best match", page)
        self.assertIn("Deploy app", page)

    def test_without_app_hosting_the_website_is_picked_and_the_app_says_why(self):
        self.app.config["APPS_ENABLED"] = False
        self.add_domain("example.com")
        (self.root / "public").mkdir()
        (self.root / "public" / "index.html").write_text("hi", encoding="utf-8")
        _user, repository_id = self.owner()
        page, choices = self.choices(repository_id)
        self.assertEqual(choices, [("app:.", False, True), ("site:public", True, False)])
        self.assertIn("App hosting is turned off on this server", page)

    def test_a_dockerfile_without_webmanager_json_is_explained(self):
        (self.root / "webmanager.json").unlink()
        self.add_domain("example.com")
        _user, repository_id = self.owner()
        page, choices = self.choices(repository_id)
        self.assertEqual(choices, [("app:.", False, True)])
        self.assertIn("no webmanager.json", page)
        self.assertNotIn("Name and address", page)  # nothing can be deployed

    def test_deploying_the_app(self):
        self.add_domain("example.com")
        _user, repository_id = self.owner()
        with patch.object(RuntimeManager, "start_app_async", return_value=True):
            response = self.client.post(f"/repositories/{repository_id}/deploy-choice", data={
                "_csrf_token": self.csrf(), "choice": "app:.", "site_name": "Status", "slug": "status",
            })
        self.assertIn(response.status_code, (302, 303))
        with self.app.app_context():
            site = get_db().execute("SELECT * FROM sites").fetchone()
        self.assertEqual((site["kind"], site["slug"], site["folder"]), ("app", "status", "."))


class DeployWebsiteTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.user = self.add_user("alice", "alice@example.com")
        self.login_user(self.user)
        root = base.Path(self.temp_directory.name) / "repo"
        (root / "dist").mkdir(parents=True)
        (root / "dist" / "index.html").write_text("built", encoding="utf-8")
        (root / "index.html").write_text("source", encoding="utf-8")
        self.repository_id = self.add_repository(self.user, root / "dist")
        with self.app.app_context():
            database = get_db()
            self.domain_id = database.execute("INSERT INTO domains (name) VALUES ('example.com')").lastrowid
            database.commit()

    def deploy(self, **form):
        with patch("webmanager.services.RuntimeManager.start_site", return_value="nginx"):
            return self.client.post(f"/repositories/{self.repository_id}/deploy-choice", data={
                "_csrf_token": self.csrf(), "domain_id": str(self.domain_id), **form,
            }, follow_redirects=True).get_data(as_text=True)

    def site(self):
        with self.app.app_context():
            return get_db().execute("SELECT * FROM sites").fetchone()

    def test_the_build_output_is_the_best_match(self):
        page = self.client.get(f"/repositories/{self.repository_id}/select").get_data(as_text=True)
        values = re.findall(r'name="choice" value="([^"]+)"', page)
        self.assertEqual(values, ["site:dist", "site:."])
        self.assertIn('value="site:dist" checked', page)

    def test_a_website_with_its_own_subdomain(self):
        page = self.deploy(choice="site:dist", site_name="Docs", slug="docs", spa_fallback="on")
        self.assertIn("https://docs.example.com", page)
        site = self.site()
        self.assertEqual((site["slug"], site["folder"], site["spa_fallback"], site["use_domain_root"]),
                         ("docs", "dist", 1, 0))

    def test_a_website_on_the_bare_domain_without_spa(self):
        self.deploy(choice="site:dist", site_name="Main", use_domain_root="on")
        site = self.site()
        self.assertEqual((site["use_domain_root"], site["spa_fallback"]), (1, 0))

    def test_nothing_chosen(self):
        page = self.deploy(site_name="Main")
        self.assertIn("Choose what to deploy.", page)
        self.assertIsNone(self.site())

    def test_someone_elses_repository_is_refused(self):
        self.login_user(self.add_user("mallory", "m@example.com"))
        response = self.client.post(f"/repositories/{self.repository_id}/deploy-choice", data={
            "_csrf_token": self.csrf(), "choice": "site:dist", "site_name": "X",
        })
        self.assertIn(response.status_code, (403, 404))
        self.assertIsNone(self.site())


if __name__ == "__main__":
    unittest.main()
