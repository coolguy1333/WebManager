"""App hosting tests. The container runtime is faked; see test_app.py for the
shared fixtures (borrowed below without re-running its tests)."""

import json
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from webmanager import apps
from webmanager.db import get_db
from webmanager.nginx import build_app_config
from webmanager.services import RuntimeErrorDetail, RuntimeManager

from tests import test_app as base

MANIFEST = {
    "type": "app",
    "health": "/api/health",
    "memory_mb": 128,
    "env": [
        {"name": "ADMIN_PASSWORD", "secret": True, "required": True},
        {"name": "SITE_TITLE", "default": "Uptime"},
        {"name": "MODE", "choices": ["a", "b"]},
    ],
}


class FakeRuntime:
    """In-memory stand-in for apps.ContainerRuntime."""

    binary = "/usr/bin/docker"

    def __init__(self):
        self.containers = {}
        self.images = set()
        self.volumes_removed = []
        self.calls = []

    container_name = staticmethod(apps.ContainerRuntime.container_name)
    volume_name = staticmethod(apps.ContainerRuntime.volume_name)
    image_name = staticmethod(apps.ContainerRuntime.image_name)

    def available(self):
        return True, "fake"

    def image_exists(self, image):
        return image in self.images

    def build(self, site_id, context, tag, log_path=None):
        self.calls.append(("build", site_id, tag))
        image = self.image_name(site_id, tag)
        self.images.add(image)
        return image

    def remove_images(self, site_id, keep=None):
        self.calls.append(("remove_images", site_id, keep))

    def inspect(self, name):
        container = self.containers.get(name)
        if container is None:
            return None
        return {
            "State": {"Status": container["status"], "StartedAt": "2026-01-01T00:00:00Z"},
            "Config": {"Labels": {"webmanager.config": container["hash"]}, "Image": container["image"]},
            "RestartCount": 0,
        }

    def status(self, name):
        container = self.containers.get(name)
        return container["status"] if container else None

    def run(self, *, site_id, image, name, host_port, env, memory_mb, cpus, config_hash):
        self.calls.append(("run", name, dict(env), memory_mb))
        self.containers[name] = {"status": "running", "hash": config_hash, "image": image, "env": dict(env)}

    def stop(self, name, timeout=10):
        self.calls.append(("stop", name))
        if name in self.containers:
            self.containers[name]["status"] = "exited"

    def start(self, name):
        self.calls.append(("start", name))
        self.containers[name]["status"] = "running"

    def remove(self, name):
        self.containers.pop(name, None)

    def rename(self, old, new):
        self.containers[new] = self.containers.pop(old)

    def remove_volume(self, site_id):
        self.volumes_removed.append(site_id)

    def logs(self, name, tail=200):
        return "listening on 8080"

    def backup_data(self, name, destination):
        self.calls.append(("backup", name))
        return None


class AppHostingTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    csrf = base.WebManagerTestCase.csrf
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    add_repository = base.WebManagerTestCase.add_repository

    def setUp(self):
        self.setUp_base()
        self.app.config["APPS_ENABLED"] = True
        self.fake = FakeRuntime()
        patchers = [
            patch.object(RuntimeManager, "container_runtime", new_callable=PropertyMock, return_value=self.fake),
            patch.object(RuntimeManager, "nginx_binary", new_callable=PropertyMock, return_value="/usr/sbin/nginx"),
            patch.object(RuntimeManager, "_reload_nginx", return_value=None),
            patch.object(RuntimeManager, "_run_nginx", return_value=None),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = Path(self.temp_directory.name) / "repo"
        self.root.mkdir()
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.root / "webmanager.json").write_text(json.dumps(MANIFEST), encoding="utf-8")

    # -- helpers -----------------------------------------------------------
    def owner(self, *, host=True, admin=False):
        user_id = self.add_user("alice", "alice@example.com", is_admin=admin)
        if host and not admin:
            with self.app.app_context():
                database = get_db()
                group = database.execute("INSERT INTO groups (name) VALUES ('App hosts')").lastrowid
                database.execute("INSERT INTO group_permissions (group_id, permission_code) VALUES (?, 'apps.host')", (group,))
                database.execute("INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)", (user_id, group))
                database.commit()
        repository_id = self.add_repository(user_id, self.root / "x")
        self.login_user(user_id)
        return user_id, repository_id

    def deploy(self, repository_id, slug="status", **extra):
        with patch.object(RuntimeManager, "start_app_async", return_value=True) as start:
            response = self.client.post(
                f"/repositories/{repository_id}/deploy-app",
                data={"_csrf_token": self.csrf(), "site_name": "Status", "slug": slug, "folder": ".", **extra},
            )
        return response, start

    def site(self):
        with self.app.app_context():
            return get_db().execute("SELECT * FROM sites WHERE kind = 'app'").fetchone()

    def store(self, site_id, values):
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET app_env = ? WHERE id = ?",
                (apps.encrypt_env(values, self.app.config["SECRET_KEY"]), site_id),
            )
            database.commit()

    # -- manifest ------------------------------------------------------------
    def test_manifest_validation_messages(self):
        good = apps.load_manifest(self.root)
        self.assertEqual(good["memory_mb"], 128)
        self.assertTrue(good["env"][0]["secret"])

        cases = [
            ({"type": "site", "health": "/"}, '"type": "app"'),
            ({"type": "app", "health": "health"}, '"health"'),
            ({"type": "app", "health": "/", "memory_mb": 99999}, "memory_mb"),
            ({"type": "app", "health": "/", "env": [{"name": "PORT"}]}, "set by WebManager"),
            ({"type": "app", "health": "/", "env": [{"name": "lower"}]}, "UPPER_SNAKE_CASE"),
            ({"type": "app", "health": "/", "env": [{"name": "A"}, {"name": "A"}]}, "more than once"),
            ({"type": "app", "health": "/", "env": [{"name": "A", "choices": ["x"], "default": "y"}]}, "one of the choices"),
        ]
        for manifest, message in cases:
            (self.root / "webmanager.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(message=message):
                with self.assertRaises(apps.AppError) as caught:
                    apps.load_manifest(self.root)
                self.assertIn(message, str(caught.exception))

        (self.root / "webmanager.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        (self.root / ".env").write_text("SECRET=1", encoding="utf-8")
        with self.assertRaisesRegex(apps.AppError, ".env"):
            apps.load_manifest(self.root)

    def test_find_app_folders_skips_dependencies(self):
        nested = self.root / "node_modules" / "pkg"
        nested.mkdir(parents=True)
        (nested / "Dockerfile").write_text("", encoding="utf-8")
        (nested / "webmanager.json").write_text("{}", encoding="utf-8")
        found = apps.find_app_folders(self.root)
        self.assertEqual([item["folder"] for item in found], ["."])
        self.assertIsNone(found[0]["error"])

    def test_env_is_encrypted_and_reserved_values_win(self):
        token = apps.encrypt_env({"ADMIN_PASSWORD": "hunter2"}, "key-1")
        self.assertNotIn("hunter2", token)
        self.assertEqual(apps.decrypt_env(token, "key-1"), {"ADMIN_PASSWORD": "hunter2"})
        with self.assertRaises(apps.AppError):
            apps.decrypt_env(token, "another-key")

        manifest = apps.load_manifest(self.root)
        env, missing = apps.effective_env(manifest, {}, app_id=7, public_url="https://s.example")
        self.assertEqual(missing, ["ADMIN_PASSWORD"])
        self.assertEqual(env["SITE_TITLE"], "Uptime")
        self.assertEqual(env["PORT"], "8080")
        self.assertEqual(env["DATA_DIR"], "/data")
        self.assertEqual(env["PUBLIC_URL"], "https://s.example")
        self.assertIsNotNone(apps.validate_value("MODE", "c", manifest["env"][2]))
        self.assertIsNotNone(apps.validate_value("X", "a\nb", {}))

    def test_app_nginx_config_proxies_to_loopback_only(self):
        config = build_app_config("Status", 43105, ["status.example.com"], 43099)
        self.assertIn("proxy_pass http://127.0.0.1:43105", config)
        self.assertIn("server_name status.example.com", config)
        self.assertIn("listen 127.0.0.1:43099", config)
        self.assertIn("X-Forwarded-Proto", config)
        self.assertIn("@webmanager_app_down", config)

    # -- permissions and limits ---------------------------------------------
    def test_deploy_requires_app_hosting_permission(self):
        _, repository_id = self.owner(host=False)
        response, start = self.deploy(repository_id)
        self.assertEqual(response.status_code, 403)
        start.assert_not_called()
        self.assertIsNone(self.site())

    def test_deploy_refused_when_app_hosting_is_off(self):
        _, repository_id = self.owner()
        with patch.object(RuntimeManager, "apps_status", return_value=(False, "App hosting is turned off.")):
            response, _ = self.deploy(repository_id)
        self.assertIn(response.status_code, (302, 303))
        self.assertIsNone(self.site())

    def test_app_limit_is_enforced(self):
        _, repository_id = self.owner()
        with self.app.app_context():
            get_db().execute("UPDATE users SET max_apps = 1").connection.commit()
        self.deploy(repository_id, slug="one")
        (self.root / "webmanager.json").write_text(
            json.dumps({**MANIFEST, "env": []}), encoding="utf-8"
        )
        self.deploy(repository_id, slug="two")
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) FROM sites WHERE kind = 'app'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_admin_can_set_app_limits(self):
        admin_id = self.add_user("root", is_admin=True)
        user_id = self.add_user("bob")
        self.login_user(admin_id)
        response = self.client.post(
            "/admin/limits",
            data={"_csrf_token": self.csrf(), "max_sites": "3", "max_sources": "2", "max_apps": "4"},
        )
        self.assertIn(response.status_code, (302, 303))
        with self.app.app_context():
            from webmanager import quotas

            self.assertEqual(quotas.get_defaults(get_db())["apps"], 4)
        page = self.client.get("/admin/?section=people")
        self.assertIn(b'name="max_apps"', page.data)
        self.assertIn(b"0/4 apps", page.data)
        self.assertTrue(user_id)

    # -- deploy and variables -------------------------------------------------
    def test_deploy_with_required_variable_goes_to_variables_first(self):
        _, repository_id = self.owner()
        response, start = self.deploy(repository_id)
        site = self.site()
        self.assertEqual(site["kind"], "app")
        self.assertEqual(site["app_memory_mb"], 128)
        self.assertIn(f"/sites/{site['id']}/variables", response.headers["Location"])
        start.assert_not_called()

        page = self.client.get(f"/sites/{site['id']}/variables")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"ADMIN_PASSWORD", page.data)
        self.assertIn(b'type="password"', page.data)

    def test_deploy_without_required_variables_starts_in_background(self):
        (self.root / "webmanager.json").write_text(
            json.dumps({**MANIFEST, "env": [{"name": "SITE_TITLE"}]}), encoding="utf-8"
        )
        _, repository_id = self.owner()
        response, start = self.deploy(repository_id)
        site = self.site()
        self.assertIn(f"/sites/{site['id']}", response.headers["Location"])
        start.assert_called_once_with(site["id"])

    def test_deploy_rejects_taken_subdomain(self):
        _, repository_id = self.owner()
        self.deploy(repository_id, slug="status")
        (self.root / "webmanager.json").write_text(json.dumps({**MANIFEST, "env": []}), encoding="utf-8")
        with self.app.app_context():
            get_db().execute("UPDATE users SET max_apps = 0").connection.commit()
        response, _ = self.deploy(repository_id, slug="status")
        self.assertIn(response.status_code, (302, 303))
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        self.assertEqual(count, 1)

    def test_variables_keep_secret_when_blank_and_can_clear(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        with patch.object(RuntimeManager, "start_app_async", return_value=True) as start:
            response = self.client.post(
                f"/sites/{site_id}/variables",
                data={"_csrf_token": self.csrf(), "var_ADMIN_PASSWORD": "s3cret", "var_MODE": "a"},
            )
        self.assertIn(response.status_code, (302, 303))
        start.assert_called_once()
        stored_token = self.site()["app_env"]
        self.assertNotIn("s3cret", stored_token)
        with self.app.app_context():
            self.assertEqual(
                apps.decrypt_env(stored_token, self.app.config["SECRET_KEY"])["ADMIN_PASSWORD"], "s3cret"
            )

        page = self.client.get(f"/sites/{site_id}/variables")
        self.assertNotIn(b"s3cret", page.data)
        self.assertIn(b"A value is saved", page.data)

        # Blank keeps the saved secret.
        with patch.object(RuntimeManager, "start_app_async", return_value=True):
            self.client.post(
                f"/sites/{site_id}/variables",
                data={"_csrf_token": self.csrf(), "var_ADMIN_PASSWORD": "", "var_SITE_TITLE": "Hi"},
            )
        with self.app.app_context():
            values = apps.decrypt_env(self.site()["app_env"], self.app.config["SECRET_KEY"])
        self.assertEqual(values, {"ADMIN_PASSWORD": "s3cret", "SITE_TITLE": "Hi"})

        # Clearing a required secret is refused.
        response = self.client.post(
            f"/sites/{site_id}/variables",
            data={"_csrf_token": self.csrf(), "clear_ADMIN_PASSWORD": "on"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"Required: ADMIN_PASSWORD", response.data)

    def test_variables_reject_invalid_choice(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        response = self.client.post(
            f"/sites/{site_id}/variables",
            data={"_csrf_token": self.csrf(), "var_ADMIN_PASSWORD": "x", "var_MODE": "zzz"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"MODE must be one of", response.data)

    def test_other_users_cannot_open_variables(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        mallory = self.add_user("mallory")
        self.login_user(mallory)
        self.assertIn(self.client.get(f"/sites/{site_id}/variables").status_code, (403, 404))

    def test_nginx_config_editor_is_not_available_for_apps(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        response = self.client.get(f"/sites/{site_id}/config")
        self.assertIn(response.status_code, (302, 303, 404))
        detail = self.client.get(f"/sites/{site_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Container", detail.data)
        settings = self.client.get(f"/sites/{site_id}/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertNotIn(b'name="spa_fallback"', settings.data)

    # -- container lifecycle ----------------------------------------------------
    def start_now(self, site_id, force=False, healthy=True):
        with patch.object(apps, "wait_until_healthy", return_value=(healthy, "ok" if healthy else "HTTP 500")):
            with self.app.app_context():
                manager = self.app.extensions["runtime_manager"]
                return manager.restart_site(site_id) if force else manager.start_site(site_id)

    def test_start_builds_runs_and_routes(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.assertEqual(self.start_now(site_id), "container")

        name = self.fake.container_name(site_id)
        run = [call for call in self.fake.calls if call[0] == "run"][0]
        self.assertEqual(run[1], name)
        self.assertEqual(run[2]["ADMIN_PASSWORD"], "pw")
        self.assertEqual(run[2]["PUBLIC_URL"], "https://status.webmanager.example")
        self.assertEqual(run[3], 128)
        site = self.site()
        self.assertEqual(site["status"], "running")
        self.assertEqual(site["runtime_backend"], "container")
        configs = list((Path(self.app.config["NGINX_ROOT"]) / "conf.d").glob(f"{site_id}-*.conf"))
        self.assertTrue(configs)
        self.assertIn(f"proxy_pass http://127.0.0.1:{site['port']}", configs[0].read_text())

        # Starting again with the same config keeps the running container.
        self.fake.calls.clear()
        self.start_now(site_id)
        self.assertFalse([call for call in self.fake.calls if call[0] in ("run", "build")])

    def test_failed_health_check_rolls_back_to_previous_version(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)
        name = self.fake.container_name(site_id)
        original = self.fake.containers[name]["env"]

        self.store(site_id, {"ADMIN_PASSWORD": "new"})
        with self.assertRaises(RuntimeErrorDetail):
            self.start_now(site_id, force=True, healthy=False)
        self.assertIn(("backup", name), self.fake.calls)
        self.assertEqual(self.fake.containers[name]["env"], original)
        self.assertEqual(self.fake.containers[name]["status"], "running")
        site = self.site()
        self.assertEqual(site["status"], "running")
        self.assertIn("previous version was restored", site["last_error"])
        detail = self.client.get(f"/sites/{site_id}")
        self.assertIn(b"The previous version is still running", detail.data)

    def test_first_start_failure_marks_error(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        with self.assertRaises(RuntimeErrorDetail):
            self.start_now(site_id, healthy=False)
        self.assertEqual(self.site()["status"], "error")
        self.assertNotIn(self.fake.container_name(site_id), self.fake.containers)

    def test_missing_required_variable_blocks_start(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        with self.assertRaisesRegex(RuntimeErrorDetail, "ADMIN_PASSWORD"):
            self.start_now(site_id)
        self.assertFalse([call for call in self.fake.calls if call[0] == "run"])

    def test_stop_shows_paused_page_and_delete_can_remove_data(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        response = self.client.post(f"/sites/{site_id}/stop", data={"_csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.site()["status"], "stopped")
        self.assertEqual(self.fake.status(self.fake.container_name(site_id)), "exited")
        paused = list((Path(self.app.config["NGINX_ROOT"]) / "conf.d").glob(f"{site_id}-*.paused.conf"))
        self.assertTrue(paused)

        response = self.client.post(
            f"/sites/{site_id}/delete", data={"_csrf_token": self.csrf(), "delete_data": "on"}
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertIsNone(self.site())
        self.assertEqual(self.fake.volumes_removed, [site_id])
        self.assertNotIn(self.fake.container_name(site_id), self.fake.containers)

    def test_approved_source_update_rebuilds_app_in_background(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        repository_root = Path(self.temp_directory.name) / "repo"
        pending = repository_root.parent / ".repo.pending"
        pending.mkdir()
        for name in ("Dockerfile", "webmanager.json"):
            (pending / name).write_text((repository_root / name).read_text(), encoding="utf-8")
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE sites SET status = 'running' WHERE id = ?", (site_id,))
            database.execute(
                "UPDATE repositories SET local_path = ?, pending_path = ?, pending_commit = ? WHERE id = ?",
                (str(repository_root), str(pending), "b" * 40, repository_id),
            )
            database.commit()
        runtime = self.app.extensions["runtime_manager"]
        with (
            patch("webmanager.repository_refresh.repository_commit", return_value="b" * 40),
            patch.object(runtime, "start_app_async", return_value=True) as start,
            patch.object(runtime, "restart_site") as restart,
        ):
            result = self.app.extensions["repository_refresh_manager"].apply_pending(repository_id)
        self.assertEqual(result.status, "applied")
        start.assert_called_once_with(site_id)
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
