import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from flask import current_app

from .db import get_db
from .nginx import (
    NginxConfigError,
    build_main_config,
    route_site_config,
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

    def migrate_site_configs(self):
        domain = self.app.config["SITE_BASE_DOMAIN"]
        if not domain:
            return
        gateway_port = self.app.config["SITE_GATEWAY_PORT"]
        with self.app.app_context():
            database = get_db()
            sites = database.execute("SELECT * FROM sites").fetchall()
            for site in sites:
                hostname = f"{site['slug']}.{domain}"
                routed = route_site_config(
                    site["nginx_config"],
                    site["port"],
                    hostname,
                    gateway_port,
                )
                if routed != site["nginx_config"]:
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

        time.sleep(0.15)
        if process.poll() is not None:
            message = f"Static server exited with code {process.returncode}. See {log_path}."
            self._set_site_state(database, site_id, "error", "builtin", None, message)
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
                self._restart_nginx()
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

            for old_config in config_dir.glob("*.conf"):
                if old_config.name not in expected_configs:
                    old_config.unlink()

            for site in active:
                try:
                    hostname = None
                    gateway_port = None
                    if self.app.config["SITE_BASE_DOMAIN"]:
                        hostname = (
                            f"{site['slug']}.{self.app.config['SITE_BASE_DOMAIN']}"
                        )
                        gateway_port = self.app.config["SITE_GATEWAY_PORT"]
                    validate_site_config(
                        site["nginx_config"],
                        site["document_root"],
                        site["port"],
                        hostname,
                        gateway_port,
                    )
                except NginxConfigError as exc:
                    raise RuntimeErrorDetail(
                        f"{site['name']} has an unsafe Nginx config: {exc}"
                    ) from exc
                path = config_dir / f"{site['id']}-{site['slug']}.conf"
                path.write_text(site["nginx_config"], encoding="utf-8")

            (root / "nginx.conf").write_text(
                build_main_config(
                    root,
                    config_dir,
                    self.app.config["SITE_GATEWAY_PORT"]
                    if self.app.config["SITE_BASE_DOMAIN"]
                    else None,
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
            self._run_nginx(("-s", "stop"))
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
