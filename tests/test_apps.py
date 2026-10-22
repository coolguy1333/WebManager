"""App hosting tests. The container runtime is faked; see test_app.py for the
shared fixtures (borrowed below without re-running its tests)."""

import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from webmanager import app_limits, apps
from webmanager.db import get_db
from webmanager.nginx import build_app_config
from webmanager.services import RuntimeErrorDetail, RuntimeManager
from webmanager.usage_sampler import UsageSampler

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
    limits_of = staticmethod(apps.ContainerRuntime.limits_of)
    fail_live_update = False

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

    def inspect(self, name, timeout=30):
        container = self.containers.get(name)
        if container is None:
            return None
        memory_mb, cpus, pids = container.get("limits", (None, None, None))
        return {
            "State": {"Status": container["status"], "StartedAt": "2026-01-01T00:00:00Z"},
            "Config": {"Labels": {"webmanager.config": container["hash"]}, "Image": container["image"]},
            "HostConfig": {
                "Memory": memory_mb * 1048576 if memory_mb else 0,
                "NanoCpus": int(cpus * 1e9) if cpus else 0,
                "PidsLimit": pids or 0,
            },
            "RestartCount": 0,
        }

    def status(self, name):
        container = self.containers.get(name)
        return container["status"] if container else None

    def run(self, *, site_id, image, name, host_port, env, memory_mb, cpus, config_hash, pids_limit=256):
        self.calls.append(("run", name, dict(env), memory_mb))
        self.containers[name] = {
            "status": "running", "hash": config_hash, "image": image, "env": dict(env),
            "limits": (memory_mb, round(cpus, 2), pids_limit),
        }

    def update_limits(self, name, *, memory_mb, cpus, pids_limit):
        self.calls.append(("update_limits", name, memory_mb, cpus, pids_limit))
        if self.fail_live_update:
            raise apps.AppError("update is not supported here")
        self.containers[name]["limits"] = (memory_mb, round(cpus, 2), pids_limit)

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

    def logs(self, name, tail=200, timeout=30):
        return "listening on 8080"

    def running_app_names(self):
        return [name for name, container in self.containers.items() if container["status"] == "running"]

    def stats_many(self, names):
        return {
            name: {
                "cpu_percent": 1.2,
                "memory_percent": 5.0,
                "memory_used": "12MiB",
                "memory_limit": "256MiB",
                "net_rx": "1kB",
                "net_tx": "2kB",
                "block_read": "0B",
                "block_write": "0B",
                "pids": 3,
            }
            for name in names
            if name in self.containers and self.containers[name]["status"] == "running"
        }

    def backup_data(self, name, destination):
        self.calls.append(("backup", name))
        return None

    def restore_data(self, volume_name, tar_path):
        # Read eagerly: the real ContainerRuntime.restore_data() also
        # consumes the file synchronously before returning, since the
        # caller's scratch directory may be gone right after.
        content = Path(tar_path).read_bytes()
        self.calls.append(("restore_data", volume_name, content))


