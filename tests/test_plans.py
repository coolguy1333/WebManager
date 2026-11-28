"""Plans (Starter, Basic, Advanced…) set how many sites, apps and Git sources
someone may have and whether they may host apps; super admins manage them,
everyone sees their own on Your account."""

import html
import unittest

from tests import test_app as base
from webmanager import plans, quotas
from webmanager.db import get_db


class Case(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.root = self.add_user("root", "root@example.com", is_admin=True)
        self.bob = self.add_user("bob", "bob@example.com")
        self.login_user(self.root)

    def db(self):
        return get_db()

    def plan_id(self, name):
        with self.app.app_context():
            return self.db().execute("SELECT id FROM plans WHERE name = ?", (name,)).fetchone()["id"]

    def limit(self, user_id, kind):
        with self.app.app_context():
            return quotas.limit_for(self.db(), user_id, kind)

    def post(self, path, **data):
        response = self.client.post(path, data={"_csrf_token": self.csrf(), **data}, follow_redirects=True)
        return response, html.unescape(response.get_data(as_text=True))


class SeedTests(Case):
    def test_starter_basic_and_advanced_with_basic_as_the_default(self):
        with self.app.app_context():
            names = [plan["name"] for plan in plans.all_plans(self.db())]
            default = plans.default_plan_id(self.db())
        self.assertEqual(names, ["Starter", "Basic", "Advanced"])
        self.assertEqual(default, self.plan_id("Basic"))

    def test_basic_keeps_todays_default_limits_and_grants_nothing_new(self):
        self.assertEqual(self.limit(self.bob, "sites"), quotas.DEFAULT_MAX_SITES)
        self.assertEqual(self.limit(self.bob, "sources"), quotas.DEFAULT_MAX_SOURCES)
        self.login_user(self.bob)
        page = self.client.get("/account").get_data(as_text=True)
        self.assertIn("Basic plan", page)

    def test_seeding_runs_once(self):
        with self.app.app_context():
            database = self.db()
            database.execute("DELETE FROM plans WHERE name = 'Starter'")
            plans.seed(database)
            self.assertIsNone(database.execute("SELECT 1 FROM plans WHERE name = 'Starter'").fetchone())


class LimitTests(Case):
    def test_a_persons_plan_sets_their_limits(self):
        self.post(f"/admin/users/{self.bob}", is_active="on", role="member", plan_id=str(self.plan_id("Starter")),
                  username="bob", display_name="Bob")
        self.assertEqual(self.limit(self.bob, "sites"), 1)
        self.post(f"/admin/users/{self.bob}", is_active="on", role="member", plan_id=str(self.plan_id("Advanced")),
                  username="bob", display_name="Bob")
        self.assertEqual(self.limit(self.bob, "sites"), 20)

    def test_custom_limits_override_the_plan(self):
        self.post(f"/admin/users/{self.bob}", is_active="on", role="member", plan_id=str(self.plan_id("Starter")),
                  username="bob", display_name="Bob", max_sites="7", max_sources="", max_apps="")
        self.assertEqual(self.limit(self.bob, "sites"), 7)
        self.assertEqual(self.limit(self.bob, "sources"), 1)  # the plan's

    def test_changing_a_plan_changes_everyone_on_it(self):
        basic = self.plan_id("Basic")
        self.post(f"/admin/plans/{basic}", name="Basic", max_sites="9", max_sources="4", max_apps="0")
        self.assertEqual(self.limit(self.bob, "sites"), 9)
        self.assertIsNone(self.limit(self.bob, "apps"), "0 = unlimited")

    def test_quota_is_enforced_from_the_plan(self):
        starter = self.plan_id("Starter")
        with self.app.app_context():
            database = self.db()
            database.execute("UPDATE users SET plan_id = ? WHERE id = ?", (starter, self.bob))
            database.execute(
                "INSERT INTO repositories (user_id, name, url, local_path) VALUES (?, 'r', 'u', '/x')", (self.bob,)
            )
            database.commit()
            self.assertIn("limit", quotas.check(database, self.bob, "sources"))

    def test_super_admins_are_never_limited(self):
        self.assertIsNone(self.limit(self.root, "sites"))


class AppHostingByPlanTests(Case):
    def test_a_plan_can_let_its_people_host_apps(self):
        self.app.config["APPS_ENABLED"] = True
        self.login_user(self.bob)
        self.assertNotIn("Deploy an app", self.client.get("/?view=apps").get_data(as_text=True))
        with self.app.app_context():
            database = self.db()
            database.execute("UPDATE users SET plan_id = ? WHERE id = ?", (self.plan_id("Advanced"), self.bob))
            database.commit()
        self.assertIn("Deploy an app", self.client.get("/?view=apps").get_data(as_text=True))


class ManagePlansTests(Case):
    def test_add_rename_and_make_default(self):
        _response, page = self.post("/admin/plans", name="Pro", max_sites="50", max_sources="20", max_apps="5",
                                    can_host_apps="1")
        self.assertIn("Added the Pro plan", page)
        pro = self.plan_id("Pro")
        self.post(f"/admin/plans/{pro}/default")
        with self.app.app_context():
            self.assertEqual(plans.default_plan_id(self.db()), pro)
        self.assertEqual(self.limit(self.bob, "sites"), 50)  # bob is on the default plan

    def test_bad_values_and_duplicate_names_are_refused(self):
        for data, message in (
            ({"name": "", "max_sites": "1", "max_sources": "1", "max_apps": "1"}, "Give the plan a name"),
            ({"name": "X", "max_sites": "-1", "max_sources": "1", "max_apps": "1"}, "Sites: must be between"),
            ({"name": "X", "max_sites": "lots", "max_sources": "1", "max_apps": "1"}, "Sites: enter a whole number"),
            ({"name": "basic", "max_sites": "1", "max_sources": "1", "max_apps": "1"}, "already a plan called"),
        ):
            with self.subTest(data=data):
                _response, page = self.post("/admin/plans", **data)
                self.assertIn(message, page)

    def test_deleting_a_plan_moves_its_people_to_the_default(self):
        starter = self.plan_id("Starter")
        with self.app.app_context():
            database = self.db()
            database.execute("UPDATE users SET plan_id = ? WHERE id = ?", (starter, self.bob))
            database.commit()
        _response, page = self.post(f"/admin/plans/{starter}/delete")
        self.assertIn("1 person is on the default plan now", page)
        self.assertEqual(self.limit(self.bob, "sites"), quotas.DEFAULT_MAX_SITES)

    def test_the_default_plan_cannot_be_deleted(self):
        _response, page = self.post(f"/admin/plans/{self.plan_id('Basic')}/delete")
        self.assertIn("can't be deleted", page)

    def test_only_super_admins(self):
        self.login_user(self.bob)
        for path in ("/admin/plans", f"/admin/plans/{self.plan_id('Basic')}",
                     f"/admin/plans/{self.plan_id('Basic')}/default", f"/admin/plans/{self.plan_id('Starter')}/delete"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"_csrf_token": self.csrf(), "name": "Hacked", "max_sites": "0",
                                                         "max_sources": "0", "max_apps": "0"})
                self.assertEqual(response.status_code, 403)
        self.assertNotEqual(self.client.get("/admin/?section=plans").status_code, 200)

    def test_a_person_cannot_change_their_own_plan_or_role(self):
        self.login_user(self.bob)
        self.client.post(f"/admin/users/{self.bob}", data={
            "_csrf_token": self.csrf(), "role": "admin", "is_admin": "on", "plan_id": str(self.plan_id("Advanced")),
            "max_sites": "0", "is_active": "on",
        })
        with self.app.app_context():
            row = self.db().execute("SELECT is_admin, plan_id, max_sites FROM users WHERE id = ?", (self.bob,)).fetchone()
        self.assertEqual((row["is_admin"], row["plan_id"], row["max_sites"]), (0, None, None))

    def test_the_pages_render(self):
        self.assertIn("Add a plan", self.client.get("/admin/?section=plans").get_data(as_text=True))
        people = self.client.get("/admin/?section=people").get_data(as_text=True)
        self.assertIn('name="plan_id"', people)
        self.assertIn('name="role"', people)


class AccountPageTests(Case):
    def test_shows_your_plan_and_usage_and_saves_your_name(self):
        self.login_user(self.bob)
        page = html.unescape(self.client.get("/account").get_data(as_text=True))
        self.assertIn("Basic plan", page)
        self.assertIn(f"of {quotas.DEFAULT_MAX_SITES}", page)
        self.post("/account", display_name="  Bobby  B ")
        with self.app.app_context():
            self.assertEqual(self.db().execute("SELECT display_name FROM users WHERE id = ?", (self.bob,)).fetchone()[0],
                             "Bobby B")

    def test_only_your_own_name(self):
        self.login_user(self.bob)
        self.post("/account", display_name="X", is_admin="1", plan_id="3", max_sites="0", user_id=str(self.root))
        with self.app.app_context():
            row = self.db().execute("SELECT * FROM users WHERE id = ?", (self.root,)).fetchone()
            bob = self.db().execute("SELECT * FROM users WHERE id = ?", (self.bob,)).fetchone()
        self.assertNotEqual(row["display_name"], "X")
        self.assertEqual((bob["is_admin"], bob["plan_id"], bob["max_sites"]), (0, None, None))


if __name__ == "__main__":
    unittest.main()
