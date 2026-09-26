import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


from . import apps as app_support
from .db import get_db
from .domains import site_hostname, site_hostnames
from .nginx import (
    NginxConfigError,
    build_app_config,
    build_main_config,
    build_paused_site_config,
    route_site_config,
    upgrade_legacy_site_config,
    validate_site_config,
)


class RuntimeErrorDetail(RuntimeError):
    pass


def port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def allocate_port(database, minimum: int, maximum: int, requested: int | None = None) -> int:
    used = {row["port"] for row in database.execute("SELECT port FROM sites").fetchall()}
    if requested is not None:
        if requested < minimum or requested > maximum:
            raise RuntimeErrorDetail(f"Port must be between {minimum} and {maximum}.")
        if requested in used or not port_is_available(requested):
            raise RuntimeErrorDetail(f"Port {requested} is already in use.")
        return requested

    for port in range(minimum, maximum + 1):
        if port not in used and port_is_available(port):
            return port
    raise RuntimeErrorDetail("No available site ports remain in the configured range.")


class RuntimeManager:
    def __init__(self, app):
        self.app = app
        self.processes: dict[int, subprocess.Popen] = {}
        self._app_locks: dict[int, threading.Lock] = {}
        self._app_locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # App hosting (containers)
    # ------------------------------------------------------------------
    @property
    def container_runtime(self):
        if not self.app.config.get("APPS_ENABLED"):
            return None
        binary = app_support.ContainerRuntime.detect(self.app.config.get("CONTAINER_RUNTIME", ""))
        if not binary:
            return None
        return app_support.ContainerRuntime(
            binary,
            Path(self.app.instance_path) / "app-work",
            self.app.logger,
        )

    def apps_status(self):
        """(enabled, message) describing whether apps can run here."""
        if not self.app.config.get("APPS_ENABLED"):
            return False, "App hosting is turned off (set WEBMANAGER_APPS_ENABLED=1)."
        runtime = self.container_runtime
        if runtime is None:
            return False, "No container runtime found. Install Docker or Podman."
        if not self.nginx_binary:
            return False, "App hosting needs Nginx."
        ok, detail = runtime.available()
        if not ok:
            return False, f"The container runtime isn't reachable: {detail}"
        return True, f"{Path(runtime.binary).name} {detail}"

    def _app_lock(self, site_id):
        with self._app_locks_guard:
            return self._app_locks.setdefault(site_id, threading.Lock())

    def start_app_async(self, site_id: int, force: bool = False) -> bool:
        """Start/rebuild an app in the background. Returns False if busy."""
        lock = self._app_lock(site_id)
        if not lock.acquire(blocking=False):
            return False
        with self.app.app_context():
            database = get_db()
            database.execute(
                "UPDATE sites SET status = 'starting', last_error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (site_id,),
            )
            database.commit()

        def worker():
            try:
                with self.app.app_context():
                    try:
                        self._start_app(site_id, force=force)
                    except RuntimeErrorDetail as exc:
                        self.app.logger.warning("App %s failed to start: %s", site_id, exc)
            finally:
                lock.release()

        threading.Thread(target=worker, name=f"webmanager-app-{site_id}", daemon=True).start()
        return True

    def _app_env(self, site, manifest):
        database = get_db()
        stored = app_support.decrypt_env(site["app_env"], self.app.config["SECRET_KEY"])
        hostname = site_hostname(database, site)
        public_url = f"{self.app.config['SITE_PUBLIC_SCHEME']}://{hostname}" if hostname else ""
        return app_support.effective_env(
            manifest, stored, app_id=site["id"], public_url=public_url
        )

    def _start_app(self, site_id: int, force: bool = False):
        database = get_db()
        site = database.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if site is None:
            raise RuntimeErrorDetail("Site not found.")

        def fail(message):
            self._set_site_state(database, site_id, "error", "container", None, message)
            try:
                self._write_all_nginx_configs(database)
                if self.nginx_binary:
                    self._reload_nginx()
            except RuntimeErrorDetail:
                pass
            raise RuntimeErrorDetail(message)

        runtime = self.container_runtime
        if runtime is None or not self.nginx_binary:
            fail(self.apps_status()[1])
        if not site_hostnames(database, site):
            fail("Apps need a public domain. Choose one in Settings.")
        try:
            manifest = app_support.load_manifest(
                Path(site["document_root"]), self.app.config["APP_DEFAULT_MEMORY_MB"]
            )
            env, missing = self._app_env(site, manifest)
        except app_support.AppError as exc:
            fail(str(exc))
        if missing:
            fail(f"Set these required variables first: {', '.join(missing)}.")

        commit = database.execute(
            "SELECT current_commit FROM repositories WHERE id = ?", (site["repository_id"],)
        ).fetchone()["current_commit"]
        memory = site["app_memory_mb"] or manifest["memory_mb"]
        cpus = self.app.config["APP_CPUS"]
        image = runtime.image_name(site_id, (commit or "manual")[:12])
        name = runtime.container_name(site_id)
        previous_name = runtime.container_name(site_id, "-previous")
        log_path = Path(self.app.config["LOG_ROOT"]) / f"site-{site_id}.log"
        rolled_back = False
        restore_error = None
        try:
            if not runtime.image_exists(image):
                runtime.build(site_id, Path(site["document_root"]), (commit or "manual")[:12], log_path)
            digest = app_support.config_hash(image, env, memory, cpus, site["port"])
            current = runtime.inspect(name)
            already_running = (
                current is not None
                and current["State"]["Status"] == "running"
                and current["Config"]["Labels"].get("webmanager.config") == digest
            )
            if force or not already_running:
                runtime.remove(previous_name)
                previous = None
                if current is not None:
                    runtime.backup_data(
                        name, Path(self.app.instance_path) / "app-backups" / str(site_id)
                    )
                    runtime.stop(name)
                    runtime.rename(name, previous_name)
                    previous = previous_name
                runtime.run(
                    site_id=site_id,
                    image=image,
                    name=name,
                    host_port=site["port"],
                    env=env,
                    memory_mb=memory,
                    cpus=cpus,
                    config_hash=digest,
                )
                healthy, message = app_support.wait_until_healthy(
                    site["port"],
                    manifest["health"],
                    timeout=self.app.config["APP_START_TIMEOUT"],
                    is_running=lambda: runtime.status(name) == "running",
                )
                if not healthy:
                    logs = runtime.logs(name, 30)
                    runtime.remove(name)
                    detail = f"The new version didn't pass its health check: {message}.\n{logs[-1500:]}"
                    if not previous:
                        raise app_support.AppError(detail)
                    runtime.rename(previous, name)
                    runtime.start(name)
                    rolled_back = True
                    restore_error = detail + "\nThe previous version was restored and is still running."
                elif previous:
                    runtime.remove(previous)
                if not rolled_back:
                    runtime.remove_images(site_id, keep=image)
        except app_support.AppError as exc:
            fail(str(exc))

        try:
            self._write_all_nginx_configs(database, activating_site_id=site_id)
            self._reload_nginx()
        except RuntimeErrorDetail as exc:
            fail(str(exc))
        # "nginx -s reload" returns before the new workers take over; give
        # them a moment so "Running" is only shown once the app is reachable.
        time.sleep(0.5)
        # After a rollback the old version is serving again, so the site is
        # running, but the failed update is kept as last_error for the owner.
        self._set_site_state(database, site_id, "running", "container", None, restore_error)
        if rolled_back:
            raise RuntimeErrorDetail(restore_error)
        return "container"

    def _stop_app(self, site):
        database = get_db()
        runtime = self.container_runtime
        lock = self._app_lock(site["id"])
        # Don't hang the request behind a long build; ask to retry instead.
        if not lock.acquire(timeout=5):
            raise RuntimeErrorDetail(
                "The app is still building or starting. Try again when that finishes."
            )
        try:
            if runtime is not None:
                try:
                    runtime.stop(runtime.container_name(site["id"]))
                except app_support.AppError as exc:
                    raise RuntimeErrorDetail(f"Could not stop the app container: {exc}") from exc
            self._set_site_state(database, site["id"], "stopped", None, None, None)
            try:
                self._write_all_nginx_configs(database)
                if self.nginx_binary:
                    self._reload_nginx()
            except RuntimeErrorDetail:
                pass
        finally:
            lock.release()

    def remove_app(self, site, delete_data: bool):
        runtime = self.container_runtime
        if runtime is None:
            return
        for name in (
            runtime.container_name(site["id"]),
            runtime.container_name(site["id"], "-previous"),
        ):
            runtime.remove(name)
        runtime.remove_images(site["id"])
        if delete_data:
            runtime.remove_volume(site["id"])
            shutil.rmtree(
                Path(self.app.instance_path) / "app-backups" / str(site["id"]),
                ignore_errors=True,
            )

    def app_details(self, site):
        """Container status and recent logs for the app page."""
        runtime = self.container_runtime
        if runtime is None:
            return {"available": False, "status": None, "logs": "", "message": self.apps_status()[1]}
        name = runtime.container_name(site["id"])
        info = runtime.inspect(name)
        running = info is not None and info["State"]["Status"] == "running"
        backups = sorted(
            (Path(self.app.instance_path) / "app-backups" / str(site["id"])).glob("data-*.tar"),
            reverse=True,
        )
        return {
            "available": True,
            "status": info["State"]["Status"] if info else None,
            "started_at": info["State"].get("StartedAt") if info else None,
            "restarts": info.get("RestartCount") if info else None,
            "image": info["Config"]["Image"] if info else None,
            "logs": runtime.logs(name, 200) if info else "",
            "volume": runtime.volume_name(site["id"]),
            "backups": [backup.name for backup in backups],
            "stats": runtime.stats_many([name]).get(name) if running else None,
        }

    def app_status(self, site) -> dict:
        """Cheap container status/usage snapshot for the app page's 2-second
        poll — unlike app_details(), it skips fetching logs and backups."""
        runtime = self.container_runtime
        if runtime is None:
            return {"status": None, "restarts": None, "stats": None}
        name = runtime.container_name(site["id"])
        info = runtime.inspect(name)
        running = info is not None and info["State"]["Status"] == "running"
        return {
            "status": info["State"]["Status"] if info else None,
            "restarts": info.get("RestartCount") if info else None,
            "stats": runtime.stats_many([name]).get(name) if running else None,
        }

    def stats_for_sites(self, site_ids: list[int]) -> dict[int, dict]:
        """Live CPU/memory/network snapshot for many apps in one docker call."""
        runtime = self.container_runtime
        if runtime is None or not site_ids:
            return {}
        name_by_site = {site_id: runtime.container_name(site_id) for site_id in site_ids}
        running = set(runtime.running_app_names())
        names = [name for name in name_by_site.values() if name in running]
        raw = runtime.stats_many(names)
        return {
            site_id: raw[name]
            for site_id, name in name_by_site.items()
            if name in raw
        }

    @property
    def nginx_binary(self):
        configured = self.app.config["NGINX_BINARY"]
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)

    def restore_sites(self, include_apps: bool = True):
        with self.app.app_context():
            sites = get_db().execute(
                "SELECT * FROM sites WHERE status IN ('running', 'starting')"
            ).fetchall()
            for site in sites:
                if site["kind"] == "app":
                    if not include_apps:
                        # A data replica: apps stay stopped until it's
                        # promoted, so nothing writes to data the next
                        # sync round is about to overwrite.
                        continue
                    # Usually instant (container already running with the same
                    # config), but may need a build, so don't block startup.
                    self.start_app_async(site["id"])
                    continue
                try:
                    self.start_site(site["id"])
                except RuntimeErrorDetail:
                    continue

    def restore_gateway(self):
        with self.app.app_context():
            try:
                self.apply_nginx_configs()
            except RuntimeErrorDetail as exc:
                self.app.logger.error(
                    "Could not restore the managed Nginx gateway: %s",
                    exc,
                )

    def migrate_site_configs(self):
        gateway_port = self.app.config["SITE_GATEWAY_PORT"]
        with self.app.app_context():
            database = get_db()
            sites = database.execute("SELECT * FROM sites").fetchall()
            for site in sites:
                if site["kind"] == "app":
                    continue  # app configs are generated fresh on every write
                hostnames = site_hostnames(database, site)
                current = site["nginx_config"]
                upgraded = upgrade_legacy_site_config(current)
                if upgraded != current:
                    try:
                        validate_site_config(
                            upgraded,
                            site["document_root"],
                            site["port"],
                            hostnames,
                            gateway_port if hostnames else None,
                        )
                    except NginxConfigError:
                        upgraded = current
                if upgraded != current:
                    database.execute(
                        "UPDATE sites SET nginx_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (upgraded, site["id"]),
                    )
                if not hostnames:
                    continue
                routed = route_site_config(
                    upgraded,
                    site["port"],
                    hostnames,
                    gateway_port,
                )
                if routed != upgraded:
                    database.execute(
                        """
                        UPDATE sites
                        SET nginx_config = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (routed, site["id"]),
                    )
            database.commit()

    def start_site(self, site_id: int):
        database = get_db()
        site = database.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if site is None:
            raise RuntimeErrorDetail("Site not found.")
        if site["kind"] == "app":
            with self._app_lock(site_id):
                return self._start_app(site_id)

        if self.nginx_binary:
            if site["runtime_backend"] == "builtin":
                self._stop_builtin(site_id, site["runtime_pid"], site["port"])
            try:
                self._write_all_nginx_configs(database, activating_site_id=site_id)
                self._reload_nginx()
            except RuntimeErrorDetail as exc:
                self._set_site_state(database, site_id, "error", "nginx", None, str(exc))
                try:
                    self._write_all_nginx_configs(database)
                except RuntimeErrorDetail:
                    pass
                raise
            self._set_site_state(database, site_id, "running", "nginx", None, None)
            return "nginx"

        self._stop_builtin(site_id, site["runtime_pid"], site["port"])
        if not port_is_available(site["port"]):
            message = f"Port {site['port']} is already in use."
            self._set_site_state(database, site_id, "error", "builtin", None, message)
            raise RuntimeErrorDetail(message)

        log_path = Path(self.app.config["LOG_ROOT"]) / f"site-{site_id}.log"
        log_handle = log_path.open("a", encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "webmanager.site_server",
            "--root",
            site["document_root"],
            "--port",
            str(site["port"]),
            "--index",
            site["index_file"],
        ]
        if site["spa_fallback"]:
            command.append("--spa")

        kwargs = {
            "cwd": str(Path(self.app.root_path).parent),
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        try:
            try:
                process = subprocess.Popen(command, **kwargs)
            except OSError as exc:
                message = f"Could not launch the static server: {exc}"
                self._set_site_state(database, site_id, "error", "builtin", None, message)
                raise RuntimeErrorDetail(message) from exc
        finally:
            log_handle.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                message = (
                    f"Static server exited with code {process.returncode}. "
                    f"See {log_path}."
                )
                self._set_site_state(
                    database,
                    site_id,
                    "error",
                    "builtin",
                    None,
                    message,
                )
                raise RuntimeErrorDetail(message)
            try:
                with socket.create_connection(
                    ("127.0.0.1", site["port"]),
                    timeout=0.2,
                ):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            message = (
                f"Static server did not accept connections within 5 seconds. "
                f"See {log_path}."
            )
            self._set_site_state(
                database,
                site_id,
                "error",
                "builtin",
                None,
                message,
            )
            raise RuntimeErrorDetail(message)

        self.processes[site_id] = process
        self._set_site_state(database, site_id, "running", "builtin", process.pid, None)
        return "builtin"

    def stop_site(self, site_id: int):
        database = get_db()
        site = database.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if site is None:
            raise RuntimeErrorDetail("Site not found.")
        if site["kind"] == "app":
            return self._stop_app(site)

        if site["runtime_backend"] == "nginx" and self.nginx_binary:
            database.execute(
                """
                UPDATE sites
                SET status = 'stopped', runtime_backend = NULL, runtime_pid = NULL,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (site_id,),
            )
            try:
                self._write_all_nginx_configs(database)
                # Graceful reload, never a stop/start: the dashboard itself is
                # often proxied through this Nginx, and a hard stop would cut
                # off the very request that asked to stop the site (502).
                self._reload_nginx()
            except RuntimeErrorDetail:
                database.rollback()
                try:
                    self._write_all_nginx_configs(database)
                    self._reload_nginx()
                except RuntimeErrorDetail:
                    pass
                raise
            else:
                database.commit()
        else:
            self._stop_builtin(site_id, site["runtime_pid"], site["port"])
            self._set_site_state(database, site_id, "stopped", None, None, None)

    def restart_site(self, site_id: int):
        database = get_db()
        site = database.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if site and site["kind"] == "app":
            with self._app_lock(site_id):
                return self._start_app(site_id, force=True)
        if site and site["runtime_backend"] == "builtin":
            self._stop_builtin(site_id, site["runtime_pid"], site["port"])
        return self.start_site(site_id)

    def validate_nginx(self, activating_site_id=None):
        if not self.nginx_binary:
            return False, "Nginx is not installed; syntax validation is unavailable."
        database = get_db()
        try:
            self._write_all_nginx_configs(database, activating_site_id=activating_site_id)
            self._run_nginx(("-t",))
        except RuntimeErrorDetail as exc:
            return False, str(exc)
        return True, "Nginx configuration is valid."

    def sync_nginx_configs(self):
        self._write_all_nginx_configs(get_db())

    def apply_nginx_configs(self):
        self._write_all_nginx_configs(get_db())
        self._reload_nginx()

    def _set_site_state(self, database, site_id, status, backend, pid, error):
        database.execute(
            """
            UPDATE sites
            SET status = ?, runtime_backend = ?, runtime_pid = ?, last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, backend, pid, error, site_id),
        )
        database.commit()

    def _stop_builtin(self, site_id: int, stored_pid: int | None = None, port: int | None = None):
        process = self.processes.pop(site_id, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            return

        if stored_pid and self._is_builtin_process(stored_pid, port):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ("taskkill", "/PID", str(stored_pid), "/T", "/F"),
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                else:
                    os.kill(stored_pid, 15)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _is_builtin_process(self, pid: int, port: int | None) -> bool:
        if pid <= 1:
            return False
        if os.name == "nt":
            return True

        command_path = Path(f"/proc/{pid}/cmdline")
        try:
            command = command_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except OSError:
            return False

        if "webmanager.site_server" not in command:
            return False
        return port is None or f"--port {port}" in command

    def _write_all_nginx_configs(self, database, activating_site_id=None):
        try:
            root = Path(self.app.config["NGINX_ROOT"])
            config_dir = root / "conf.d"
            config_dir.mkdir(parents=True, exist_ok=True)
            for directory in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
                (root / "temp" / directory).mkdir(parents=True, exist_ok=True)

            active = database.execute(
                "SELECT * FROM sites WHERE status = 'running' OR id = ?",
                (activating_site_id or -1,),
            ).fetchall()
            expected_configs = {
                f"{site['id']}-{site['slug']}.conf"
                for site in active
            }
            gateway_port = self.app.config["SITE_GATEWAY_PORT"]
            active_ids = {site["id"] for site in active}
            active_hostnames = set()
            for site in active:
                active_hostnames.update(site_hostnames(database, site))
            paused_configs = {}
            for site in database.execute("SELECT * FROM sites").fetchall():
                if site["id"] in active_ids:
                    continue
                # Stopped sites keep their addresses with a friendly
                # "temporarily unavailable" page instead of a bare 404.
                names = [
                    name
                    for name in site_hostnames(database, site)
                    if name not in active_hostnames
                ]
                try:
                    placeholder = build_paused_site_config(names, gateway_port)
                except NginxConfigError:
                    continue
                if placeholder:
                    filename = f"{site['id']}-{site['slug']}.paused.conf"
                    paused_configs[filename] = placeholder
                    active_hostnames.update(names)
            expected_configs.update(paused_configs)

            for old_config in config_dir.glob("*.conf"):
                if old_config.name not in expected_configs:
                    old_config.unlink()

            for site in active:
                if site["kind"] == "app":
                    try:
                        config = build_app_config(
                            site["name"],
                            site["port"],
                            site_hostnames(database, site),
                            self.app.config["SITE_GATEWAY_PORT"],
                        )
                    except NginxConfigError as exc:
                        if site["id"] == activating_site_id:
                            raise RuntimeErrorDetail(str(exc)) from exc
                        self.app.logger.error("App %s config: %s", site["id"], exc)
                        continue
                    (config_dir / f"{site['id']}-{site['slug']}.conf").write_text(config, encoding="utf-8")
                    continue
                try:
                    hostnames = site_hostnames(database, site)
                    gateway_port = (
                        self.app.config["SITE_GATEWAY_PORT"] if hostnames else None
                    )
                    validate_site_config(
                        site["nginx_config"],
                        site["document_root"],
                        site["port"],
                        hostnames,
                        gateway_port,
                    )
                except NginxConfigError as exc:
                    message = f"{site['name']} has an unsafe Nginx config: {exc}"
                    if site["id"] == activating_site_id:
                        raise RuntimeErrorDetail(message) from exc
                    # Never let one bad config take every other site offline.
                    self.app.logger.error(message)
                    (config_dir / f"{site['id']}-{site['slug']}.conf").unlink(missing_ok=True)
                    database.execute(
                        """
                        UPDATE sites SET status = 'error', runtime_backend = NULL,
                            last_error = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (f"{message}. Open Settings and save to regenerate a safe config.", site["id"]),
                    )
                    database.commit()
                    try:
                        placeholder = build_paused_site_config(hostnames, gateway_port)
                    except NginxConfigError:
                        placeholder = ""
                    if placeholder:
                        (config_dir / f"{site['id']}-{site['slug']}.paused.conf").write_text(
                            placeholder, encoding="utf-8"
                        )
                    continue
                path = config_dir / f"{site['id']}-{site['slug']}.conf"
                path.write_text(site["nginx_config"], encoding="utf-8")

            for filename, placeholder in paused_configs.items():
                (config_dir / filename).write_text(placeholder, encoding="utf-8")

            (root / "nginx.conf").write_text(
                build_main_config(
                    root,
                    config_dir,
                    self.app.config["SITE_GATEWAY_PORT"]
                    if (
                        database.execute("SELECT 1 FROM domains LIMIT 1").fetchone()
                        or database.execute(
                            "SELECT 1 FROM dashboard_domains LIMIT 1"
                        ).fetchone()
                    )
                    else None,
                    tuple(
                        row["name"]
                        for row in database.execute(
                            """
                            SELECT name FROM dashboard_domains
                            ORDER BY is_primary DESC, name COLLATE NOCASE
                            """
                        ).fetchall()
                    ),
                    self.app.config["PORT"],
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise RuntimeErrorDetail(f"Could not write the managed Nginx configuration: {exc}") from exc

    def _run_nginx(self, arguments):
        root = Path(self.app.config["NGINX_ROOT"]).resolve()
        command = [
            str(self.nginx_binary),
            "-p",
            f"{root}{os.sep}",
            "-c",
            "nginx.conf",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeErrorDetail("Nginx is no longer available on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeErrorDetail("Nginx did not respond within 30 seconds.") from exc
        except OSError as exc:
            raise RuntimeErrorDetail(f"Could not run Nginx: {exc}") from exc
        if result.returncode != 0:
            raise RuntimeErrorDetail((result.stderr or result.stdout).strip() or "Nginx command failed.")
        return result

    def _reload_nginx(self):
        root = Path(self.app.config["NGINX_ROOT"])
        self._run_nginx(("-t",))
        pid_file = root / "nginx.pid"
        if self._nginx_is_running(pid_file):
            self._run_nginx(("-s", "reload"))
        else:
            pid_file.unlink(missing_ok=True)
            self._run_nginx(())

    def _restart_nginx(self):
        root = Path(self.app.config["NGINX_ROOT"])
        pid_file = root / "nginx.pid"
        self._run_nginx(("-t",))
        if self._nginx_is_running(pid_file):
            # "quit" lets in-flight requests finish, unlike "stop".
            self._run_nginx(("-s", "quit"))
            deadline = time.monotonic() + 10
            while self._nginx_is_running(pid_file) and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._nginx_is_running(pid_file):
                raise RuntimeErrorDetail(
                    "Managed Nginx did not stop within 10 seconds."
                )
        pid_file.unlink(missing_ok=True)
        self._run_nginx(())

    def _nginx_is_running(self, pid_file: Path) -> bool:
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if pid <= 1:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False

        if os.name != "nt":
            command_path = Path(f"/proc/{pid}/cmdline")
            try:
                command = command_path.read_bytes().replace(b"\0", b" ")
            except OSError:
                return False
            if b"nginx" not in command:
                return False
        return True
