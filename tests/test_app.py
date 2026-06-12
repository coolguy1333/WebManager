import os
import json
import socket
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import redirect

from webmanager import create_app, db
from webmanager.analytics import site_analytics
from webmanager.auth import oauth
from webmanager.db import get_db
from webmanager.domains import default_domain
from webmanager.git_service import (
    GitError,
    clone_repository,
    find_index_folders,
    resolve_folder,
    validate_repo_url,
)
from webmanager.nginx import (
    NginxConfigError,
    build_main_config,
    build_site_config,
    config_uses_port,
    route_site_config,
    validate_site_config,
)
from webmanager.repository_refresh import MAX_REFRESH_MINUTES
from webmanager.services import RuntimeErrorDetail, allocate_port


class WebManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        root = Path(self.temp_directory.name)
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": str(root / "test.sqlite3"),
                "REPOSITORY_ROOT": str(root / "repositories"),
                "NGINX_ROOT": str(root / "nginx"),
                "LOG_ROOT": str(root / "logs"),
                "SITE_PORT_MIN": 43100,
                "SITE_PORT_MAX": 43120,
                "SITE_GATEWAY_PORT": 43099,
                "SITE_BASE_DOMAIN": "webmanager.example",
                "SITE_PUBLIC_SCHEME": "https",
                "NGINX_BINARY": "definitely-not-installed-nginx",
                "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET": "test-client-secret",
                "GOOGLE_REDIRECT_URI": "https://webmanager.example/auth/google/callback",
                "PROGRAM_UPDATE_STATUS_FILE": str(root / "update-status.json"),
                "PROGRAM_UPDATE_REQUEST_FILE": str(
                    root / "update-requests" / "install.commit"
                ),
                "PROGRAM_UPDATE_CHECK_REQUEST_FILE": str(
                    root / "update-requests" / "check"
                ),
            }
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            self.google_oauth = oauth.google

    def tearDown(self):
        self.temp_directory.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "test-csrf"
        return "test-csrf"

    def add_user(
        self,
        username,
        email=None,
        google_sub=None,
        *,
        is_admin=False,
        is_active=True,
    ):
        with self.app.app_context():
            database = get_db()
            cursor = database.execute(
                """
                INSERT INTO users (
                    username, password_hash, email, google_sub, auth_provider,
                    is_admin, is_active
                ) VALUES (?, 'legacy-password-hash', ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    email,
                    google_sub,
                    "google" if google_sub else "legacy",
                    int(is_admin),
                    int(is_active),
                ),
            )
            database.commit()
            return cursor.lastrowid

    def login_user(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def google_claims(self, **overrides):
        claims = {
            "sub": "google-subject-123",
            "email": "alice@example.com",
            "email_verified": True,
            "name": "Alice Example",
            "picture": "https://example.com/alice.png",
        }
        claims.update(overrides)
        return claims

    def google_callback(self, claims=None):
        with self.client.session_transaction() as session:
            session["google_oidc_nonce"] = "test-nonce"
        with patch.object(
            self.google_oauth,
            "authorize_access_token",
            return_value={"userinfo": claims or self.google_claims()},
        ):
            return self.client.get("/auth/google/callback")

    def add_repository(self, user_id, document_root):
        repository_root = document_root.parent
        with self.app.app_context():
            database = get_db()
            cursor = database.execute(
                """
                INSERT INTO repositories (user_id, name, url, local_path, status)
                VALUES (?, 'demo', 'https://example.com/demo.git', ?, 'ready')
                """,
                (user_id, str(repository_root)),
            )
            database.commit()
            return cursor.lastrowid

    def add_site(
        self,
        user_id,
        repository_id,
        document_root,
        *,
        port=43100,
        status="stopped",
        runtime_backend=None,
    ):
        config = build_site_config(
            "Demo",
            document_root,
            "index.html",
            port,
            True,
            "demo.webmanager.example",
            43099,
        )
        with self.app.app_context():
            database = get_db()
            repository = database.execute(
                "SELECT local_path FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            domain_id = database.execute(
                "SELECT id FROM domains WHERE is_default = 1 LIMIT 1"
            ).fetchone()["id"]
            repository_root = Path(repository["local_path"]).resolve()
            resolved_document_root = Path(document_root).resolve()
            try:
                relative_folder = resolved_document_root.relative_to(repository_root)
            except ValueError:
                folder = "."
            else:
                folder = "." if relative_folder == Path(".") else relative_folder.as_posix()
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, domain_id, name, slug, folder, document_root,
                    index_file, port, spa_fallback, nginx_config, status,
                    runtime_backend
                ) VALUES (?, ?, ?, 'Demo', 'demo', ?, ?, 'index.html', ?, 1, ?, ?, ?)
                """,
                (
                    user_id,
                    repository_id,
                    domain_id,
                    folder,
                    str(document_root),
                    port,
                    config,
                    status,
                    runtime_backend,
                ),
            )
            database.commit()
            return cursor.lastrowid

    def test_login_page_uses_google_only(self):
        response = self.client.get("/auth/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Continue with Google", response.data)
        self.assertNotIn(b"Password", response.data)
        self.assertIn(b"favicon.svg", response.data)
        self.assertIn(b"logo-mark.svg", response.data)

    def test_google_login_starts_oidc_with_configured_callback(self):
        with patch.object(
            self.google_oauth,
            "authorize_redirect",
            return_value=redirect("https://accounts.google.com/o/oauth2/v2/auth"),
        ) as authorize:
            response = self.client.get("/auth/google?next=/sites/7")
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com", response.headers["Location"])
        self.assertEqual(
            authorize.call_args.args[0],
            "https://webmanager.example/auth/google/callback",
        )
        self.assertEqual(authorize.call_args.kwargs["prompt"], "select_account")
        self.assertTrue(authorize.call_args.kwargs["nonce"])

    def test_google_login_uses_approved_dashboard_alias_callback(self):
        with self.app.app_context():
            database = get_db()
            database.execute(
                "INSERT INTO dashboard_domains (name) VALUES ('control.example')"
            )
            database.commit()
        with patch.object(
            self.google_oauth,
            "authorize_redirect",
            return_value=redirect("https://accounts.google.com/o/oauth2/v2/auth"),
        ) as authorize:
            response = self.client.get(
                "/auth/google",
                base_url="https://control.example",
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            authorize.call_args.args[0],
            "https://control.example/auth/google/callback",
        )

    def test_google_login_rejects_external_return_url(self):
        with patch.object(
            self.google_oauth,
            "authorize_redirect",
            return_value=redirect("https://accounts.google.com/o/oauth2/v2/auth"),
        ):
            response = self.client.get("/auth/google?next=https://evil.example/steal")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("post_login_next"))

    def test_google_callback_creates_user_and_signs_in(self):
        response = self.google_callback()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users").fetchone()
            self.assertEqual(user["google_sub"], "google-subject-123")
            self.assertEqual(user["email"], "alice@example.com")
            self.assertEqual(user["display_name"], "Alice Example")
            self.assertEqual(user["is_admin"], 1)
            user_id = user["id"]

        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], user_id)

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sites at a glance", response.data)
        self.assertIn(b"Alice Example", response.data)

    def test_health_endpoint_checks_database(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_security_headers_are_set(self):
        response = self.client.get("/auth/login")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_invalid_csrf_uses_friendly_error_page(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid CSRF token", response.data)

    def test_registration_redirects_to_google_login(self):
        response = self.client.get("/auth/register")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/login"))

    def test_google_callback_rejects_unverified_email(self):
        response = self.google_callback(self.google_claims(email_verified=False))
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT id FROM users").fetchone())

    def test_google_workspace_domain_allowlist_uses_hd_claim(self):
        self.app.config["GOOGLE_ALLOWED_DOMAINS"] = "example.com"
        denied = self.google_callback(self.google_claims(email="alice@example.com"))
        self.assertEqual(denied.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT id FROM users").fetchone())

        allowed = self.google_callback(
            self.google_claims(sub="allowed-subject", hd="example.com")
        )
        self.assertEqual(allowed.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(get_db().execute("SELECT id FROM users").fetchone())

    def test_google_email_allowlist_permits_listed_account(self):
        self.app.config["GOOGLE_ALLOWED_EMAILS"] = "alice@example.com"
        response = self.google_callback()
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(get_db().execute("SELECT id FROM users").fetchone())

    def test_google_login_fails_closed_without_credentials(self):
        self.app.config["GOOGLE_CLIENT_ID"] = ""
        response = self.client.get("/auth/google", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"has not been configured", response.data)

    def test_google_callback_links_existing_user_by_preconfigured_email(self):
        user_id = self.add_user("legacy-user", email="alice@example.com")
        response = self.google_callback()
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            self.assertEqual(user["google_sub"], "google-subject-123")
            self.assertEqual(user["auth_provider"], "google")

    def test_user_cannot_open_another_users_repository(self):
        alice_id = self.add_user("alice")
        bob_id = self.add_user("bob")
        repository_root = Path(self.temp_directory.name) / "other-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(alice_id, repository_root)

        self.login_user(bob_id)
        response = self.client.get(f"/repositories/{repository_id}/select")
        self.assertEqual(response.status_code, 404)

        with self.app.app_context():
            self.assertIsNotNone(get_db().execute("SELECT 1 FROM users WHERE id = ?", (bob_id,)).fetchone())

    def test_only_repository_owner_or_manager_can_approve_site_update(self):
        alice_id = self.add_user("alice")
        bob_id = self.add_user("bob")
        repository_root = Path(self.temp_directory.name) / "approval-owner-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(alice_id, repository_root)
        self.login_user(bob_id)

        with patch.object(
            self.app.extensions["repository_refresh_manager"],
            "apply_pending",
        ) as apply_pending:
            response = self.client.post(
                f"/repositories/{repository_id}/updates/approve",
                data={"_csrf_token": self.csrf()},
            )

        self.assertEqual(response.status_code, 404)
        apply_pending.assert_not_called()

    def test_non_admin_cannot_open_admin_console(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 403)

    def test_admin_console_warns_when_google_access_is_unrestricted(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Google sign-in is unrestricted", response.data)

    def test_admin_console_keeps_workspaces_separate(self):
        admin_id = self.add_user("admin", is_admin=True)
        user_id = self.add_user("operator")
        repository_root = Path(self.temp_directory.name) / "admin-sections-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(user_id, repository_id, repository_root)
        with self.app.app_context():
            database = get_db()
            group_id = database.execute(
                "INSERT INTO groups (name) VALUES ('Operators')"
            ).lastrowid
            pool_id = database.execute(
                "INSERT INTO pools (name) VALUES ('Production')"
            ).lastrowid
            database.commit()
        self.login_user(admin_id)

        overview = self.client.get("/admin/")
        self.assertIn(b"Access model", overview.data)
        self.assertNotIn(f"/admin/users/{user_id}".encode(), overview.data)
        self.assertNotIn(f"/admin/groups/{group_id}".encode(), overview.data)

        people = self.client.get("/admin/?section=users")
        self.assertIn(f"/admin/users/{user_id}".encode(), people.data)
        self.assertNotIn(b'action="/admin/groups"', people.data)

        groups = self.client.get("/admin/?section=groups")
        self.assertIn(f"/admin/groups/{group_id}".encode(), groups.data)
        self.assertNotIn(f"/admin/users/{user_id}".encode(), groups.data)

        pools = self.client.get("/admin/access?section=pools")
        self.assertIn(f"/admin/pools/{pool_id}".encode(), pools.data)
        self.assertNotIn(f"/admin/sites/{site_id}/access".encode(), pools.data)

        site_access = self.client.get("/admin/access?section=sites")
        self.assertIn(f"/admin/sites/{site_id}/access".encode(), site_access.data)
        self.assertNotIn(f"/admin/pools/{pool_id}".encode(), site_access.data)

    def test_super_admin_can_add_domain_and_assign_it_to_site(self):
        admin_id = self.add_user("admin", is_admin=True)
        owner_id = self.add_user("owner")
        repository_root = Path(self.temp_directory.name) / "domain-site-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        self.login_user(admin_id)

        response = self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "school-sites.example",
                "is_default": "on",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Domain school-sites.example added", response.data)
        self.assertIn(b"*.school-sites.example", response.data)
        self.assertIn(b"http://localhost:8080", response.data)
        self.assertIn(b"Host a site at the domain root", response.data)
        self.assertIn(b"WEBMANAGER-LAN-IP:8080", response.data)
        self.assertIn(b"Always include <code>:8080</code>", response.data)

        with self.app.app_context():
            domain_id = get_db().execute(
                "SELECT id FROM domains WHERE name = 'school-sites.example'"
            ).fetchone()["id"]

        self.login_user(owner_id)
        with patch.object(
            self.app.extensions["runtime_manager"],
            "sync_nginx_configs",
        ):
            response = self.client.post(
                f"/sites/{site_id}/settings",
                data={
                    "_csrf_token": self.csrf(),
                    "name": "Demo",
                    "slug": "demo",
                    "folder": "domain-site-repo",
                    "port": "43100",
                    "domain_id": str(domain_id),
                    "spa_fallback": "on",
                },
                follow_redirects=True,
            )
        self.assertIn(b"Site settings saved", response.data)
        with self.app.app_context():
            site = get_db().execute(
                "SELECT * FROM sites WHERE id = ?",
                (site_id,),
            ).fetchone()
        self.assertEqual(site["domain_id"], domain_id)
        self.assertIn(
            "server_name demo.school-sites.example",
            site["nginx_config"],
        )

        self.login_user(admin_id)
        blocked = self.client.post(
            f"/admin/domains/{domain_id}/delete",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"Move the 1 assigned site", blocked.data)

    def test_delegated_admin_cannot_manage_domains(self):
        manager_id = self.add_user("manager")
        with self.app.app_context():
            database = get_db()
            group_id = database.execute(
                "INSERT INTO groups (name) VALUES ('Access managers')"
            ).lastrowid
            database.execute(
                """
                INSERT INTO group_permissions (group_id, permission_code)
                VALUES (?, 'access.manage')
                """,
                (group_id,),
            )
            database.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (manager_id, group_id),
            )
            database.commit()
        self.login_user(manager_id)

        self.assertEqual(self.client.get("/admin/domains").status_code, 403)
        response = self.client.post(
            "/admin/domains",
            data={"_csrf_token": self.csrf(), "name": "blocked.example"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/admin/domains/dashboard",
            data={"_csrf_token": self.csrf(), "name": "control.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_super_admin_can_reset_default_domain_persistently(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)

        page = self.client.get("/admin/domains")
        self.assertIn(b"Reset default", page.data)

        response = self.client.post(
            "/admin/domains/default/reset",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"Default deployment domain cleared", response.data)
        self.assertIn(b"No default deployment domain is set", response.data)

        with self.app.app_context():
            database = get_db()
            self.assertIsNone(
                database.execute(
                    "SELECT id FROM domains WHERE is_default = 1"
                ).fetchone()
            )
            self.assertIsNone(default_domain(database))
            db.init_db()
            self.assertIsNone(
                database.execute(
                    "SELECT id FROM domains WHERE is_default = 1"
                ).fetchone()
            )

        self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "another.example",
            },
        )
        with self.app.app_context():
            self.assertIsNone(
                get_db().execute(
                    "SELECT id FROM domains WHERE is_default = 1"
                ).fetchone()
            )

    def test_non_admin_cannot_reset_default_domain(self):
        user_id = self.add_user("member")
        self.login_user(user_id)

        response = self.client.post(
            "/admin/domains/default/reset",
            data={"_csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 403)

    def test_super_admin_can_manage_multiple_dashboard_domains(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)
        runtime = self.app.extensions["runtime_manager"]

        with patch.object(runtime, "apply_nginx_configs"):
            response = self.client.post(
                "/admin/domains/dashboard",
                data={
                    "_csrf_token": self.csrf(),
                    "name": "control.example",
                },
                follow_redirects=True,
            )
        self.assertIn(b"Dashboard domain control.example added", response.data)
        self.assertIn(
            b"https://control.example/auth/google/callback",
            response.data,
        )

        with self.app.app_context():
            database = get_db()
            alias = database.execute(
                "SELECT * FROM dashboard_domains WHERE name = 'control.example'"
            ).fetchone()
            original = database.execute(
                "SELECT * FROM dashboard_domains WHERE name = 'webmanager.example'"
            ).fetchone()
        self.assertEqual(alias["is_primary"], 0)

        with patch.object(runtime, "apply_nginx_configs"):
            response = self.client.post(
                f"/admin/domains/dashboard/{alias['id']}/primary",
                data={"_csrf_token": self.csrf()},
                follow_redirects=True,
            )
        self.assertIn(b"primary dashboard domain", response.data)

        refused = self.client.post(
            f"/admin/domains/dashboard/{alias['id']}/delete",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"Make another dashboard domain primary", refused.data)

        with patch.object(runtime, "apply_nginx_configs"):
            self.client.post(
                f"/admin/domains/dashboard/{original['id']}/primary",
                data={"_csrf_token": self.csrf()},
            )
            deleted = self.client.post(
                f"/admin/domains/dashboard/{alias['id']}/delete",
                data={"_csrf_token": self.csrf()},
                follow_redirects=True,
            )
        self.assertIn(b"Dashboard domain control.example removed", deleted.data)

    def test_dashboard_domain_cannot_be_added_as_deployment_domain(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)
        response = self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "webmanager.example",
            },
            follow_redirects=True,
        )
        self.assertIn(
            b"A dashboard domain cannot also be used for deployments",
            response.data,
        )

    def test_domain_blocklist_blocks_parent_and_subdomains(self):
        admin_id = self.add_user("admin", is_admin=True)
        owner_id = self.add_user("owner")
        repository_root = Path(self.temp_directory.name) / "blocked-domain-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        self.login_user(admin_id)

        response = self.client.post(
            "/admin/domains/blocklist",
            data={
                "_csrf_token": self.csrf(),
                "blocked_domains": "webmanager.example\ninternal.test",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Domain blocklist saved with 2 entries", response.data)
        self.assertIn(b"Blocked", response.data)
        self.assertIn(b"Existing sites remain online until moved", response.data)

        refused = self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "school.webmanager.example",
            },
            follow_redirects=True,
        )
        self.assertIn(b"blocked by the deployment-domain blocklist", refused.data)
        with self.app.app_context():
            self.assertIsNone(
                get_db().execute(
                    "SELECT id FROM domains WHERE name = 'school.webmanager.example'"
                ).fetchone()
            )
            site = get_db().execute(
                "SELECT * FROM sites WHERE id = ?",
                (site_id,),
            ).fetchone()
        self.assertIn("server_name demo.webmanager.example", site["nginx_config"])

        self.login_user(owner_id)
        settings = self.client.get(f"/sites/{site_id}/settings")
        self.assertIn(b"blocked; move recommended", settings.data)
        refused_change = self.client.post(
            f"/sites/{site_id}/settings",
            data={
                "_csrf_token": self.csrf(),
                "name": "Demo",
                "slug": "demo-new",
                "folder": "blocked-domain-repo",
                "port": "43100",
                "domain_id": str(site["domain_id"]),
                "spa_fallback": "on",
            },
            follow_redirects=True,
        )
        self.assertIn(
            b"Keep the existing public hostname or move the site",
            refused_change.data,
        )
        with self.app.app_context():
            unchanged = get_db().execute(
                "SELECT slug, use_domain_root FROM sites WHERE id = ?",
                (site_id,),
            ).fetchone()
        self.assertEqual(unchanged["slug"], "demo")
        self.assertEqual(unchanged["use_domain_root"], 0)

    def test_deleting_default_domain_does_not_promote_blocked_domain(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)

        self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "blocked.example",
            },
        )
        self.client.post(
            "/admin/domains",
            data={
                "_csrf_token": self.csrf(),
                "name": "replacement.example",
                "is_default": "on",
            },
        )
        self.client.post(
            "/admin/domains/blocklist",
            data={
                "_csrf_token": self.csrf(),
                "blocked_domains": "blocked.example",
            },
        )
        with self.app.app_context():
            database = get_db()
            replacement_id = database.execute(
                "SELECT id FROM domains WHERE name = 'replacement.example'"
            ).fetchone()["id"]

        self.client.post(
            f"/admin/domains/{replacement_id}/delete",
            data={"_csrf_token": self.csrf()},
        )

        with self.app.app_context():
            default = get_db().execute(
                "SELECT name FROM domains WHERE is_default = 1"
            ).fetchone()
        self.assertIsNotNone(default)
        self.assertNotEqual(default["name"], "blocked.example")

    def test_admin_can_create_group_and_assign_it_to_user(self):
        admin_id = self.add_user("admin", is_admin=True)
        user_id = self.add_user("operator")
        self.login_user(admin_id)

        response = self.client.post(
            "/admin/groups",
            data={
                "_csrf_token": self.csrf(),
                "name": "Operators",
                "description": "Deployment operators",
                "permissions": ["resources.view_all", "resources.manage_all"],
            },
            follow_redirects=True,
        )
        self.assertIn(b"Group Operators created", response.data)
        with self.app.app_context():
            group = get_db().execute(
                "SELECT * FROM groups WHERE name = 'Operators'"
            ).fetchone()

        response = self.client.post(
            f"/admin/users/{user_id}",
            data={
                "_csrf_token": self.csrf(),
                "is_active": "on",
                "groups": [str(group["id"])],
            },
            follow_redirects=True,
        )
        self.assertIn(b"Updated operator", response.data)
        with self.app.app_context():
            permissions = {
                row["permission_code"]
                for row in get_db().execute(
                    """
                    SELECT group_permissions.permission_code
                    FROM user_groups
                    JOIN group_permissions
                      ON group_permissions.group_id = user_groups.group_id
                    WHERE user_groups.user_id = ?
                    """,
                    (user_id,),
                ).fetchall()
            }
        self.assertEqual(
            permissions,
            {"resources.view_all", "resources.manage_all"},
        )

    def test_view_all_permission_is_read_only_for_other_users_resources(self):
        owner_id = self.add_user("owner")
        viewer_id = self.add_user("viewer")
        repository_root = Path(self.temp_directory.name) / "shared-view-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        with self.app.app_context():
            database = get_db()
            group = database.execute(
                "INSERT INTO groups (name) VALUES ('Auditors')"
            )
            database.execute(
                """
                INSERT INTO group_permissions (group_id, permission_code)
                VALUES (?, 'resources.view_all')
                """,
                (group.lastrowid,),
            )
            database.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (viewer_id, group.lastrowid),
            )
            database.commit()
        self.login_user(viewer_id)

        dashboard = self.client.get("/")
        self.assertIn(b"Owner:", dashboard.data)
        self.assertIn(b"Read only", dashboard.data)
        self.assertEqual(self.client.get(f"/sites/{site_id}").status_code, 200)
        denied = self.client.post(
            f"/sites/{site_id}/start",
            data={"_csrf_token": self.csrf()},
        )
        self.assertEqual(denied.status_code, 404)

    def test_manage_all_permission_can_control_other_users_site(self):
        owner_id = self.add_user("owner")
        operator_id = self.add_user("operator")
        repository_root = Path(self.temp_directory.name) / "managed-site-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        with self.app.app_context():
            database = get_db()
            group = database.execute(
                "INSERT INTO groups (name) VALUES ('Global operators')"
            )
            database.execute(
                """
                INSERT INTO group_permissions (group_id, permission_code)
                VALUES (?, 'resources.manage_all')
                """,
                (group.lastrowid,),
            )
            database.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (operator_id, group.lastrowid),
            )
            database.commit()
        self.login_user(operator_id)

        with patch.object(
            self.app.extensions["runtime_manager"],
            "start_site",
            return_value="nginx",
        ):
            response = self.client.post(
                f"/sites/{site_id}/start",
                data={"_csrf_token": self.csrf()},
            )
        self.assertEqual(response.status_code, 302)

    def test_direct_site_viewer_can_see_but_not_control_site(self):
        owner_id = self.add_user("owner")
        viewer_id = self.add_user("viewer")
        repository_root = Path(self.temp_directory.name) / "direct-view-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                INSERT INTO site_acl (site_id, user_id, role)
                VALUES (?, ?, 'viewer')
                """,
                (site_id, viewer_id),
            )
            database.commit()
        self.login_user(viewer_id)

        dashboard = self.client.get("/")
        self.assertIn(b"Demo", dashboard.data)
        self.assertIn(b"View &rarr;", dashboard.data)
        self.assertEqual(self.client.get(f"/sites/{site_id}").status_code, 200)
        denied = self.client.post(
            f"/sites/{site_id}/start",
            data={"_csrf_token": self.csrf()},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(
            self.client.get(f"/repositories/{repository_id}/select").status_code,
            404,
        )

    def test_pool_group_operator_can_control_only_pooled_site(self):
        owner_id = self.add_user("owner")
        operator_id = self.add_user("operator")
        repository_root = Path(self.temp_directory.name) / "pool-operator-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        with self.app.app_context():
            database = get_db()
            group_id = database.execute(
                "INSERT INTO groups (name) VALUES ('Web operators')"
            ).lastrowid
            pool_id = database.execute(
                "INSERT INTO pools (name) VALUES ('Production')"
            ).lastrowid
            database.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (operator_id, group_id),
            )
            database.execute(
                "INSERT INTO pool_sites (pool_id, site_id) VALUES (?, ?)",
                (pool_id, site_id),
            )
            database.execute(
                """
                INSERT INTO pool_acl (pool_id, group_id, role)
                VALUES (?, ?, 'operator')
                """,
                (pool_id, group_id),
            )
            database.commit()
        self.login_user(operator_id)

        dashboard = self.client.get("/")
        self.assertIn(b"Pool: Production", dashboard.data)
        self.assertIn(b"Manage &rarr;", dashboard.data)
        with patch.object(
            self.app.extensions["runtime_manager"],
            "start_site",
            return_value="nginx",
        ):
            response = self.client.post(
                f"/sites/{site_id}/start",
                data={"_csrf_token": self.csrf()},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.get(f"/repositories/{repository_id}/select").status_code,
            404,
        )

    def test_admin_can_assign_pool_access_and_delete_pool_without_site(self):
        admin_id = self.add_user("admin", is_admin=True)
        owner_id = self.add_user("owner")
        viewer_id = self.add_user("viewer")
        repository_root = Path(self.temp_directory.name) / "admin-pool-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(owner_id, repository_root)
        site_id = self.add_site(owner_id, repository_id, repository_root)
        self.login_user(admin_id)

        access_page = self.client.get("/admin/access")
        self.assertEqual(access_page.status_code, 200)
        self.assertIn(b"Pools and access", access_page.data)
        self.assertIn(b"Direct site access", access_page.data)

        response = self.client.post(
            "/admin/pools",
            data={
                "_csrf_token": self.csrf(),
                "name": "Customer sites",
                "description": "Delegated customer deployments",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            pool_id = get_db().execute(
                "SELECT id FROM pools WHERE name = 'Customer sites'"
            ).fetchone()["id"]

        response = self.client.post(
            f"/admin/pools/{pool_id}",
            data={
                "_csrf_token": self.csrf(),
                "name": "Customer sites",
                "sites": [str(site_id)],
                f"user_role_{viewer_id}": "viewer",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.login_user(viewer_id)
        self.assertEqual(self.client.get(f"/sites/{site_id}").status_code, 200)

        self.login_user(admin_id)
        response = self.client.post(
            f"/admin/pools/{pool_id}/delete",
            data={"_csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(
                get_db().execute(
                    "SELECT id FROM sites WHERE id = ?", (site_id,)
                ).fetchone()
            )
        self.login_user(viewer_id)
        self.assertEqual(self.client.get(f"/sites/{site_id}").status_code, 404)

    def test_final_active_admin_cannot_be_demoted(self):
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)
        response = self.client.post(
            f"/admin/users/{admin_id}",
            data={
                "_csrf_token": self.csrf(),
                "is_active": "on",
            },
            follow_redirects=True,
        )
        self.assertIn(b"final active administrator", response.data)
        with self.app.app_context():
            admin = get_db().execute(
                "SELECT * FROM users WHERE id = ?",
                (admin_id,),
            ).fetchone()
            self.assertEqual(admin["is_admin"], 1)

    def test_delegated_user_manager_cannot_grant_permissions_they_lack(self):
        manager_id = self.add_user("manager")
        target_id = self.add_user("target")
        with self.app.app_context():
            database = get_db()
            user_managers = database.execute(
                "INSERT INTO groups (name) VALUES ('User managers')"
            ).lastrowid
            operators = database.execute(
                "INSERT INTO groups (name) VALUES ('Operators')"
            ).lastrowid
            database.execute(
                """
                INSERT INTO group_permissions (group_id, permission_code)
                VALUES (?, 'users.manage')
                """,
                (user_managers,),
            )
            database.execute(
                """
                INSERT INTO group_permissions (group_id, permission_code)
                VALUES (?, 'resources.manage_all')
                """,
                (operators,),
            )
            database.execute(
                "INSERT INTO user_groups (user_id, group_id) VALUES (?, ?)",
                (manager_id, user_managers),
            )
            database.commit()
        self.login_user(manager_id)

        response = self.client.post(
            f"/admin/users/{target_id}",
            data={
                "_csrf_token": self.csrf(),
                "is_active": "on",
                "groups": [str(operators)],
            },
        )
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            membership = get_db().execute(
                "SELECT 1 FROM user_groups WHERE user_id = ? AND group_id = ?",
                (target_id, operators),
            ).fetchone()
            self.assertIsNone(membership)

    def test_only_super_admin_can_approve_exact_program_update(self):
        commit = "a" * 40
        status_path = Path(self.app.config["PROGRAM_UPDATE_STATUS_FILE"])
        status_path.write_text(
            json.dumps(
                {
                    "state": "available",
                    "installed_commit": "b" * 40,
                    "available_commit": commit,
                    "update_available": True,
                    "message": "Update available.",
                    "checked_at": "2026-06-11 12:00:00 UTC",
                }
            ),
            encoding="utf-8",
        )

        user_id = self.add_user("manager")
        self.login_user(user_id)
        denied = self.client.post(
            "/admin/updates/install",
            data={"_csrf_token": self.csrf(), "commit": commit},
        )
        self.assertEqual(denied.status_code, 403)

        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)
        response = self.client.post(
            "/admin/updates/install",
            data={"_csrf_token": self.csrf(), "commit": commit},
            follow_redirects=True,
        )
        self.assertIn(b"Update approved", response.data)
        request_path = Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"])
        self.assertEqual(request_path.read_text(encoding="ascii").strip(), commit)

    def test_only_super_admin_can_request_program_update_check(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.post(
            "/admin/updates/check",
            data={"_csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 403)

        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)
        response = self.client.post(
            "/admin/updates/check",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"Update check requested", response.data)
        check_path = Path(
            self.app.config["PROGRAM_UPDATE_CHECK_REQUEST_FILE"]
        )
        self.assertEqual(check_path.read_text(encoding="ascii").strip(), "check")

    def test_program_update_approval_rejects_stale_commit(self):
        available = "c" * 40
        Path(self.app.config["PROGRAM_UPDATE_STATUS_FILE"]).write_text(
            json.dumps(
                {
                    "state": "available",
                    "installed_commit": "b" * 40,
                    "available_commit": available,
                    "update_available": True,
                }
            ),
            encoding="utf-8",
        )
        admin_id = self.add_user("admin", is_admin=True)
        self.login_user(admin_id)

        response = self.client.post(
            "/admin/updates/install",
            data={"_csrf_token": self.csrf(), "commit": "d" * 40},
            follow_redirects=True,
        )

        self.assertIn(b"no longer available", response.data)
        self.assertFalse(
            Path(self.app.config["PROGRAM_UPDATE_REQUEST_FILE"]).exists()
        )

    def test_disabled_user_session_is_rejected(self):
        user_id = self.add_user("disabled", is_active=False)
        self.login_user(user_id)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])

    def test_deploy_generates_unique_port_and_owned_site(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "repo"
        site_root = repository_root / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        self.login_user(user_id)

        with patch("webmanager.services.RuntimeManager.start_site", return_value="builtin"):
            response = self.client.post(
                f"/repositories/{repository_id}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "site_name": "Demo Site",
                    "folder": "public",
                    "spa_fallback": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            site = get_db().execute("SELECT * FROM sites").fetchone()
            self.assertEqual(site["user_id"], user_id)
            self.assertEqual(site["document_root"], str(site_root.resolve()))
            self.assertTrue(config_uses_port(site["nginx_config"], site["port"]))
            self.assertTrue(config_uses_port(site["nginx_config"], 43099))
            self.assertIn(
                "server_name demo-site.webmanager.example",
                site["nginx_config"],
            )
            self.assertIn("try_files $uri $uri/ /index.html", site["nginx_config"])
            get_db().execute(
                "UPDATE sites SET status = 'running' WHERE id = ?",
                (site["id"],),
            )
            get_db().commit()

        response = self.client.get("/")
        self.assertIn(b"https://demo-site.webmanager.example", response.data)
        self.assertIn(
            b'href="https://demo-site.webmanager.example" target="_blank" rel="noopener noreferrer"',
            response.data,
        )
        response = self.client.get(f"/sites/{site['id']}")
        self.assertIn(b'href="https://demo-site.webmanager.example"', response.data)
        self.assertIn(b"*.webmanager.example", response.data)
        self.assertIn(b"http://localhost:8080", response.data)
        self.assertIn(
            b"curl -I -H &#39;Host: demo-site.webmanager.example&#39;",
            response.data,
        )
        self.assertIn(
            b'href="https://demo-site.webmanager.example" target="_blank" rel="noopener noreferrer"',
            response.data,
        )

    def test_multiple_site_folders_can_be_deployed_together(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "multi-site-repo"
        for folder in ("docs", "status"):
            site_root = repository_root / folder
            site_root.mkdir(parents=True)
            (site_root / "index.html").write_text(folder, encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root / "docs")
        self.login_user(user_id)

        with patch("webmanager.services.RuntimeManager.start_site", return_value="nginx"):
            response = self.client.post(
                f"/repositories/{repository_id}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "multi_deploy": "1",
                    "selected": ["0", "1"],
                    "folder_0": "docs",
                    "site_name_0": "Documentation",
                    "spa_fallback_0": "on",
                    "folder_1": "status",
                    "site_name_1": "Status",
                },
                follow_redirects=True,
            )

        self.assertIn(b"Deployed 2 sites", response.data)
        with self.app.app_context():
            sites = get_db().execute(
                "SELECT name, folder, port FROM sites ORDER BY name"
            ).fetchall()
        self.assertEqual(
            [(site["name"], site["folder"]) for site in sites],
            [("Documentation", "docs"), ("Status", "status")],
        )
        self.assertNotEqual(sites[0]["port"], sites[1]["port"])

    def test_site_can_be_deployed_at_domain_root(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "root-domain-repo"
        site_root = repository_root / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("root", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        with self.app.app_context():
            database = get_db()
            domain_id = database.execute(
                "INSERT INTO domains (name) VALUES ('mhsit.club')"
            ).lastrowid
            database.commit()
        self.login_user(user_id)

        selection = self.client.get(
            f"/repositories/{repository_id}/select"
        )
        self.assertIn(b"Choose public address style", selection.data)
        self.assertIn(b"Site subdomain", selection.data)
        self.assertIn(b"Domain root", selection.data)
        self.assertIn(b'data-root-available="true"', selection.data)

        with patch("webmanager.services.RuntimeManager.start_site", return_value="nginx"):
            response = self.client.post(
                f"/repositories/{repository_id}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "multi_deploy": "1",
                    "selected": "0",
                    "folder_0": "public",
                    "site_name_0": "Main site",
                    "spa_fallback_0": "on",
                    "domain_id": str(domain_id),
                    "hosting_mode": "root",
                },
                follow_redirects=True,
            )

        self.assertIn(b"https://mhsit.club", response.data)
        with self.app.app_context():
            site = get_db().execute(
                "SELECT * FROM sites WHERE domain_id = ?",
                (domain_id,),
            ).fetchone()
        self.assertEqual(site["use_domain_root"], 1)
        self.assertIn("server_name mhsit.club", site["nginx_config"])
        self.assertNotIn("server_name main-site.mhsit.club", site["nginx_config"])
        self.assertIn(
            b"Cloudflare Tunnel must include this exact root hostname",
            response.data,
        )
        self.assertIn(b"wildcard hostname does not cover the domain root", response.data)
        self.assertIn(b"Keep <code>:8080</code>", response.data)

        dashboard = self.client.get("/")
        self.assertIn(b">mhsit.club<", dashboard.data)
        self.assertNotIn(b"main-site.mhsit.club", dashboard.data)

    def test_site_can_be_deployed_on_multiple_domains(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "multi-domain-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("multi", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            database = get_db()
            primary_id = database.execute(
                "INSERT INTO domains (name) VALUES ('primary.example')"
            ).lastrowid
            alias_id = database.execute(
                "INSERT INTO domains (name) VALUES ('alias.example')"
            ).lastrowid
            database.commit()
        self.login_user(user_id)

        with patch("webmanager.services.RuntimeManager.start_site", return_value="nginx"):
            response = self.client.post(
                f"/repositories/{repository_id}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "multi_deploy": "1",
                    "selected": "0",
                    "folder_0": "multi-domain-repo",
                    "site_name_0": "Multi Domain",
                    "spa_fallback_0": "on",
                    "domain_id": str(primary_id),
                    "additional_domain_ids": str(alias_id),
                    "hosting_mode": "subdomain",
                },
                follow_redirects=True,
            )

        self.assertIn(b"https://multi-domain.primary.example", response.data)
        self.assertIn(b"https://multi-domain.alias.example", response.data)
        self.assertIn(b"route every domain used by this site", response.data)
        with self.app.app_context():
            database = get_db()
            site = database.execute(
                "SELECT * FROM sites WHERE domain_id = ?",
                (primary_id,),
            ).fetchone()
            alias = database.execute(
                """
                SELECT * FROM site_domain_aliases
                WHERE site_id = ? AND domain_id = ?
                """,
                (site["id"], alias_id),
            ).fetchone()
        self.assertIsNotNone(alias)
        self.assertIn(
            "server_name multi-domain.primary.example multi-domain.alias.example;",
            site["nginx_config"],
        )

    def test_root_domain_alias_cannot_replace_an_existing_root_site(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "root-alias-conflict"
        first_root = repository_root / "first"
        second_root = repository_root / "second"
        first_root.mkdir(parents=True)
        second_root.mkdir()
        (first_root / "index.html").write_text("first", encoding="utf-8")
        (second_root / "index.html").write_text("second", encoding="utf-8")
        repository_id = self.add_repository(user_id, first_root)
        first_site_id = self.add_site(user_id, repository_id, first_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET slug = 'first' WHERE id = ?",
                (first_site_id,),
            )
            database.commit()
        second_site_id = self.add_site(
            user_id,
            repository_id,
            second_root,
            port=43101,
        )
        with self.app.app_context():
            database = get_db()
            occupied_id = database.execute(
                "INSERT INTO domains (name) VALUES ('occupied.example')"
            ).lastrowid
            primary_id = database.execute(
                "INSERT INTO domains (name) VALUES ('second.example')"
            ).lastrowid
            database.execute(
                """
                UPDATE sites
                SET domain_id = ?, use_domain_root = 1
                WHERE id = ?
                """,
                (occupied_id, first_site_id),
            )
            database.commit()
        self.login_user(user_id)

        response = self.client.post(
            f"/sites/{second_site_id}/settings",
            data={
                "_csrf_token": self.csrf(),
                "name": "Second",
                "slug": "second",
                "folder": "second",
                "port": "43101",
                "domain_id": str(primary_id),
                "additional_domain_ids": str(occupied_id),
                "hosting_mode": "root",
            },
            follow_redirects=True,
        )

        self.assertIn(b"occupied.example already has a site at its root", response.data)
        with self.app.app_context():
            aliases = get_db().execute(
                "SELECT COUNT(*) AS count FROM site_domain_aliases WHERE site_id = ?",
                (second_site_id,),
            ).fetchone()["count"]
        self.assertEqual(aliases, 0)

    def test_domain_root_can_only_be_assigned_to_one_site(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "root-conflict-repo"
        first_root = repository_root / "first"
        second_root = repository_root / "second"
        first_root.mkdir(parents=True)
        second_root.mkdir()
        (first_root / "index.html").write_text("first", encoding="utf-8")
        (second_root / "index.html").write_text("second", encoding="utf-8")
        repository_id = self.add_repository(user_id, first_root)
        first_site_id = self.add_site(user_id, repository_id, first_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET slug = 'first' WHERE id = ?",
                (first_site_id,),
            )
            database.commit()
        second_site_id = self.add_site(
            user_id,
            repository_id,
            second_root,
            port=43101,
        )
        with self.app.app_context():
            database = get_db()
            domain_id = database.execute(
                "INSERT INTO domains (name) VALUES ('mhsit.club')"
            ).lastrowid
            database.commit()
        self.login_user(user_id)
        runtime = self.app.extensions["runtime_manager"]

        with patch.object(runtime, "sync_nginx_configs"):
            first = self.client.post(
                f"/sites/{first_site_id}/settings",
                data={
                    "_csrf_token": self.csrf(),
                    "name": "First",
                    "slug": "first",
                    "folder": "first",
                    "port": "43100",
                    "domain_id": str(domain_id),
                    "use_domain_root": "on",
                },
                follow_redirects=True,
            )
            second = self.client.post(
                f"/sites/{second_site_id}/settings",
                data={
                    "_csrf_token": self.csrf(),
                    "name": "Second",
                    "slug": "second",
                    "folder": "second",
                    "port": "43101",
                    "domain_id": str(domain_id),
                    "use_domain_root": "on",
                },
                follow_redirects=True,
            )

        self.assertIn(b"Site settings saved", first.data)
        self.assertIn(b"already has a site at its root", second.data)
        with self.app.app_context():
            root_sites = get_db().execute(
                """
                SELECT COUNT(*) AS count FROM sites
                WHERE domain_id = ? AND use_domain_root = 1
                """,
                (domain_id,),
            ).fetchone()["count"]
        self.assertEqual(root_sites, 1)

    def test_dashboard_hostname_cannot_be_used_as_site_root(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "dashboard-root-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("dashboard", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            domain_id = get_db().execute(
                "SELECT id FROM domains WHERE name = 'webmanager.example'"
            ).fetchone()["id"]
        self.login_user(user_id)

        response = self.client.post(
            f"/repositories/{repository_id}/deploy",
            data={
                "_csrf_token": self.csrf(),
                "multi_deploy": "1",
                "selected": "0",
                "folder_0": ".",
                "site_name_0": "Conflict",
                "domain_id": str(domain_id),
                "use_domain_root": "on",
            },
            follow_redirects=True,
        )

        self.assertIn(
            b"The WebManager dashboard hostname cannot also host a site",
            response.data,
        )
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) AS count FROM sites").fetchone()[
                    "count"
                ],
                0,
            )

    def test_dashboard_alias_cannot_be_used_as_site_subdomain(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "dashboard-alias-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("dashboard", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                INSERT INTO dashboard_domains (name)
                VALUES ('reserved.webmanager.example')
                """
            )
            database.commit()
            domain_id = database.execute(
                "SELECT id FROM domains WHERE name = 'webmanager.example'"
            ).fetchone()["id"]
        self.login_user(user_id)

        response = self.client.post(
            f"/repositories/{repository_id}/deploy",
            data={
                "_csrf_token": self.csrf(),
                "multi_deploy": "1",
                "selected": "0",
                "folder_0": "dashboard-alias-repo",
                "site_name_0": "Reserved",
                "domain_id": str(domain_id),
            },
            follow_redirects=True,
        )
        self.assertIn(
            b"The generated hostname is reserved for the WebManager dashboard",
            response.data,
        )

    def test_site_settings_can_change_folder_slug_port_and_fallback(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "settings-repo"
        original = repository_root / "public"
        replacement = repository_root / "dist"
        original.mkdir(parents=True)
        replacement.mkdir(parents=True)
        (original / "index.html").write_text("old", encoding="utf-8")
        (replacement / "index.htm").write_text("new", encoding="utf-8")
        repository_id = self.add_repository(user_id, original)
        site_id = self.add_site(user_id, repository_id, original)
        self.login_user(user_id)

        runtime = self.app.extensions["runtime_manager"]
        with patch.object(runtime, "sync_nginx_configs"):
            response = self.client.post(
                f"/sites/{site_id}/settings",
                data={
                    "_csrf_token": self.csrf(),
                    "name": "New Name",
                    "slug": "new-name",
                    "folder": "dist",
                    "port": "43101",
                },
                follow_redirects=True,
            )

        self.assertIn(b"Site settings saved", response.data)
        with self.app.app_context():
            site = get_db().execute(
                "SELECT * FROM sites WHERE id = ?", (site_id,)
            ).fetchone()
        self.assertEqual(site["name"], "New Name")
        self.assertEqual(site["slug"], "new-name")
        self.assertEqual(site["folder"], "dist")
        self.assertEqual(site["index_file"], "index.htm")
        self.assertEqual(site["port"], 43101)
        self.assertEqual(site["spa_fallback"], 0)
        self.assertIn("server_name new-name.webmanager.example", site["nginx_config"])

    def test_site_analytics_reads_structured_nginx_log(self):
        log_path = Path(self.app.config["NGINX_ROOT"]) / "access.log"
        log_path.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "time": "2026-06-11T12:00:00+00:00",
                            "host": "demo.webmanager.example",
                            "status": 200,
                            "bytes": 512,
                            "client": "203.0.113.5, 127.0.0.1",
                            "uri": "/docs?x=1",
                        }
                    ),
                    json.dumps(
                        {
                            "time": "2026-06-11T12:01:00+00:00",
                            "host": "other.webmanager.example",
                            "status": 200,
                            "bytes": 100,
                            "client": "203.0.113.6",
                            "uri": "/",
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )
        analytics = site_analytics(
            log_path,
            "demo.webmanager.example",
            days=365,
        )
        self.assertEqual(analytics["requests"], 1)
        self.assertEqual(analytics["visitors"], 1)
        self.assertEqual(analytics["bytes"], 512)
        self.assertEqual(analytics["top_paths"], [("/docs", 1)])

    def test_not_found_page_has_navigation_and_status_code(self):
        user_id = self.add_user("alice")
        self.login_user(user_id)
        response = self.client.get("/definitely-not-a-page")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"Page not found", response.data)
        self.assertIn(b"Return to dashboard", response.data)

    def test_site_subdomain_slugs_are_unique_across_users(self):
        alice_id = self.add_user("alice")
        bob_id = self.add_user("bob")
        repository_root = Path(self.temp_directory.name) / "shared-name-repos"
        alice_root = repository_root / "alice"
        bob_root = repository_root / "bob"
        alice_root.mkdir(parents=True)
        bob_root.mkdir(parents=True)
        (alice_root / "index.html").write_text("alice", encoding="utf-8")
        (bob_root / "index.html").write_text("bob", encoding="utf-8")
        alice_repository = self.add_repository(alice_id, alice_root)
        bob_repository = self.add_repository(bob_id, bob_root)

        with patch(
            "webmanager.services.RuntimeManager.start_site",
            return_value="builtin",
        ):
            self.login_user(alice_id)
            self.client.post(
                f"/repositories/{alice_repository}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "site_name": "Demo",
                    "folder": "alice",
                },
            )
            self.login_user(bob_id)
            self.client.post(
                f"/repositories/{bob_repository}/deploy",
                data={
                    "_csrf_token": self.csrf(),
                    "site_name": "Demo",
                    "folder": "bob",
                },
            )

        with self.app.app_context():
            slugs = [
                row["slug"]
                for row in get_db().execute(
                    "SELECT slug FROM sites ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(slugs, ["demo", "demo-2"])

    def test_builtin_runtime_serves_the_selected_index(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "served-repo"
        repository_root.mkdir()
        (repository_root / "Index.HTML").write_text("<h1>Runtime works</h1>", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        config = build_site_config("Runtime", repository_root, "Index.HTML", port, True)
        with self.app.app_context():
            database = get_db()
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, name, slug, folder, document_root,
                    index_file, port, spa_fallback, nginx_config
                ) VALUES (?, ?, 'Runtime', 'runtime', '.', ?, 'Index.HTML', ?, 1, ?)
                """,
                (user_id, repository_id, str(repository_root), port, config),
            )
            database.commit()
            site_id = cursor.lastrowid
            runtime = self.app.extensions["runtime_manager"]
            runtime.start_site(site_id)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    self.assertIn(b"Runtime works", response.read())
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/client/route", timeout=5) as response:
                    self.assertIn(b"Runtime works", response.read())
            finally:
                runtime.stop_site(site_id)

    def test_management_pages_render_for_owned_site(self):
        user_id = self.add_user("alice", email="alice@example.com")
        repository_root = Path(self.temp_directory.name) / "render-repo"
        site_root = repository_root / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        site_id = self.add_site(user_id, repository_id, site_root)
        self.login_user(user_id)

        pages = {
            f"/repositories/{repository_id}/select": b"Select a site folder",
            f"/sites/{site_id}": b"Hosting details",
            f"/sites/{site_id}/config": b"Edit Nginx config",
        }
        for path, expected in pages.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.data)

    def test_repository_auto_refresh_schedule_can_be_set_and_disabled(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "scheduled-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        self.login_user(user_id)

        response = self.client.post(
            f"/repositories/{repository_id}/schedule",
            data={
                "_csrf_token": self.csrf(),
                "auto_refresh_minutes": "45",
            },
            follow_redirects=True,
        )
        self.assertIn(b"every 45 minutes", response.data)
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertEqual(repository["auto_refresh_minutes"], 45)
            self.assertIsNotNone(repository["next_refresh_at"])

        response = self.client.post(
            f"/repositories/{repository_id}/schedule",
            data={
                "_csrf_token": self.csrf(),
                "auto_refresh_minutes": "",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Scheduled update checks disabled", response.data)
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertIsNone(repository["auto_refresh_minutes"])
            self.assertIsNone(repository["next_refresh_at"])

    def test_repository_auto_refresh_schedule_validates_interval(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "invalid-schedule-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        self.login_user(user_id)

        response = self.client.post(
            f"/repositories/{repository_id}/schedule",
            data={
                "_csrf_token": self.csrf(),
                "auto_refresh_minutes": str(MAX_REFRESH_MINUTES + 1),
            },
            follow_redirects=True,
        )
        self.assertIn(b"must be between", response.data)
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertIsNone(repository["auto_refresh_minutes"])

    def test_scheduled_refresh_rejects_update_that_breaks_deployed_site(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "protected-refresh-repo"
        site_root = repository_root / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("live", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        self.add_site(user_id, repository_id, site_root)
        manager = self.app.extensions["repository_refresh_manager"]

        def invalid_clone(_url, _target, _branch, validate_staging):
            staging = Path(self.temp_directory.name) / "invalid-staging"
            staging.mkdir()
            validate_staging(staging)

        with patch(
            "webmanager.repository_refresh.clone_repository",
            side_effect=invalid_clone,
        ):
            result = manager.refresh(repository_id)

        self.assertEqual(result.status, "error")
        self.assertIn("deployed folder", result.message)
        self.assertEqual((site_root / "index.html").read_text(encoding="utf-8"), "live")
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertIn("deployed folder", repository["error"])

    def test_due_repository_refreshes_are_dispatched(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "due-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                UPDATE repositories
                SET auto_refresh_minutes = 15,
                    next_refresh_at = '2000-01-01 00:00:00'
                WHERE id = ?
                """,
                (repository_id,),
            )
            database.commit()

        manager = self.app.extensions["repository_refresh_manager"]
        with patch.object(manager, "refresh") as refresh:
            count = manager.run_due_once()

        self.assertEqual(count, 1)
        refresh.assert_called_once_with(repository_id, wait=False)

    def test_validated_update_waits_for_owner_approval(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "successful-schedule-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("old", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                UPDATE repositories
                SET local_path = ?, auto_refresh_minutes = 20,
                    next_refresh_at = '2000-01-01 00:00:00',
                    error = 'old failure', current_commit = ?
                WHERE id = ?
                """,
                (str(repository_root), "a" * 40, repository_id),
            )
            database.commit()

        def staged_clone(_url, target, _branch, validate_staging):
            target.mkdir(parents=True)
            (target / "index.html").write_text("new", encoding="utf-8")
            validate_staging(target)

        manager = self.app.extensions["repository_refresh_manager"]
        with (
            patch(
                "webmanager.repository_refresh.clone_repository",
                side_effect=staged_clone,
            ),
            patch(
                "webmanager.repository_refresh.repository_commit",
                return_value="b" * 40,
            ),
        ):
            result = manager.refresh(repository_id)

        self.assertEqual(result.status, "available")
        self.assertEqual(
            (repository_root / "index.html").read_text(encoding="utf-8"),
            "old",
        )
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertEqual(repository["pending_commit"], "b" * 40)
            self.assertIsNone(repository["last_refreshed_at"])
            self.assertIsNotNone(repository["next_refresh_at"])
            self.assertIsNone(repository["error"])

        with patch("webmanager.repository_refresh.clone_repository") as clone:
            result = manager.refresh(repository_id)
        self.assertEqual(result.status, "available")
        clone.assert_not_called()

        with patch(
            "webmanager.repository_refresh.repository_commit",
            return_value="b" * 40,
        ):
            result = manager.apply_pending(repository_id)

        self.assertEqual(result.status, "applied")
        self.assertEqual(
            (repository_root / "index.html").read_text(encoding="utf-8"),
            "new",
        )
        with self.app.app_context():
            repository = get_db().execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertEqual(repository["current_commit"], "b" * 40)
            self.assertIsNone(repository["pending_commit"])
            self.assertIsNotNone(repository["last_refreshed_at"])

    def test_automatic_update_mode_applies_validated_update(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "automatic-update-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("old", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                UPDATE repositories
                SET local_path = ?, update_mode = 'auto', current_commit = ?
                WHERE id = ?
                """,
                (str(repository_root), "a" * 40, repository_id),
            )
            database.commit()

        def staged_clone(_url, target, _branch, validate_staging):
            target.mkdir(parents=True)
            (target / "index.html").write_text("new", encoding="utf-8")
            validate_staging(target)

        manager = self.app.extensions["repository_refresh_manager"]
        with (
            patch(
                "webmanager.repository_refresh.clone_repository",
                side_effect=staged_clone,
            ),
            patch(
                "webmanager.repository_refresh.repository_commit",
                return_value="b" * 40,
            ),
        ):
            result = manager.refresh(repository_id)

        self.assertEqual(result.status, "applied")
        self.assertEqual(
            (repository_root / "index.html").read_text(encoding="utf-8"),
            "new",
        )

    def test_applied_repository_update_restarts_running_sites(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "restart-update-repo"
        site_root = repository_root / "public"
        site_root.mkdir(parents=True)
        (site_root / "index.html").write_text("old", encoding="utf-8")
        repository_id = self.add_repository(user_id, site_root)
        site_id = self.add_site(
            user_id,
            repository_id,
            site_root,
            status="running",
            runtime_backend="nginx",
        )
        pending = repository_root.parent / f".{repository_root.name}.pending"
        (pending / "public").mkdir(parents=True)
        (pending / "public" / "index.html").write_text("new", encoding="utf-8")
        with self.app.app_context():
            database = get_db()
            database.execute(
                """
                UPDATE repositories
                SET local_path = ?, pending_path = ?, pending_commit = ?
                WHERE id = ?
                """,
                (
                    str(repository_root),
                    str(pending),
                    "b" * 40,
                    repository_id,
                ),
            )
            database.commit()

        manager = self.app.extensions["repository_refresh_manager"]
        runtime = self.app.extensions["runtime_manager"]
        with (
            patch(
                "webmanager.repository_refresh.repository_commit",
                return_value="b" * 40,
            ),
            patch.object(runtime, "restart_site", return_value="nginx") as restart,
        ):
            result = manager.apply_pending(repository_id)

        self.assertEqual(result.status, "applied")
        restart.assert_called_once_with(site_id)

    def test_due_refresh_skips_repository_already_being_updated(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "busy-refresh-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        manager = self.app.extensions["repository_refresh_manager"]

        with manager.repository_lock(repository_id):
            result = manager.refresh(repository_id, wait=False)

        self.assertEqual(result.status, "busy")

    def test_config_editor_rejects_removing_assigned_port(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        config = build_site_config("Demo", repository_root, "index.html", 43100, True)

        with self.app.app_context():
            database = get_db()
            cursor = database.execute(
                """
                INSERT INTO sites (
                    user_id, repository_id, name, slug, folder, document_root,
                    index_file, port, spa_fallback, nginx_config
                ) VALUES (?, ?, 'Demo', 'demo', '.', ?, 'index.html', 43100, 1, ?)
                """,
                (user_id, repository_id, str(repository_root), config),
            )
            database.commit()
            site_id = cursor.lastrowid

        self.login_user(user_id)
        response = self.client.post(
            f"/sites/{site_id}/config",
            data={"_csrf_token": self.csrf(), "nginx_config": "server { listen 9999; }"},
            follow_redirects=True,
        )
        self.assertIn(b"assigned document root", response.data)

        with self.app.app_context():
            saved = get_db().execute("SELECT nginx_config FROM sites WHERE id = ?", (site_id,)).fetchone()
            self.assertEqual(saved["nginx_config"], config)

    def test_site_delete_keeps_record_when_runtime_cannot_stop(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "delete-site-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(user_id, repository_id, repository_root)
        self.login_user(user_id)

        runtime = self.app.extensions["runtime_manager"]
        with patch.object(
            runtime,
            "stop_site",
            side_effect=RuntimeErrorDetail("reload failed"),
        ):
            response = self.client.post(
                f"/sites/{site_id}/delete",
                data={"_csrf_token": self.csrf()},
                follow_redirects=True,
            )

        self.assertIn(b"was not deleted", response.data)
        with self.app.app_context():
            saved = get_db().execute("SELECT id FROM sites WHERE id = ?", (site_id,)).fetchone()
            self.assertIsNotNone(saved)

    def test_repository_delete_does_not_remove_unmanaged_path(self):
        user_id = self.add_user("alice")
        external = Path(self.temp_directory.name) / "external-content"
        external.mkdir()
        (external / "index.html").write_text("keep me", encoding="utf-8")
        with self.app.app_context():
            database = get_db()
            cursor = database.execute(
                """
                INSERT INTO repositories (user_id, name, url, local_path, status)
                VALUES (?, 'external', 'https://example.com/external.git', ?, 'ready')
                """,
                (user_id, str(external)),
            )
            database.commit()
            repository_id = cursor.lastrowid
        self.login_user(user_id)

        response = self.client.post(
            f"/repositories/{repository_id}/delete",
            data={"_csrf_token": self.csrf()},
            follow_redirects=True,
        )

        self.assertIn(b"unsafe stored path was not removed", response.data)
        self.assertTrue((external / "index.html").exists())
        with self.app.app_context():
            saved = get_db().execute(
                "SELECT id FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            self.assertIsNone(saved)

    def test_builtin_launch_failure_is_recorded(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "launch-failure-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(user_id, repository_id, repository_root)

        with self.app.app_context(), patch(
            "webmanager.services.subprocess.Popen",
            side_effect=OSError("permission denied"),
        ):
            with self.assertRaises(RuntimeErrorDetail):
                self.app.extensions["runtime_manager"].start_site(site_id)
            site = get_db().execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

        self.assertEqual(site["status"], "error")
        self.assertIn("permission denied", site["last_error"])

    def test_builtin_runtime_waits_for_listener_readiness(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "slow-runtime-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(user_id, repository_id, repository_root)

        process = SimpleNamespace(
            pid=43210,
            poll=lambda: None,
            terminate=lambda: None,
        )
        connection_attempts = []

        def delayed_connection(*_args, **_kwargs):
            connection_attempts.append(1)
            if len(connection_attempts) < 3:
                raise ConnectionRefusedError
            return socket.socket()

        with (
            self.app.app_context(),
            patch("webmanager.services.subprocess.Popen", return_value=process),
            patch(
                "webmanager.services.socket.create_connection",
                side_effect=delayed_connection,
            ),
            patch("webmanager.services.time.sleep"),
        ):
            backend = self.app.extensions["runtime_manager"].start_site(site_id)

        self.assertEqual(backend, "builtin")
        self.assertEqual(len(connection_attempts), 3)

    def test_nginx_stop_rolls_back_state_when_reload_fails(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "nginx-stop-repo"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(
            user_id,
            repository_id,
            repository_root,
            status="running",
            runtime_backend="nginx",
        )
        fake_nginx = Path(self.temp_directory.name) / "fake-nginx"
        fake_nginx.write_text("", encoding="utf-8")
        self.app.config["NGINX_BINARY"] = str(fake_nginx)
        runtime = self.app.extensions["runtime_manager"]

        with self.app.app_context(), patch.object(
            runtime,
            "_restart_nginx",
            side_effect=RuntimeErrorDetail("reload failed"),
        ):
            with self.assertRaises(RuntimeErrorDetail):
                runtime.stop_site(site_id)
            site = get_db().execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

        self.assertEqual(site["status"], "running")
        self.assertEqual(site["runtime_backend"], "nginx")

    def test_nginx_stop_removes_route_and_restarts_gateway(self):
        user_id = self.add_user("alice")
        repository_root = Path(self.temp_directory.name) / "nginx-stop-success"
        repository_root.mkdir()
        (repository_root / "index.html").write_text("hello", encoding="utf-8")
        repository_id = self.add_repository(user_id, repository_root)
        site_id = self.add_site(
            user_id,
            repository_id,
            repository_root,
            status="running",
            runtime_backend="nginx",
        )
        fake_nginx = Path(self.temp_directory.name) / "fake-nginx-success"
        fake_nginx.write_text("", encoding="utf-8")
        self.app.config["NGINX_BINARY"] = str(fake_nginx)
        runtime = self.app.extensions["runtime_manager"]

        with self.app.app_context():
            runtime.sync_nginx_configs()
            config_files = list(
                (Path(self.app.config["NGINX_ROOT"]) / "conf.d").glob("*.conf")
            )
            self.assertEqual(len(config_files), 1)
            with patch.object(runtime, "_restart_nginx") as restart:
                runtime.stop_site(site_id)
            site = get_db().execute(
                "SELECT * FROM sites WHERE id = ?",
                (site_id,),
            ).fetchone()

        restart.assert_called_once_with()
        self.assertEqual(site["status"], "stopped")
        self.assertFalse(config_files[0].exists())


class ServiceUnitTests(unittest.TestCase):
    def test_site_config_routing_migration_is_stable_and_loopback_only(self):
        root = Path.cwd()
        original = build_site_config(
            "Demo",
            root,
            "index.html",
            8123,
            True,
        )
        routed = route_site_config(
            original,
            8123,
            "demo.webmanager.example",
            8090,
        )

        self.assertEqual(
            routed,
            route_site_config(
                routed,
                8123,
                "demo.webmanager.example",
                8090,
            ),
        )
        self.assertIn("listen 127.0.0.1:8123;", routed)
        self.assertIn("listen 127.0.0.1:8090;", routed)
        self.assertNotIn("listen [::]:8123;", routed)
        validate_site_config(
            routed,
            root,
            8123,
            "demo.webmanager.example",
            8090,
        )

    def test_site_config_supports_multiple_server_names(self):
        root = Path.cwd()
        hostnames = (
            "demo.primary.example",
            "demo.alias.example",
        )
        config = build_site_config(
            "Demo",
            root,
            "index.html",
            8123,
            True,
            hostnames,
            8090,
        )

        self.assertIn(
            "server_name demo.primary.example demo.alias.example;",
            config,
        )
        validate_site_config(config, root, 8123, hostnames, 8090)

    def test_runtime_requirements_include_authlib_requests_integration(self):
        root = Path(__file__).resolve().parent.parent
        requirements = (root / "requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertRegex(requirements, r"(?m)^Authlib[<>=]")
        self.assertRegex(requirements, r"(?m)^requests[<>=]")

    def test_debian_self_updater_has_required_safety_controls(self):
        root = Path(__file__).resolve().parent.parent
        updater = (root / "deploy" / "debian" / "update.sh").read_text(
            encoding="utf-8"
        )
        timer = (root / "deploy" / "debian" / "webmanager-update.timer").read_text(
            encoding="utf-8"
        )
        path_unit = (
            root / "deploy" / "debian" / "webmanager-update.path"
        ).read_text(encoding="utf-8")
        service = (
            root / "deploy" / "debian" / "webmanager-update.service"
        ).read_text(encoding="utf-8")
        installer = (root / "deploy" / "debian" / "install.sh").read_text(
            encoding="utf-8"
        )
        uninstaller = (
            root / "deploy" / "debian" / "uninstall.sh"
        ).read_text(encoding="utf-8")
        setup = (root / "setup.sh").read_text(encoding="utf-8")

        self.assertIn("https://github", updater)
        self.assertIn("merge-base --is-ancestor", updater)
        self.assertIn('"$python" -m unittest discover', updater)
        self.assertIn("Reusing installed Python dependencies", updater)
        self.assertIn(
            "Tests failed with installed dependencies; retrying in a clean environment.",
            updater,
        )
        self.assertIn("Last test output:", updater)
        self.assertIn("UPDATE_STAGE=installing_dependencies", updater)
        self.assertIn("UPDATE_STAGE=preflighting_install", updater)
        self.assertIn("source.backup(target)", updater)
        self.assertIn('"runtime_manager"].sync_nginx_configs()', updater)
        self.assertIn('app.test_client().get("/healthz")', updater)
        self.assertIn(
            "The running installation was not stopped.",
            updater,
        )
        self.assertLess(
            updater.index("UPDATE_STAGE=preflighting_install"),
            updater.rindex('systemctl stop webmanager\n'),
        )
        self.assertIn("--retries 5", updater)
        self.assertIn("REUSE_VENV=0", installer)
        self.assertIn("Reusing the installed Python environment", installer)
        self.assertIn(
            "Building a replacement Python environment before changing the active one.",
            installer,
        )
        self.assertIn('mktemp -d "$APP_DIR/.venv.build.XXXXXX"', installer)
        self.assertIn('mv "$APP_DIR/.venv" "$OLD_VENV"', installer)
        self.assertIn('mv "$NEW_VENV" "$APP_DIR/.venv"', installer)
        self.assertIn("The replacement Python environment failed validation.", installer)
        self.assertIn("The installed Python environment failed validation.", installer)
        self.assertIn(
            "Restored the previous Python environment after installation failure.",
            installer,
        )
        self.assertIn("set +e", installer)
        self.assertIn(
            "if [[ $SELF_UPDATE -eq 0 && $restored -eq 1 ]]; then",
            installer,
        )
        self.assertIn("VENV_SWAPPED=1", installer)
        self.assertIn("INSTALL_SUCCEEDED=1", installer)
        self.assertGreater(
            installer.index('printf \'%s\\n\' "$SOURCE_COMMIT"'),
            installer.index("if [[ $READY -ne 1 ]]"),
        )
        self.assertGreater(
            installer.index('rm -rf "$OLD_VENV"'),
            installer.index("if [[ $READY -ne 1 ]]"),
        )
        self.assertLess(
            installer.index("INSTALL_SUCCEEDED=1"),
            installer.index('rm -rf "$OLD_VENV"'),
        )
        self.assertIn("rollback()", updater)
        self.assertIn("wait_for_webmanager()", updater)
        self.assertIn("systemctl reset-failed webmanager", updater)
        self.assertIn(
            "Rollback restored the files, but WebManager did not become healthy.",
            updater,
        )
        self.assertIn("flock -n", updater)
        self.assertIn("APPROVED_COMMIT", updater)
        self.assertIn("sync_data_backup()", updater)
        self.assertIn("Preparing the data backup while WebManager remains online.", updater)
        self.assertIn('sync_data_backup "$DATA_BACKUP_DIR"', updater)
        self.assertIn('cp -a "$DATA_BACKUP_DIR" "$DATA_DIR"', updater)
        self.assertNotIn("webmanager-data.tar.gz", updater)
        self.assertIn("waiting for super-admin approval", updater)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("PathChanged=", path_unit)
        self.assertIn("requests/check", path_unit)
        self.assertIn("ProtectSystem=full", service)
        self.assertIn("ReadWritePaths=/opt/webmanager", service)
        self.assertIn(
            "ReadWritePaths=/etc/nginx/sites-available",
            service,
        )
        self.assertIn(
            "ReadWritePaths=/usr/local/sbin",
            service,
        )
        self.assertIn("--self-update", installer)
        self.assertIn(
            "https://github.com/coolguy1333/WebManager.git", installer
        )
        self.assertIn("Keeping existing $UPDATER_ENV", installer)
        self.assertIn("webmanager-update.timer", installer)
        self.assertIn("webmanager-update.path", installer)
        self.assertIn("site gateway the explicit", installer)
        self.assertIn("server_name $DASHBOARD_HOST", installer)
        self.assertIn("listen 8080 default_server;", installer)
        self.assertIn("listen [::]:8080 default_server;", installer)
        self.assertIn(r"proxy_set_header Host \$host;", installer)
        self.assertIn("webmanager-uninstall", installer)
        self.assertNotIn(
            'set_env_value WEBMANAGER_SITE_PUBLIC_SCHEME "https"',
            installer,
        )
        self.assertIn("WEBMANAGER_GOOGLE_CLIENT_SECRET", setup)
        self.assertIn('bash "$SCRIPT_DIR/configure-google.sh"', setup)
        self.assertIn("Initial deployment domain:", setup)
        self.assertIn("Default deployment domain added:", setup)
        self.assertIn("WEBMANAGER_INITIAL_SITE_BASE_DOMAIN", setup)
        self.assertIn("INSERT INTO domains (name, is_default)", setup)
        self.assertLess(
            setup.index("Initial deployment domain:"),
            setup.index('bash "$SCRIPT_DIR/deploy/debian/install.sh" "$@"'),
        )
        self.assertNotIn("Configure Google sign-in now?", setup)
        self.assertNotIn(
            'set_env_value WEBMANAGER_SITE_BASE_DOMAIN "$SITE_BASE_DOMAIN"',
            installer,
        )
        self.assertIn("WEBMANAGER_INITIAL_SITE_BASE_DOMAIN", installer)

        google_setup = (root / "configure-google.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            r"^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$",
            google_setup,
        )
        self.assertIn("Client secret is required. Try again.", google_setup)
        self.assertIn("Allow unrestricted Google sign-in? [y/N]:", google_setup)
        self.assertIn("wildcard TLS coverage", google_setup)
        self.assertIn("Hosted site base domain", google_setup)
        self.assertIn("CONFIGURED_SITE_BASE_DOMAIN", google_setup)
        self.assertIn("server_name $PUBLIC_HOST", google_setup)
        self.assertIn("configured site hostnames at the loopback gateway", google_setup)
        self.assertIn(
            'set_env WEBMANAGER_SITE_PUBLIC_SCHEME "$SITE_PUBLIC_SCHEME"',
            google_setup,
        )
        self.assertIn("Cloudflare Tunnel", google_setup)
        self.assertIn("systemctl stop webmanager-update.service", uninstaller)
        self.assertIn("Configuration remains in $CONFIG_DIR", uninstaller)
        self.assertIn('rm -rf "$CONFIG_DIR"', uninstaller)

    def test_legacy_user_schema_migrates_without_losing_users(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "legacy.sqlite3"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO users (username, password_hash)
                VALUES ('legacy-user', 'old-password-hash');
                """
            )
            database.commit()
            database.close()

            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test",
                    "DATABASE": str(database_path),
                    "REPOSITORY_ROOT": str(root / "repositories"),
                    "NGINX_ROOT": str(root / "nginx"),
                    "LOG_ROOT": str(root / "logs"),
                }
            )
            with app.app_context():
                columns = {
                    row["name"]
                    for row in get_db().execute("PRAGMA table_info(users)").fetchall()
                }
                user = get_db().execute(
                    "SELECT * FROM users WHERE username = 'legacy-user'"
                ).fetchone()
            self.assertIn("google_sub", columns)
            self.assertIn("email", columns)
            self.assertIn("is_admin", columns)
            self.assertIn("is_active", columns)
            self.assertEqual(user["password_hash"], "old-password-hash")
            self.assertEqual(user["is_admin"], 1)

    def test_legacy_repository_schema_gains_refresh_schedule_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "legacy-repositories.sqlite3"
            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    branch TEXT,
                    local_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            database.commit()
            database.close()

            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test",
                    "DATABASE": str(database_path),
                    "REPOSITORY_ROOT": str(root / "repositories"),
                    "NGINX_ROOT": str(root / "nginx"),
                    "LOG_ROOT": str(root / "logs"),
                }
            )
            with app.app_context():
                columns = {
                    row["name"]
                    for row in get_db().execute(
                        "PRAGMA table_info(repositories)"
                    ).fetchall()
                }

            self.assertIn("auto_refresh_minutes", columns)
            self.assertIn("next_refresh_at", columns)
            self.assertIn("last_refreshed_at", columns)
            self.assertIn("update_mode", columns)
            self.assertIn("current_commit", columns)
            self.assertIn("pending_path", columns)
            self.assertIn("pending_commit", columns)

    def test_external_data_directory_becomes_flask_instance_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"WEBMANAGER_DATA_DIR": directory}):
                app = create_app({"TESTING": True, "SECRET_KEY": "test"})
            self.assertEqual(Path(app.instance_path), Path(directory).resolve())
            self.assertEqual(Path(app.config["DATABASE"]), Path(directory) / "webmanager.sqlite3")

    def test_index_discovery_and_safe_folder_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("root", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "index.htm").write_text("docs", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "index.html").write_text("ignored", encoding="utf-8")

            candidates = find_index_folders(root)
            self.assertEqual([item["folder"] for item in candidates], [".", "docs"])
            self.assertEqual(resolve_folder(root, "docs"), (root / "docs").resolve())
            with self.assertRaises(GitError):
                resolve_folder(root, "../outside")

    def test_repository_url_validation_rejects_local_paths(self):
        self.assertEqual(
            validate_repo_url("https://github.com/example/site.git"),
            "https://github.com/example/site.git",
        )
        self.assertEqual(
            validate_repo_url("git@github.com:example/site.git"),
            "git@github.com:example/site.git",
        )
        with self.assertRaises(GitError):
            validate_repo_url("C:/private/repository")
        with self.assertRaises(GitError):
            validate_repo_url("file:///private/repository")

    def test_failed_repository_refresh_preserves_existing_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "site"
            target.mkdir()
            (target / "index.html").write_text("working", encoding="utf-8")
            failure = SimpleNamespace(returncode=1, stderr="network failed", stdout="")

            with patch("webmanager.git_service.subprocess.run", return_value=failure):
                with self.assertRaises(GitError):
                    clone_repository("https://example.com/site.git", target)

            self.assertEqual(
                (target / "index.html").read_text(encoding="utf-8"),
                "working",
            )
            self.assertEqual(list(root.glob(".site.*")), [])

    def test_successful_repository_refresh_replaces_clone_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "site"
            target.mkdir()
            (target / "index.html").write_text("old", encoding="utf-8")

            def fake_clone(command, **_kwargs):
                staging = Path(command[-1])
                staging.mkdir()
                (staging / "index.html").write_text("new", encoding="utf-8")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch("webmanager.git_service.subprocess.run", side_effect=fake_clone):
                clone_repository("https://example.com/site.git", target)

            self.assertEqual((target / "index.html").read_text(encoding="utf-8"), "new")
            self.assertEqual(list(root.glob(".site.*")), [])

    def test_port_allocator_avoids_database_and_os_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test",
                    "DATABASE": str(Path(directory) / "db.sqlite3"),
                    "REPOSITORY_ROOT": str(Path(directory) / "repositories"),
                    "NGINX_ROOT": str(Path(directory) / "nginx"),
                    "LOG_ROOT": str(Path(directory) / "logs"),
                }
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            occupied_port = listener.getsockname()[1]
            try:
                with app.app_context():
                    port = allocate_port(get_db(), occupied_port, occupied_port + 5)
                    self.assertNotEqual(port, occupied_port)
            finally:
                listener.close()

    def test_managed_nginx_config_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = build_site_config("Demo", root, "index.html", 8123, True)
            validate_site_config(config, root, 8123)
            self.assertIn("disable_symlinks on;", config)

            with self.assertRaises(NginxConfigError):
                validate_site_config(config.replace(root.resolve().as_posix(), "/etc"), root, 8123)
            with self.assertRaises(NginxConfigError):
                validate_site_config(config.replace("server {", "include /etc/nginx/nginx.conf;\nserver {"), root, 8123)
            with self.assertRaises(NginxConfigError):
                validate_site_config(config.replace("listen 8123;", "listen 9999;"), root, 8123)
            nested_guard = config.replace(
                "    disable_symlinks on;\n",
                "",
            ).replace(
                "    location / {\n",
                "    location / {\n        disable_symlinks on;\n",
            )
            with self.assertRaises(NginxConfigError):
                validate_site_config(nested_guard, root, 8123)

            main_config = build_main_config(root, root / "conf.d")
            self.assertIn(f'{root.as_posix()}/temp/client_body', main_config)

            gateway_config = build_main_config(
                root,
                root / "conf.d",
                8090,
                ("web.example", "control.example"),
                5000,
            )
            self.assertIn(
                "server_name web.example control.example;",
                gateway_config,
            )
            self.assertIn(
                "proxy_pass http://127.0.0.1:5000;",
                gateway_config,
            )
            self.assertIn(
                "listen 127.0.0.1:8090 default_server;",
                gateway_config,
            )


if __name__ == "__main__":
    unittest.main()
