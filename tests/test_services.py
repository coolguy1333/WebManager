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

    def test_restore_starts_apps_that_failed_but_not_stopped_ones(self):
        self.make_sites(3)
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE sites SET kind = 'app'")
            database.execute("UPDATE sites SET status = 'error' WHERE slug = 'site0'")
            database.execute("UPDATE sites SET status = 'stopped' WHERE slug = 'site1'")
            database.commit()
            ids = {row["slug"]: row["id"] for row in database.execute("SELECT id, slug FROM sites")}
        runtime = self.app.extensions["runtime_manager"]
        with patch.object(runtime, "start_app_async") as start:
            runtime.restore_sites()
        self.assertEqual(sorted(call.args[0] for call in start.call_args_list), sorted([ids["site0"], ids["site2"]]))
        with patch.object(runtime, "start_app_async") as start:
            runtime.restore_sites(include_apps=False)  # a replica never starts apps
        start.assert_not_called()

    def test_a_replica_stops_serving_the_last_site_once_the_primary_stopped_it(self):
        self.make_sites(1)
        runtime = self.app.extensions["runtime_manager"]
        runtime.restore_sites(include_apps=False)
        config_dir = Path(self.app.config["NGINX_ROOT"]) / "conf.d"
        self.assertEqual([path.name for path in config_dir.glob("*.conf")], ["1-site0.conf"])
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE sites SET status = 'stopped'")  # arrives with a sync
            database.commit()

        runtime.restore_sites(include_apps=False)

        self.assertEqual([path.name for path in config_dir.glob("*.conf")], ["1-site0.paused.conf"])

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

    def test_a_running_site_whose_folder_vanished_gets_the_unavailable_page(self):
        ids = self.make_sites(2)
        gone = Path(self.temp_directory.name) / "gone"
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE sites SET document_root = ? WHERE id = ?", (str(gone), ids[1]))
            database.commit()

        self.app.extensions["runtime_manager"].restore_sites(include_apps=False)

        configs = self.written_configs()
        self.assertIn("1-site0.conf", configs)
        self.assertNotIn("2-site1.conf", configs, "nginx would answer 500 for a missing root")
        self.assertIn("return 503", configs["2-site1.paused.conf"])
        self.assertIn("site1.webmanager.example", configs["2-site1.paused.conf"])

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


class ConcurrentStartTests(unittest.TestCase):
    """Several sites/apps starting at once (after a restart or an update) must
    not trip over each other's Nginx changes. On a real server they did:
    one start deleted another's config file while that one was being tested
    ('open() .../38-fantasy-frc.conf failed'), and two starts each launched
    Nginx ('bind() to 127.0.0.1:8090 failed (98: Address already in use)')."""

    setUp_base = base.WebManagerTestCase.setUp
    tearDown_base = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    add_repository = base.WebManagerTestCase.add_repository
    add_site = base.WebManagerTestCase.add_site
    make_sites = RestoreSitesTests.make_sites
    _rename = RestoreSitesTests._rename
    _extra_site = RestoreSitesTests._extra_site
    statuses = RestoreSitesTests.statuses

    def setUp(self):
        self.setUp_base()
        root = Path(self.temp_directory.name)
        script = root / "fake-nginx"
        # Like real Nginx where it matters: "-t" opens each config file a
        # moment after listing them, and a started master writes its pid file
        # a moment after the command returns.
        script.write_text(
            '#!/bin/sh\n'
            'root="$2"\n'
            'case "$*" in\n'
            '  serve) sleep 30; exit 0 ;;\n'
            '  *" -t")\n'
            '    for f in "${root}"conf.d/*.conf; do\n'
            '      [ -e "$f" ] || continue; sleep 0.02\n'
            '      [ -e "$f" ] || { echo "nginx: [emerg] open() \\"$f\\" failed (2: No such file or directory)" >&2; exit 1; }\n'
            '    done; exit 0 ;;\n'
            '  *"-s reload") exit 0 ;;\n'
            'esac\n'
            'if [ -e "${root}started" ]; then\n'
            '  echo "nginx: [emerg] bind() to 127.0.0.1:43099 failed (98: Address already in use)" >&2; exit 1\n'
            'fi\n'
            'touch "${root}started"\n'
            '"$0" serve >/dev/null 2>&1 &\n'
            'echo $! > "${root}master.pid"\n'
            '(sleep 0.3; cat "${root}master.pid" > "${root}nginx.pid") >/dev/null 2>&1 &\n'
            'exit 0\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.app.config["NGINX_BINARY"] = str(script)
        self.app.config["NGINX_START_WAIT_SECONDS"] = 3  # this one writes its pid file
        self.nginx_root = Path(self.app.config["NGINX_ROOT"]).resolve()
        self.site_root = root / "repositories" / "1" / "1"
        self.site_root.mkdir(parents=True)
        (self.site_root / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        self.user_id = self.add_user("owner", is_admin=True)
        self.repository_id = self.add_repository(self.user_id, self.site_root)

    def tearDown(self):
        import os
        import signal

        try:
            os.kill(int((self.nginx_root / "master.pid").read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        self.tearDown_base()

    def test_sites_starting_at_the_same_time_all_come_up(self):
        import threading

        ids = self.make_sites(4, status="stopped")
        runtime = self.app.extensions["runtime_manager"]
        errors = []

        def start(site_id):
            with self.app.app_context():
                try:
                    runtime.start_site(site_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        threads = [threading.Thread(target=start, args=(site_id,)) for site_id in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(errors, [])
        self.assertEqual(set(self.statuses().values()), {"running"})
        configs = {path.name for path in (self.nginx_root / "conf.d").glob("*.conf")}
        self.assertEqual(len([name for name in configs if not name.endswith(".paused.conf")]), 4, configs)

    def test_a_port_held_by_another_program_says_how_to_clear_it(self):
        root = Path(self.temp_directory.name)
        script = root / "busy-nginx"
        script.write_text(
            '#!/bin/sh\ncase "$*" in *" -t") exit 0 ;; esac\n'
            'echo "nginx: [emerg] bind() to 127.0.0.1:43099 failed (98: Address already in use)" >&2; exit 1\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.app.config["NGINX_BINARY"] = str(script)
        runtime = self.app.extensions["runtime_manager"]
        with self.app.app_context(), patch.object(runtime, "_wait_for_nginx", return_value=False):
            with self.assertRaises(Exception) as caught:
                runtime.apply_nginx_configs()
        self.assertIn("restarting WebManager on the server clears it", str(caught.exception))
