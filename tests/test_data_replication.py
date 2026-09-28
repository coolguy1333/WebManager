"""Site/app data replication tests: exporting a snapshot on the primary,
and applying one on a replica. See webmanager/data_replication.py."""

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from webmanager import data_replication, peer_http
from webmanager.services import RuntimeManager

from tests import test_app as base
from tests.test_apps import FakeRuntime


class SafeMembersTests(unittest.TestCase):
    def _member(self, name):
        info = tarfile.TarInfo(name=name)
        return info

    def test_strips_the_prefix_and_keeps_ordinary_members(self):
        members = [self._member("repositories"), self._member("repositories/1/index.html")]
        safe = data_replication._safe_members(members, "repositories")
        self.assertEqual([m.name for m in safe], ["1/index.html"])

    def test_rejects_path_traversal_attempts(self):
        members = [
            self._member("repositories/../../etc/passwd"),
            self._member("repositories/1/../../../etc/passwd"),
        ]
        safe = data_replication._safe_members(members, "repositories")
        self.assertEqual(safe, [])

    def test_rejects_absolute_paths_after_stripping(self):
        members = [self._member("repositories//etc/passwd")]
        # "repositories//etc/passwd" -> relative "/etc/passwd" (absolute) -> rejected.
        safe = data_replication._safe_members(members, "repositories")
        self.assertEqual(safe, [])

    def test_drops_escaping_links_devices_and_special_bits(self):
        def link(name, target, kind=tarfile.SYMTYPE):
            info = tarfile.TarInfo(name=name)
            info.type = kind
            info.linkname = target
            return info

        device = tarfile.TarInfo(name="repositories/1/dev")
        device.type = tarfile.CHRTYPE
        setuid = tarfile.TarInfo(name="repositories/1/run.sh")
        setuid.mode = 0o4777
        members = [
            link("repositories/1/out", "../../../etc"),
            link("repositories/1/abs", "/etc/passwd"),
            link("repositories/1/hard", "/etc/passwd", tarfile.LNKTYPE),
            link("repositories/1/ok", "index.html"),
            device,
            setuid,
        ]
        safe = data_replication._safe_members(members, "repositories")
        self.assertEqual([m.name for m in safe], ["1/ok", "1/run.sh"])
        self.assertEqual(safe[1].mode, 0o755)

    def test_ignores_members_outside_the_prefix(self):
        members = [self._member("app-data/1.tar"), self._member("repositories/1/index.html")]
        safe = data_replication._safe_members(members, "repositories")
        self.assertEqual([m.name for m in safe], ["1/index.html"])


class ApplySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.repository_root = Path(self.temp_directory.name) / "repositories"
        self.fake = FakeRuntime()
        patcher = patch.object(
            RuntimeManager, "container_runtime", new_callable=PropertyMock, return_value=self.fake
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        class _App:
            logger = __import__("logging").getLogger("test")

            def __init__(self, config, extensions):
                self.config = config
                self.extensions = extensions

        self.runtime_manager = RuntimeManager(app=None)
        self.app = _App(
            {"REPOSITORY_ROOT": str(self.repository_root)},
            {"runtime_manager": self.runtime_manager},
        )
        self.runtime_manager.app = self.app
        self.manager = data_replication.DataReplicationManager(self.app, "https://primary.example", "s3cret-s3cret-s3cret")

    def _build_archive(self, *, with_app_data=True) -> Path:
        archive_path = Path(self.temp_directory.name) / "snapshot.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo(name="repositories/1/index.html")
            body = b"<h1>hi</h1>"
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
            if with_app_data:
                app_tar = io.BytesIO()
                with tarfile.open(fileobj=app_tar, mode="w") as inner:
                    inner_info = tarfile.TarInfo(name="data/app.db")
                    inner_body = b"sqlite-bytes"
                    inner_info.size = len(inner_body)
                    inner.addfile(inner_info, io.BytesIO(inner_body))
                app_tar.seek(0)
                app_bytes = app_tar.read()
                app_info = tarfile.TarInfo(name="app-data/5.tar")
                app_info.size = len(app_bytes)
                archive.addfile(app_info, io.BytesIO(app_bytes))
        return archive_path

    def test_apply_snapshot_extracts_repositories_and_restores_app_data(self):
        archive_path = self._build_archive()
        self.manager._apply_snapshot(archive_path)

        self.assertEqual(
            (self.repository_root / "1" / "index.html").read_text(encoding="utf-8"), "<h1>hi</h1>"
        )
        restore_calls = [call for call in self.fake.calls if call[0] == "restore_data"]
        self.assertEqual(len(restore_calls), 1)
        _, volume_name, content = restore_calls[0]
        self.assertEqual(volume_name, self.fake.volume_name(5))
        with tarfile.open(fileobj=io.BytesIO(content)) as inner:
            self.assertEqual(inner.extractfile("data/app.db").read(), b"sqlite-bytes")

    def test_sync_once_survives_a_truncated_archive_and_keeps_the_files(self):
        self.repository_root.mkdir(parents=True)
        (self.repository_root / "keep.html").write_text("kept", encoding="utf-8")
        truncated = self._build_archive().read_bytes()[:700]

        class _Response:
            status = 200

            def __init__(self, data):
                self._data = io.BytesIO(data)

            def read(self, size=-1):
                return self._data.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.object(peer_http, "open_peer", return_value=_Response(truncated)):
            self.assertFalse(self.manager.sync_once())
        self.assertTrue(self.manager.last_error)
        self.assertEqual(
            (self.repository_root / "keep.html").read_text(encoding="utf-8"), "kept"
        )

    def test_apply_snapshot_wipes_stale_repositories_no_longer_present(self):
        stale = self.repository_root / "99" / "old.html"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")

        archive_path = self._build_archive(with_app_data=False)
        self.manager._apply_snapshot(archive_path)

        self.assertFalse(stale.exists())
        self.assertTrue((self.repository_root / "1" / "index.html").exists())


class DataSnapshotEndpointTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown

    def setUp(self):
        self.setUp_base()

    def test_requires_a_token(self):
        response = self.client.get("/replication/data-snapshot")
        self.assertEqual(response.status_code, 401)

    def test_returns_a_tar_of_the_repository_root_with_the_right_token(self):
        repository_root = Path(self.app.config["REPOSITORY_ROOT"])
        (repository_root / "42").mkdir(parents=True)
        (repository_root / "42" / "index.html").write_text("hi", encoding="utf-8")

        hub = self.app.extensions["mesh_hub"]
        hub.token = "s3cret-s3cret-s3cret"
        from webmanager.mesh import _hash_token

        hub._token_hash = _hash_token("s3cret-s3cret-s3cret")
        try:
            response = self.client.get(
                "/replication/data-snapshot", headers={"Authorization": "Bearer s3cret-s3cret-s3cret"}
            )
            self.assertEqual(response.status_code, 200)
            with tarfile.open(fileobj=io.BytesIO(response.data)) as archive:
                names = archive.getnames()
            self.assertIn("repositories/42/index.html", names)
        finally:
            hub.token = ""
            hub._token_hash = None


class SyncDataAdminRouteTests(unittest.TestCase):
    setUp_base = base.WebManagerTestCase.setUp
    tearDown = base.WebManagerTestCase.tearDown
    add_user = base.WebManagerTestCase.add_user
    login_user = base.WebManagerTestCase.login_user
    csrf = base.WebManagerTestCase.csrf

    def setUp(self):
        self.setUp_base()

    def test_sync_data_now_runs_locally_and_reports_failure(self):
        admin_id = self.add_user("root", is_admin=True)
        self.login_user(admin_id)
        manager = self.app.extensions["data_replication_manager"]
        manager.primary_url = "https://primary.example"
        manager.token = "s3cret-s3cret-s3cret"
        try:
            with patch.object(
                peer_http, "open_peer",
                side_effect=data_replication.urllib.error.URLError("no route"),
            ) as urlopen:
                response = self.client.post(
                    "/admin/replication/sync-data",
                    data={"_csrf_token": self.csrf()},
                    follow_redirects=True,
                )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Could not sync data from the primary", response.data)
            forwarded_request = urlopen.call_args[0][0]
            self.assertIn("/replication/data-snapshot", forwarded_request.full_url)
        finally:
            manager.primary_url = ""
            manager.token = ""

    def test_sync_data_now_requires_admin(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.post(
            "/admin/replication/sync-data", data={"_csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
