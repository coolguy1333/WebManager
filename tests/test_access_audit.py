"""Nobody gets at anything they shouldn't: every route in the app is tried by
people with too little access, and each must be refused with nothing in the
database changed. New routes are covered automatically: they are read from
the app's own URL map, so one added without a check fails here."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_app as base
from webmanager.db import get_db
from webmanager.services import RuntimeManager

# Routes anyone signed in may use on their own behalf (their own account,
# signing out, connecting their own repository) or that need no session at
# all (sign-in, health, the token-only server-to-server and deploy-hook
# endpoints, checked separately below).
SELF_SERVICE = {
    "deployments.account", "auth.logout", "deployments.inspect_repository",
    "deployments.dashboard", "deployments.docs", "deployments.new_site", "deployments.analytics",
    "deployments.apps_stats_json", "static", "health",
}
PUBLIC_OR_TOKEN = {
    "auth.login", "auth.google_login", "auth.google_callback", "auth.handoff", "auth.handoff_complete",
    "auth.register", "auth.via_primary", "hooks.deploy",
    "mesh.mesh_control", "mesh.mesh_join_info", "mesh.mesh_register", "mesh.mesh_secret_key",
    "mesh.mesh_status", "mesh.mesh_update",
    "replication.db_snapshot", "replication.find_or_create_user", "data_replication.data_snapshot",
}
TABLES = ("users", "plans", "groups", "user_groups", "group_permissions", "sites", "repositories", "domains",
          "dashboard_domains", "pools", "pool_sites", "pool_acl", "site_acl", "app_settings", "blocked_domains")


class AccessAuditTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository
    add_site = base.WebManagerTestCase.add_site
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()
        self.app.config["APPS_ENABLED"] = True
        self.admin = self.add_user("root", "root@example.com", is_admin=True)
        self.alice = self.add_user("alice", "alice@example.com")
        self.mallory = self.add_user("mallory", "mallory@example.com")
        self.viewer = self.add_user("victor", "victor@example.com")
        site_root = Path(self.temp_directory.name) / "alice-repo" / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("secret site", encoding="utf-8")
        self.repository = self.add_repository(self.alice, site_root)
        self.site = self.add_site(self.alice, self.repository, site_root)
        with self.app.app_context():
            database = get_db()
            self.domain = database.execute("INSERT INTO domains (name) VALUES ('other.example')").lastrowid
            self.group = database.execute("INSERT INTO groups (name) VALUES ('Staff')").lastrowid
            self.pool = database.execute("INSERT INTO pools (name) VALUES ('Pool')").lastrowid
            self.plan = database.execute("SELECT id FROM plans LIMIT 1").fetchone()["id"]
            self.dashboard_domain = database.execute(
                "INSERT INTO dashboard_domains (name, is_primary) VALUES ('panel.example', 0)"
            ).lastrowid
            # victor may look at alice's site, nothing more.
            database.execute("INSERT INTO site_acl (site_id, user_id, role) VALUES (?, ?, 'viewer')",
                             (self.site, self.viewer))
            database.commit()
        self.ids = {
            "site_id": self.site, "repository_id": self.repository, "user_id": self.alice,
            "domain_id": self.domain, "group_id": self.group, "pool_id": self.pool, "plan_id": self.plan,
            "name": "webmanager-backup.tar.gz", "token": "not-a-real-token", "filename": "app.css",
        }
        patchers = [
            patch.object(RuntimeManager, "start_site", return_value="nginx"),
            patch.object(RuntimeManager, "stop_site", return_value=None),
            patch.object(RuntimeManager, "start_app_async", return_value=True),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers -----------------------------------------------------------
    def snapshot(self):
        with self.app.app_context():
            database = get_db()
            return {
                table: [tuple(row) for row in database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
                for table in TABLES
            }

    def routes(self):
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in SELF_SERVICE or rule.endpoint in PUBLIC_OR_TOKEN:
                continue
            url = rule.rule
            for argument in rule.arguments:
                url = url.replace(f"<int:{argument}>", str(self.ids[argument]))
                url = url.replace(f"<path:{argument}>", str(self.ids[argument]))
                url = url.replace(f"<{argument}>", str(self.ids[argument]))
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                yield rule.endpoint, method, url

    def refused(self, response):
        if response.status_code in (401, 403, 404):
            return True
        # A redirect to sign-in, or back with an error, is a refusal too, as
        # long as nothing changed (checked by the caller).
        return response.status_code in (302, 303) and "/auth/login" in response.headers.get("Location", "")

    def assert_everything_refused(self, user_id, allowed=()):
        self.login_user(user_id)
        before = self.snapshot()
        token = self.csrf()
        problems = []
        for endpoint, method, url in self.routes():
            if endpoint in allowed:
                continue
            if method == "GET":
                response = self.client.get(url)
            else:
                response = self.client.post(url, data={
                    "_csrf_token": token, "name": "hacked", "role": "admin", "is_admin": "on",
                    "is_active": "on", "plan_id": str(self.plan), "max_sites": "0", "enabled": "1",
                    "url": "http://evil.example", "WEBMANAGER_BACKUP_KEEP": "1", "permissions": "users.manage",
                    "groups": str(self.group), "domain": "evil.example", "site_name": "hacked",
                    "config": "server {}", "commit": "a" * 40, "users": str(user_id),
                })
            if not self.refused(response):
                problems.append(f"{method} {url} ({endpoint}) answered {response.status_code}")
        self.assertEqual(problems, [])
        self.assertEqual(self.snapshot(), before, "something changed")

    # -- tests -------------------------------------------------------------
    def test_someone_with_no_access_is_refused_everywhere(self):
        self.assert_everything_refused(self.mallory)

    def test_view_access_to_a_site_is_view_only(self):
        self.assert_everything_refused(self.viewer, allowed={
            "deployments.site_detail", "deployments.site_status_json",
        })
        self.login_user(self.viewer)
        self.assertEqual(self.client.get(f"/sites/{self.site}").status_code, 200)

    def test_a_people_manager_cannot_make_anyone_a_super_admin_or_touch_the_server(self):
        with self.app.app_context():
            database = get_db()
            database.execute("INSERT INTO group_permissions (group_id, permission_code) VALUES (?, 'users.manage')",
                             (self.group,))
            database.execute("INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (self.mallory, self.group))
            database.commit()
        self.login_user(self.mallory)
        self.client.post(f"/admin/users/{self.alice}", data={
            "_csrf_token": self.csrf(), "is_active": "on", "role": "admin", "is_admin": "on",
            "plan_id": str(self.plan), "max_sites": "0", "username": "pwned",
        })
        self.client.post(f"/admin/users/{self.mallory}", data={
            "_csrf_token": self.csrf(), "is_active": "on", "role": "admin", "is_admin": "on",
        })
        with self.app.app_context():
            rows = {row["id"]: row for row in get_db().execute("SELECT * FROM users").fetchall()}
        self.assertEqual(rows[self.alice]["is_admin"], 0)
        self.assertEqual(rows[self.mallory]["is_admin"], 0)
        self.assertEqual(rows[self.alice]["username"], "alice")
        self.assertIsNone(rows[self.alice]["plan_id"])
        self.assertIsNone(rows[self.alice]["max_sites"])
        for path in ("/admin/?section=updates", "/admin/domains", "/admin/activity", "/admin/?section=plans"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertFalse(response.status_code == 200 and b"System" in response.data and path.endswith("updates"))
                self.assertNotEqual(response.status_code, 200) if "section=" not in path else None

    def test_what_a_plan_allows_cannot_be_handed_on_through_a_team(self):
        with self.app.app_context():
            database = get_db()
            database.execute("INSERT INTO group_permissions (group_id, permission_code) VALUES (?, 'groups.manage')",
                             (self.group,))
            database.execute("INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (self.mallory, self.group))
            apps_plan = database.execute("SELECT id FROM plans WHERE can_host_apps = 1").fetchone()["id"]
            database.execute("UPDATE users SET plan_id = ? WHERE id = ?", (apps_plan, self.mallory))
            database.commit()
        self.login_user(self.mallory)
        response = self.client.post("/admin/groups", data={
            "_csrf_token": self.csrf(), "name": "App friends", "permission_profile": "app_host",
        })
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT 1 FROM groups WHERE name = 'App friends'").fetchone())

    def test_analytics_and_lists_only_show_what_you_can_see(self):
        log = Path(self.app.config["NGINX_ROOT"]) / "access.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"time": base.recent(), "host": "demo.webmanager.example", "status": 200,
                                   "bytes": 1, "client": "203.0.113.5", "uri": "/alice-private-page"}) + "\n")
        self.login_user(self.mallory)
        for path in ("/analytics", "/analytics?site=" + str(self.site), "/", "/?view=sites", "/?view=sources",
                     "/?view=apps", "/apps/stats.json", "/new"):
            with self.subTest(path=path):
                page = self.client.get(path).get_data(as_text=True)
                self.assertNotIn("alice-private-page", page)
                self.assertNotIn("demo.webmanager.example", page)
                self.assertNotIn("https://example.com/demo.git", page)
        # The viewer sees it.
        self.login_user(self.viewer)
        self.assertIn("alice-private-page", self.client.get("/analytics").get_data(as_text=True))

    def test_no_admin_navigation_or_server_details_for_members(self):
        self.login_user(self.mallory)
        for path in ("/", "/docs", "/account", "/new", "/analytics"):
            with self.subTest(path=path):
                page = self.client.get(path).get_data(as_text=True)
                # (Apps are told WEBMANAGER_APP_ID, which the app docs mention.)
                for text in ("Administration", "People &amp; access", "/admin/", "webmanager.env", "sudo ",
                             "WEBMANAGER_PEER", "WEBMANAGER_GOOGLE", "System →", "Only super admins"):
                    self.assertNotIn(text, page)

    def test_token_only_endpoints_refuse_a_signed_in_person(self):
        self.login_user(self.mallory)
        for method, path in (("GET", "/mesh/secret-key"), ("GET", "/replication/db-snapshot"),
                             ("GET", "/replication/data-snapshot"), ("GET", "/mesh/join-info"),
                             ("POST", "/mesh/control"), ("POST", "/mesh/update"), ("POST", "/mesh/register"),
                             ("POST", "/replication/find-or-create-user"), ("POST", "/hooks/deploy/guess")):
            with self.subTest(path=path):
                response = self.client.open(path, method=method, json={"action": "apps_on"})
                self.assertIn(response.status_code, (401, 403, 404))


if __name__ == "__main__":
    unittest.main()
