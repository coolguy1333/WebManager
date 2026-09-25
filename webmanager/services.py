import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


from .db import get_db
from .domains import site_hostnames
from .nginx import (
    NginxConfigError,
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

    @property
    def nginx_binary(self):
        configured = self.app.config["NGINX_BINARY"]
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)

    def restore_sites(self):
        with self.app.app_context():
            sites = get_db().execute("SELECT * FROM sites WHERE status = 'running'").fetchall()
            for site in sites:
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
