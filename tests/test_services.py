"""Bringing hosted sites back after a restart or a replica's data sync."""

import unittest
from pathlib import Path
from unittest.mock import patch

from webmanager.db import get_db
from webmanager.nginx import build_site_config, drop_ipv6_listeners

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

    def test_a_config_mirrored_from_a_server_with_another_gateway_port_is_repointed(self):
        [site_id] = self.make_sites(1)
        foreign = build_site_config(
            "Demo", self.site_root, "index.html", 43100, True,
            ["site0.webmanager.example"], 43050,  # the other server's gateway port
        )
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE sites SET nginx_config = ? WHERE id = ?", (foreign, site_id))
            database.commit()

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertEqual(self.statuses(), {"site0": "running"})
        written = self.written_configs()["1-site0.conf"]
        self.assertIn("127.0.0.1:43099", written)  # this server's gateway
        self.assertNotIn("43050", written)

    def test_stopped_sites_are_left_alone(self):
        self.make_sites(2, status="stopped")

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertEqual(self.statuses(), {"site0": "stopped", "site1": "stopped"})
        self.assertEqual(self.nginx_calls(), [])

    def written_configs(self):
        root = Path(self.app.config["NGINX_ROOT"])
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in [root / "nginx.conf", *sorted((root / "conf.d").glob("*.conf"))]
        }

    def test_ipv6_listeners_are_left_out_when_the_host_has_no_ipv6(self):
        self.make_sites(2)
        with patch("webmanager.services.ipv6_loopback_available", return_value=False):
            self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        configs = self.written_configs()
        self.assertGreaterEqual(len(configs), 3)
        for name, text in configs.items():
            self.assertNotIn("[::", text, name)
            if name != "nginx.conf":
                self.assertIn("listen 127.0.0.1:", text, name)
        # The stored config stays dual-stack so it still works on a server that has IPv6.
        with self.app.app_context():
            stored = get_db().execute("SELECT nginx_config FROM sites LIMIT 1").fetchone()[0]
        self.assertIn("[::1]", stored)

    def test_ipv6_listeners_are_kept_when_the_host_has_ipv6(self):
        self.make_sites(1)
        with patch("webmanager.services.ipv6_loopback_available", return_value=True):
            self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        self.assertTrue(any("[::1]" in text for text in self.written_configs().values()))

    def test_drop_ipv6_listeners_only_removes_ipv6_listen_lines(self):
        config = (
            "server {\n"
            "    listen 127.0.0.1:8090;\n"
            "    listen [::1]:8090;\n"
            "    listen [::]:80 default_server;\n"
            "    server_name demo.example;\n"
            "}\n"
        )
        self.assertEqual(
            drop_ipv6_listeners(config),
            "server {\n    listen 127.0.0.1:8090;\n    server_name demo.example;\n}\n",
        )

    def test_repeated_nginx_error_lines_are_shown_once(self):
        from webmanager.services import _tidy_nginx_output

        noisy = (
            "nginx: [emerg] bind() to 127.0.0.1:5300 failed (98: Address already in use)\n" * 5
            + "nginx: [emerg] still could not bind()\n"
        )
        self.assertEqual(
            _tidy_nginx_output(noisy),
            "nginx: [emerg] bind() to 127.0.0.1:5300 failed (98: Address already in use)\n"
            "nginx: [emerg] still could not bind()",
        )

    def test_restoring_the_gateway_does_nothing_without_nginx(self):
        self.app.config["NGINX_BINARY"] = "definitely-not-installed-nginx"
        self.app.extensions["runtime_manager"].restore_gateway()  # must not raise


if __name__ == "__main__":
    unittest.main()