class BuildFailureSummaryTests(unittest.TestCase):
    OUTPUT = (
        "#0 building with \"default\" instance using docker driver\n"
        "#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE 0.0s\n"
        "#4 [1/4] FROM docker.io/library/busybox:latest@sha256:abc\n"
        "#4 DONE 0.0s\n"
        "#6 [2/4] RUN make build\n"
        "#6 0.312 make: *** No rule to make target 'build'.  Stop.\n"
        "#6 ERROR: process \"/bin/sh -c make build\" did not complete successfully: exit code: 2\n"
        "------\n > [2/4] RUN make build:\n0.312 make: *** No rule to make target 'build'.  Stop.\n------\n"
        "Dockerfile:2\n"
        "ERROR: failed to build: failed to solve: process \"/bin/sh -c make build\" "
        "did not complete successfully: exit code: 2\n"
    )

    def test_the_reason_comes_first_and_progress_noise_is_dropped(self):
        reason, tidy = apps.summarize_build_failure(self.OUTPUT)
        self.assertEqual(
            reason,
            'process "/bin/sh -c make build" did not complete successfully: exit code: 2',
        )
        self.assertIn("No rule to make target", tidy)
        self.assertNotIn("DONE", tidy)
        self.assertNotIn("[internal]", tidy)

    def test_long_output_is_cut_on_a_line_boundary_and_keeps_the_end(self):
        noisy = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(400)) + "\nERROR: failed to build: boom\n"
        reason, tidy = apps.summarize_build_failure(noisy, limit=500)
        self.assertEqual(reason, "boom")
        self.assertLessEqual(len(tidy), 500)
        self.assertTrue(tidy.startswith("line "))
        self.assertTrue(tidy.endswith("ERROR: failed to build: boom"))

    def test_output_without_a_recognisable_error_still_returns_its_tail(self):
        reason, tidy = apps.summarize_build_failure("something odd happened\n")
        self.assertEqual(reason, "")
        self.assertEqual(tidy, "something odd happened")


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

    def add_domain(self, name):
        with self.app.app_context():
            database = get_db()
            domain_id = database.execute("INSERT INTO domains (name) VALUES (?)", (name,)).lastrowid
            database.commit()
            return domain_id

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
    def test_app_can_be_deployed_at_the_domain_root_without_a_subdomain(self):
        _, repository_id = self.owner()
        domain_id = self.add_domain("apps.example")
        response, _ = self.deploy(repository_id, slug="", use_domain_root="on", domain_id=str(domain_id))
        self.assertIn(response.status_code, (302, 303))
        site = self.site()
        self.assertIsNotNone(site)
        self.assertEqual(site["use_domain_root"], 1)

    def test_a_second_app_cannot_take_an_already_used_domain_root(self):
        _, repository_id = self.owner()
        domain_id = str(self.add_domain("apps.example"))
        self.deploy(repository_id, slug="", use_domain_root="on", domain_id=domain_id)
        (self.root / "webmanager.json").write_text(json.dumps({**MANIFEST, "env": []}), encoding="utf-8")
        self.deploy(repository_id, slug="", use_domain_root="on", domain_id=domain_id)
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) FROM sites WHERE kind = 'app'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_settings_page_does_not_require_a_subdomain(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        page = self.client.get(f"/sites/{self.site()['id']}/settings")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'name="slug" maxlength="48" value="status" required', page.data)

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
        # Nothing is customised: the manifest's memory stays the live default,
        # so changing webmanager.json later still reaches this app.
        self.assertIsNone(site["app_memory_mb"])
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

    def _apply_update_with_app_status(self, status):
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
            database.execute("UPDATE sites SET status = ? WHERE id = ?", (status, site_id))
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
        return site_id, result, start, restart

    def test_approved_source_update_rebuilds_app_in_background(self):
        site_id, result, start, restart = self._apply_update_with_app_status("running")
        self.assertEqual(result.status, "applied")
        start.assert_called_once_with(site_id)
        restart.assert_not_called()

    def test_an_update_retries_an_app_that_was_left_in_error(self):
        # The update is often the fix for whatever made the app fail.
        site_id, result, start, _restart = self._apply_update_with_app_status("error")
        self.assertEqual(result.status, "applied")
        start.assert_called_once_with(site_id)

    def test_an_update_leaves_a_deliberately_stopped_app_stopped(self):
        _site_id, result, start, _restart = self._apply_update_with_app_status("stopped")
        self.assertEqual(result.status, "applied")
        start.assert_not_called()

    # -- usage monitoring & separate sites/apps views -------------------------
    def test_apps_view_only_lists_apps_and_sites_view_excludes_them(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        apps_page = self.client.get("/?view=apps")
        self.assertIn(b"Status", apps_page.data)
        self.assertIn(b"data-apps-live", apps_page.data)

        sites_page = self.client.get("/?view=sites")
        self.assertNotIn(b"Status</a>", sites_page.data)
        self.assertIn(b"No sites yet", sites_page.data)

    def test_per_app_status_json_reports_live_stats(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        response = self.client.get(f"/sites/{site_id}/status.json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["stats"]["cpu_percent"], 1.2)

    def test_apps_stats_json_is_scoped_to_visible_apps(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        response = self.client.get("/apps/stats.json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data[str(site_id)]["memory_used"], "12MiB")

    def test_admin_apps_dashboard_lists_every_app_with_usage(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        page = self.client.get("/admin/apps")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Status", page.data)
        self.assertIn(b"alice@example.com", page.data)

        stats = self.client.get("/admin/apps/stats.json")
        self.assertEqual(stats.status_code, 200)
        self.assertIn(str(site_id), stats.get_json())

    def test_admin_apps_dashboard_is_admin_only(self):
        user_id, repository_id = self.owner()
        response = self.client.get("/admin/apps")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(user_id)

    # -- speed: pages never wait on the container runtime -----------------------
    def test_app_pages_do_not_wait_for_docker_stats(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)

        release = threading.Event()
        self.addCleanup(release.set)
        real_stats = self.fake.stats_many
        self.fake.stats_many = lambda names: (release.wait(10), real_stats(names))[1]
        manager = self.app.extensions["runtime_manager"]
        manager.usage = UsageSampler(manager._sample_usage, pause=0.01)

        began = time.monotonic()
        for url in ("/?view=apps", f"/sites/{site_id}", f"/sites/{site_id}/status.json", "/apps/stats.json"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertLess(time.monotonic() - began, 3.0, "a page waited for the slow `docker stats`")
        self.assertEqual(self.client.get("/apps/stats.json").get_json(), {})

        release.set()
        deadline = time.monotonic() + 3
        data = {}
        while not data and time.monotonic() < deadline:
            data = self.client.get("/apps/stats.json").get_json()
            time.sleep(0.02)
        self.assertEqual(data[str(site_id)]["memory_used"], "12MiB")

    def test_runtime_reachability_is_remembered_between_page_loads(self):
        manager = self.app.extensions["runtime_manager"]
        calls = []
        self.fake.available = lambda: calls.append(1) or (True, "fake")

        for _ in range(3):
            self.assertTrue(manager.apps_status()[0])

        self.assertEqual(len(calls), 1)

    def test_an_unreachable_runtime_is_checked_again_soon(self):
        manager = self.app.extensions["runtime_manager"]
        manager.RUNTIME_DOWN_SECONDS = 0
        answers = [(False, "cannot connect to the daemon"), (True, "27.0")]
        self.fake.available = lambda: answers.pop(0)

        ready, message = manager.apps_status()
        self.assertFalse(ready)
        self.assertIn("cannot connect to the daemon", message)
        self.assertTrue(manager.apps_status()[0])

    def test_a_container_that_stops_mid_reading_does_not_blank_every_app(self):
        manager = self.app.extensions["runtime_manager"]
        self.fake.containers["webmanager-app-1"] = {"status": "running", "hash": "h", "image": "i"}
        real_stats = self.fake.stats_many
        attempts = []
        # docker stats fails for every name if one of them is gone by then.
        self.fake.stats_many = lambda names: {} if not attempts.append(1) and len(attempts) == 1 else real_stats(names)

        self.assertIn("webmanager-app-1", manager._sample_usage())
        self.assertEqual(len(attempts), 2)

    def test_site_log_only_reads_the_end_of_a_large_file(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        log = Path(self.app.config["LOG_ROOT"]) / f"site-{site_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(f"line {i:06d} " + "x" * 50 for i in range(20000)) + "\n", encoding="utf-8")

        page = self.client.get(f"/sites/{site_id}")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"line 019999", page.data)
        self.assertIn(b"line 019920", page.data)
        self.assertNotIn(b"line 019919", page.data)  # only the last 80 lines
        self.assertNotIn(b"line 000000", page.data)

    # -- resources: memory, CPU and process limits per app -----------------------
    def running_app(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})
        self.start_now(site_id)
        return site_id, self.fake.container_name(site_id)

    def save_resources(self, site_id, **form):
        return self.client.post(
            f"/sites/{site_id}/resources", data={"_csrf_token": self.csrf(), **form}
        )

    def runs(self):
        return [call for call in self.fake.calls if call[0] == "run"]

    def test_resources_page_shows_the_defaults_and_this_servers_size(self):
        site_id, _name = self.running_app()
        page = self.client.get(f"/sites/{site_id}/resources")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('placeholder="128"', html)  # the manifest's memory is the default
        self.assertIn('placeholder="0.5"', html)
        self.assertIn('placeholder="256"', html)
        self.assertIn("CPU core", html)

    def test_other_people_cannot_open_or_change_an_apps_resources(self):
        site_id, _name = self.running_app()
        self.login_user(self.add_user("mallory", "mallory@example.com"))
        self.assertEqual(self.client.get(f"/sites/{site_id}/resources").status_code, 404)
        self.assertEqual(self.save_resources(site_id, memory_mb="512").status_code, 404)
        self.assertIsNone(self.site()["app_memory_mb"])

    def test_new_limits_reach_a_running_app_without_restarting_it(self):
        site_id, name = self.running_app()
        runs_before = len(self.runs())

        response = self.save_resources(site_id, memory_mb="512", cpus="1.25", pids="300")

        self.assertIn(response.status_code, (302, 303))
        site = self.site()
        self.assertEqual((site["app_memory_mb"], site["app_cpus"], site["app_pids"]), (512, 1.25, 300))
        self.assertIn(("update_limits", name, 512, 1.25, 300), self.fake.calls)
        self.assertEqual(len(self.runs()), runs_before, "the container must not be replaced")
        page = self.client.get(f"/sites/{site_id}").get_data(as_text=True)
        self.assertIn("512 MB", page)
        self.assertIn("1.25 cores", page)

    def test_limits_saved_for_a_stopped_app_are_used_when_it_starts(self):
        _, repository_id = self.owner()
        self.deploy(repository_id)
        site_id = self.site()["id"]
        self.store(site_id, {"ADMIN_PASSWORD": "pw"})

        self.save_resources(site_id, memory_mb="700", cpus="0.75", pids="64")
        self.start_now(site_id)

        name = self.fake.container_name(site_id)
        self.assertEqual(self.fake.containers[name]["limits"], (700, 0.75, 64))
        self.assertEqual([call for call in self.fake.calls if call[0] == "update_limits"], [])

    def test_a_runtime_that_cannot_change_limits_live_replaces_the_container(self):
        site_id, _name = self.running_app()
        self.fake.fail_live_update = True
        with patch.object(RuntimeManager, "start_app_async", return_value=True) as start:
            self.save_resources(site_id, memory_mb="512")
        start.assert_called_once_with(site_id, force=True)

    def test_blank_boxes_and_reset_go_back_to_the_defaults(self):
        site_id, name = self.running_app()
        self.save_resources(site_id, memory_mb="512", cpus="1", pids="100")

        self.save_resources(site_id, memory_mb="", cpus="", pids="")
        site = self.site()
        self.assertEqual((site["app_memory_mb"], site["app_cpus"], site["app_pids"]), (None, None, None))
        self.assertEqual(self.fake.containers[name]["limits"], (128, 0.5, 256))

        self.save_resources(site_id, memory_mb="512")
        self.save_resources(site_id, reset="1", memory_mb="999")
        self.assertIsNone(self.site()["app_memory_mb"])

    def test_nonsense_is_refused_and_nothing_changes(self):
        site_id, _name = self.running_app()
        for form in (
            {"memory_mb": "lots"}, {"memory_mb": "10"}, {"memory_mb": "-5"}, {"memory_mb": "nan"},
            {"cpus": "0"}, {"cpus": "inf"}, {"cpus": "abc"}, {"pids": "1"}, {"pids": "9999999"},
        ):
            with self.subTest(form=form):
                response = self.save_resources(site_id, **form)
                self.assertEqual(response.status_code, 422)
        site = self.site()
        self.assertEqual((site["app_memory_mb"], site["app_cpus"], site["app_pids"]), (None, None, None))

    def test_owners_stop_at_the_server_caps_but_super_admins_may_go_higher(self):
        self.app.config["APP_MAX_MEMORY_MB"] = 1024
        self.app.config["APP_MAX_CPUS"] = 1
        site_id, _name = self.running_app()
        with patch.object(app_limits, "host_capacity", return_value={"memory_mb": 16384, "cpus": 8}):
            self.assertEqual(self.save_resources(site_id, memory_mb="2048").status_code, 422)
            self.assertEqual(self.save_resources(site_id, cpus="1.5").status_code, 422)
            self.assertIn(self.save_resources(site_id, memory_mb="1024", cpus="1").status_code, (302, 303))

            self.login_user(self.add_user("root", is_admin=True))
            self.assertIn(self.save_resources(site_id, memory_mb="8192", cpus="6").status_code, (302, 303))
        site = self.site()
        self.assertEqual((site["app_memory_mb"], site["app_cpus"]), (8192, 6.0))

    def test_a_value_an_admin_set_above_the_cap_survives_the_owner_saving_other_fields(self):
        self.app.config["APP_MAX_MEMORY_MB"] = 1024
        site_id, _name = self.running_app()
        with self.app.app_context():
            get_db().execute("UPDATE sites SET app_memory_mb = 3000").connection.commit()
        with patch.object(app_limits, "host_capacity", return_value={"memory_mb": 16384, "cpus": 8}):
            response = self.save_resources(site_id, memory_mb="3000", cpus="0.75")
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(self.site()["app_cpus"], 0.75)

    def test_starting_an_app_applies_changed_limits_to_the_running_container(self):
        site_id, name = self.running_app()
        runs_before = len(self.runs())
        with self.app.app_context():
            get_db().execute("UPDATE sites SET app_memory_mb = 300, app_pids = 99").connection.commit()

        self.start_now(site_id)

        self.assertEqual(len(self.runs()), runs_before)
        self.assertEqual(self.fake.containers[name]["limits"], (300, 0.5, 99))

    def test_containers_made_before_limits_left_the_hash_are_adopted_not_replaced(self):
        site_id, name = self.running_app()
        container = self.fake.containers[name]
        container["hash"] = apps.legacy_config_hash(
            container["image"], container["env"], 128, 0.5, self.site()["port"]
        )
        runs_before = len(self.runs())

        self.start_now(site_id)

        self.assertEqual(len(self.runs()), runs_before, "updating WebManager must not restart every app")

    def test_changed_settings_still_replace_the_container(self):
        site_id, _name = self.running_app()
        runs_before = len(self.runs())
        self.store(site_id, {"ADMIN_PASSWORD": "a-different-one"})
        self.start_now(site_id)
        self.assertEqual(len(self.runs()), runs_before + 1)

    def test_changing_the_manifest_memory_reaches_apps_nobody_customised(self):
        site_id, name = self.running_app()
        (self.root / "webmanager.json").write_text(
            json.dumps({**MANIFEST, "memory_mb": 512}), encoding="utf-8"
        )
        self.start_now(site_id)
        self.assertEqual(self.fake.containers[name]["limits"][0], 512)

    def test_an_apps_own_manifest_cannot_ask_for_more_than_the_owner_cap(self):
        self.app.config["APP_MAX_MEMORY_MB"] = 256
        site = {"app_memory_mb": None, "app_cpus": None, "app_pids": None}
        self.assertEqual(app_limits.effective(site, self.app.config, 4096)["memory_mb"], 256)
        # ...while a value an administrator set on the app itself stands.
        site["app_memory_mb"] = 1024
        self.assertEqual(app_limits.effective(site, self.app.config, 4096)["memory_mb"], 1024)

    def test_admins_see_all_limits_added_up(self):
        self.running_app()
        self.login_user(self.add_user("root", is_admin=True))
        with patch.object(app_limits, "host_capacity", return_value={"memory_mb": 100, "cpus": 1}):
            page = self.client.get("/admin/apps").get_data(as_text=True)
        self.assertIn("128 MB of this server's 100 MB", page)
        self.assertIn("more than the server has", page)

    def test_cpus_are_never_set_above_what_the_machine_has(self):
        site = {"app_memory_mb": None, "app_cpus": 64.0, "app_pids": None}
        with patch.object(app_limits, "host_capacity", return_value={"memory_mb": 8192, "cpus": 4}):
            self.assertEqual(app_limits.effective(site, self.app.config)["cpus"], 4.0)

    def test_docs_page_renders_app_hosting_section(self):
        self.owner()
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"webmanager.json", response.data)
        self.assertIn(b"App hosting", response.data)


if __name__ == "__main__":
    unittest.main()
