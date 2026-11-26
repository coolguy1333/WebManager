"""Apps given the wrong public address scheme (PUBLIC_URL http:// while the
sites are really served over https, as behind Cloudflare Tunnel) must be
obvious: a warning on the System page, and each app's page shows the address
it was started with and offers a restart when that is out of date."""

import unittest

from werkzeug.middleware.proxy_fix import ProxyFix

from tests import test_app as base
from tests import test_apps

HTTPS_CF = {"Cf-Visitor": '{"scheme":"https"}'}


class SystemPageSchemeWarningTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user

    def setUp(self):
        self.setUp_base()
        self.app.config["SITE_PUBLIC_SCHEME"] = "http"
        self.login_user(self.add_user("root", is_admin=True))

    def page(self, headers=None):
        return self.client.get("/admin/?section=updates", headers=headers or {}).get_data(as_text=True)

    def test_https_through_cloudflare_with_http_apps_is_flagged_with_the_fix(self):
        page = self.page(HTTPS_CF)
        self.assertIn('id="public-scheme-warning"', page)
        self.assertIn("Apps are given http:// addresses", page)
        self.assertIn("WEBMANAGER_SITE_PUBLIC_SCHEME=https", page)
        self.assertIn("/etc/webmanager/webmanager.env", page)
        self.assertIn("sudo systemctl restart webmanager", page)

    def test_https_from_a_trusted_proxy_is_flagged(self):
        self.app.config["TRUST_PROXY"] = True
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        self.assertIn('id="public-scheme-warning"', self.page({"X-Forwarded-Proto": "https"}))

    def test_x_forwarded_proto_is_not_believed_without_trust_proxy(self):
        self.assertNotIn('id="public-scheme-warning"', self.page({"X-Forwarded-Proto": "https"}))

    def test_no_warning_over_plain_http(self):
        self.assertNotIn('id="public-scheme-warning"', self.page())
        self.assertNotIn('id="public-scheme-warning"', self.page({"Cf-Visitor": '{"scheme":"http"}'}))
        self.assertNotIn('id="public-scheme-warning"', self.page({"Cf-Visitor": "not json"}))

    def test_no_warning_once_the_scheme_is_https(self):
        self.app.config["SITE_PUBLIC_SCHEME"] = "https"
        self.assertNotIn('id="public-scheme-warning"', self.page(HTTPS_CF))

    def test_only_on_the_system_page(self):
        page = self.client.get("/admin/", headers=HTTPS_CF).get_data(as_text=True)
        self.assertNotIn('id="public-scheme-warning"', page)


class AppPagePublicUrlTests(test_apps.AppHostingTests):
    """Runs on the app test fixture (a fake container runtime)."""

    def running_app(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)
        return site_id

    def test_the_page_shows_the_address_the_app_was_started_with(self):
        site_id = self.running_app()
        page = self.client.get(f"/sites/{site_id}").get_data(as_text=True)
        self.assertIn("<code>https://status.webmanager.example</code>", page)
        self.assertNotIn("Restart needed", page)

    def test_a_changed_scheme_says_restart_needed_and_offers_a_restart(self):
        site_id = self.running_app()
        self.app.config["SITE_PUBLIC_SCHEME"] = "http"  # changed since it started

        page = self.client.get(f"/sites/{site_id}").get_data(as_text=True)
        self.assertIn("Restart needed to use http://status.webmanager.example", page)
        self.assertIn("<code>PUBLIC_URL=https://status.webmanager.example</code>", page)
        self.assertIn(f'action="/sites/{site_id}/restart"', page)

        self.start_now(site_id, force=True)  # what the button does
        page = self.client.get(f"/sites/{site_id}").get_data(as_text=True)
        self.assertNotIn("Restart needed", page)
        self.assertIn("<code>http://status.webmanager.example</code>", page)

    def test_the_runtime_reports_both_addresses(self):
        site_id = self.running_app()
        self.app.config["SITE_PUBLIC_SCHEME"] = "http"
        details = self.app.extensions["runtime_manager"]
        with self.app.test_request_context():
            from webmanager.db import get_db

            site = get_db().execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
            info = details.app_details(site)
        self.assertTrue(info["public_url_stale"])
        self.assertEqual(info["public_url"], "https://status.webmanager.example")
        self.assertEqual(info["public_url_now"], "http://status.webmanager.example")

    def test_a_stopped_app_shows_no_comparison(self):
        site_id = self.running_app()
        with self.app.app_context():
            self.app.extensions["runtime_manager"].stop_site(site_id)
        self.app.config["SITE_PUBLIC_SCHEME"] = "http"
        page = self.client.get(f"/sites/{site_id}").get_data(as_text=True)
        self.assertNotIn("Restart needed", page)


# The fixture's own tests already run in test_apps; don't run them twice.
for _name in [name for name in dir(test_apps.AppHostingTests) if name.startswith("test_")]:
    if _name not in AppPagePublicUrlTests.__dict__:
        setattr(AppPagePublicUrlTests, _name, None)

if __name__ == "__main__":
    unittest.main()
