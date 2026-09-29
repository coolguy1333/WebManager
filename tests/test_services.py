"""Bringing hosted sites back after a restart or a replica's data sync."""

import unittest
from pathlib import Path

from webmanager.db import get_db
from webmanager.nginx import build_site_config

from tests import test_app as base


class RestoreSitesTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    add_repository = base.WebManagerTestCase.add_repository
    add_site = base.WebManagerTestCase.add_site

    def setUp(self):
        self.setUp_base()
        root = Path(self.temp_directory.name)
        self.calls = root / "nginx-calls.log"
        self.fail_marker = root / "nginx-fails"
        script = root / "fake-nginx"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.calls}"\n'
            f'if [ -e "{self.fail_marker}" ]; then echo "boom" >&2; exit 1; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.app.config["NGINX_BINARY"] = str(script)
        self.site_root = root / "repositories" / "1" / "1"
        self.site_root.mkdir(parents=True)
        (self.site_root / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        self.user_id = self.add_user("owner", is_admin=True)
        self.repository_id = self.add_repository(self.user_id, self.site_root)

    def make_sites(self, count, status="running"):
        ids = []
        for index in range(count):
            slug = f"site{index}"
            port = 43100 + index
            site_id = self.add_site(
                self.user_id, self.repository_id, self.site_root,
                port=port, status=status, runtime_backend="nginx",
            ) if index == 0 else self._extra_site(slug, port, status)
            if index == 0:
                self._rename(site_id, slug, port)
            ids.append(site_id)
        return ids

    def _rename(self, site_id, slug, port):
        config = build_site_config(
            "Demo", self.site_root, "index.html", port, True,
            f"{slug}.webmanager.example", 43099,
        )
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET slug = ?, nginx_config = ? WHERE id = ?",
                (slug, config, site_id),
            )
            database.commit()

    def _extra_site(self, slug, port, status):
        config = build_site_config(
            "Demo", self.site_root, "index.html", port, True,
            f"{slug}.webmanager.example", 43099,
        )
        with self.app.app_context():
            database = get_db()
            domain_id = database.execute(
                "SELECT id FROM domains WHERE is_default = 1"
            ).fetchone()["id"]
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, domain_id, name, slug, folder,
                    document_root, index_file, port, spa_fallback, nginx_config,
                    status, runtime_backend
                ) VALUES (?, ?, ?, ?, ?, '.', ?, 'index.html', ?, 1, ?, ?, 'nginx')
                """,
                (
                    self.user_id, self.repository_id, domain_id, slug, slug,
                    str(self.site_root), port, config, status,
                ),
            )
            database.commit()
            return cursor.lastrowid

    def statuses(self):
        with self.app.app_context():
            return {
                row["slug"]: row["status"]
                for row in get_db().execute("SELECT slug, status FROM sites")
            }

    def nginx_calls(self):
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_many_running_sites_share_one_config_test_and_one_reload(self):
        self.make_sites(4)

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertEqual(
            self.statuses(), {f"site{i}": "running" for i in range(4)}
        )
        calls = self.nginx_calls()
        self.assertEqual(sum(" -t" in call for call in calls), 1, calls)
        self.assertEqual(len(calls), 2, calls)  # one syntax test, one start/reload

    def test_a_site_with_an_unsafe_config_is_marked_failed_without_stopping_the_rest(self):
        ids = self.make_sites(3)
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET nginx_config = replace(nginx_config, 'disable_symlinks on', "
                "'disable_symlinks off') WHERE id = ?",
                (ids[1],),
            )
            database.commit()

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        statuses = self.statuses()
        self.assertEqual(statuses["site1"], "error")
        self.assertEqual(statuses["site0"], "running")
        self.assertEqual(statuses["site2"], "running")

    def test_when_nginx_rejects_the_config_each_site_reports_its_own_failure(self):
        self.make_sites(2)
        self.fail_marker.write_text("x", encoding="utf-8")

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertEqual(self.statuses(), {"site0": "error", "site1": "error"})

    def test_stopped_sites_are_left_alone(self):
        self.make_sites(2, status="stopped")

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertEqual(self.statuses(), {"site0": "stopped", "site1": "stopped"})
        self.assertEqual(self.nginx_calls(), [])

    def test_restoring_the_gateway_does_nothing_without_nginx(self):
        self.app.config["NGINX_BINARY"] = "definitely-not-installed-nginx"
        self.app.extensions["runtime_manager"].restore_gateway()  # must not raise


if __name__ == "__main__":
    unittest.main()
