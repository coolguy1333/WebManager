"""App hosting: run a repository as a container behind WebManager's Nginx.

See docs/APP_HOSTING.md for the contract an app repository must follow.

Pieces in this module:
- ``load_manifest`` validates ``webmanager.json`` next to a ``Dockerfile``.
- ``encrypt_env`` / ``decrypt_env`` keep dashboard-edited variables
  encrypted at rest (key derived from WebManager's SECRET_KEY).
- ``ContainerRuntime`` wraps the docker/podman CLI with hardened defaults.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MANIFEST_NAME = "webmanager.json"
DOCKERFILE_NAME = "Dockerfile"
CONTAINER_PORT = 8080
DATA_MOUNT = "/data"
RESERVED_VARIABLES = {
    "PORT",
    "HOST",
    "DATA_DIR",
    "PUBLIC_URL",
    "TRUST_PROXY",
    "WEBMANAGER_APP_ID",
}
VARIABLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HEALTH_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/%-]{0,199}$")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_VARIABLES = 100
MAX_VALUE_LENGTH = 8192
MIN_MEMORY_MB = 64
MAX_MEMORY_MB = 4096
IGNORED_FOLDERS = {".git", "node_modules", ".venv", "venv", "__pycache__", "vendor"}
BACKUPS_TO_KEEP = 3


class AppError(ValueError):
    """A problem with an app's repository, settings, or container."""


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def load_manifest(folder: Path, default_memory_mb: int = 256) -> dict:
    """Read and validate ``webmanager.json`` in ``folder``.

    Returns a normalised manifest. Raises AppError with a message that tells
    the app author exactly what to fix.
    """
    folder = Path(folder)
    dockerfile = folder / DOCKERFILE_NAME
    manifest_path = folder / MANIFEST_NAME
    if not dockerfile.is_file() or dockerfile.is_symlink():
        raise AppError(f"{DOCKERFILE_NAME} is missing from this folder.")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise AppError(f"{MANIFEST_NAME} is missing from this folder.")
    if (folder / ".env").exists():
        raise AppError(
            "This folder contains a .env file. Remove it from the repository and set "
            "those values in WebManager instead; secrets must never be committed."
        )
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise AppError(f"{MANIFEST_NAME} is larger than 64 KB.")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AppError(f"{MANIFEST_NAME} must contain a JSON object.")
    if raw.get("type") != "app":
        raise AppError(f'{MANIFEST_NAME} must include "type": "app".')

    health = raw.get("health")
    if not isinstance(health, str) or not HEALTH_PATH_RE.fullmatch(health):
        raise AppError('"health" must be a path such as "/health".')

    memory = raw.get("memory_mb", default_memory_mb)
    if not isinstance(memory, int) or isinstance(memory, bool) or not (
        MIN_MEMORY_MB <= memory <= MAX_MEMORY_MB
    ):
        raise AppError(f'"memory_mb" must be a whole number from {MIN_MEMORY_MB} to {MAX_MEMORY_MB}.')

    variables = raw.get("env", [])
    if not isinstance(variables, list) or len(variables) > MAX_VARIABLES:
        raise AppError(f'"env" must be a list of at most {MAX_VARIABLES} settings.')
    normalised = []
    seen = set()
    for index, entry in enumerate(variables, start=1):
        if not isinstance(entry, dict):
            raise AppError(f"env entry {index} must be an object.")
        name = entry.get("name")
        if not isinstance(name, str) or not VARIABLE_NAME_RE.fullmatch(name):
            raise AppError(f"env entry {index}: name must be UPPER_SNAKE_CASE.")
        if name in RESERVED_VARIABLES:
            raise AppError(f"{name} is set by WebManager and can't be listed in env.")
        if name in seen:
            raise AppError(f"{name} is listed more than once.")
        seen.add(name)
        description = entry.get("description", "")
        default = entry.get("default")
        choices = entry.get("choices")
        if not isinstance(description, str) or len(description) > 500:
            raise AppError(f"{name}: description must be text up to 500 characters.")
        if default is not None and (
            not isinstance(default, str)
            or len(default) > MAX_VALUE_LENGTH
            or any(ch in default for ch in ("\n", "\r", "\x00"))
        ):
            raise AppError(f"{name}: default must be a single-line string.")
        if choices is not None:
            if (
                not isinstance(choices, list)
                or not choices
                or len(choices) > 50
                or not all(
                    isinstance(choice, str)
                    and choice
                    and len(choice) <= 200
                    and not any(ch in choice for ch in ("\n", "\r", "\x00"))
                    for choice in choices
                )
            ):
                raise AppError(f"{name}: choices must be a list of non-empty strings.")
            if default is not None and default not in choices:
                raise AppError(f"{name}: default must be one of the choices.")
        for flag in ("secret", "required"):
            if flag in entry and not isinstance(entry[flag], bool):
                raise AppError(f"{name}: {flag} must be true or false.")
        normalised.append(
            {
                "name": name,
                "description": description,
                "secret": bool(entry.get("secret", False)),
                "required": bool(entry.get("required", False)),
                "default": default,
                "choices": choices,
            }
        )
    return {"type": "app", "health": health, "memory_mb": memory, "env": normalised}


def find_app_folders(repository_root: Path, max_depth: int = 4) -> list[dict]:
    """Folders containing both a Dockerfile and webmanager.json."""
    root = Path(repository_root)
    found = []
    if not root.is_dir():
        return found
    for current, dirs, files in os.walk(root):
        relative = Path(current).relative_to(root)
        depth = 0 if relative == Path(".") else len(relative.parts)
        dirs[:] = sorted(
            d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith(".") and depth < max_depth
        )
        if MANIFEST_NAME in files and DOCKERFILE_NAME in files:
            folder = "." if relative == Path(".") else relative.as_posix()
            try:
                manifest = load_manifest(Path(current))
                error = None
            except AppError as exc:
                manifest, error = None, str(exc)
            found.append({"folder": folder, "manifest": manifest, "error": error})
    return found


# ---------------------------------------------------------------------------
# Encrypted variables
# ---------------------------------------------------------------------------


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(b"webmanager-app-env:" + str(secret_key).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_env(values: dict, secret_key: str) -> str:
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet(secret_key).encrypt(payload).decode("ascii")


def decrypt_env(token: str | None, secret_key: str) -> dict:
    if not token:
        return {}
    try:
        data = json.loads(_fernet(secret_key).decrypt(token.encode("ascii")))
    except (InvalidToken, ValueError) as exc:
        raise AppError(
            "Saved app settings could not be decrypted (was the WebManager secret key "
            "changed?). Re-enter them under Variables."
        ) from exc
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def validate_value(name: str, value: str, variable: dict) -> str | None:
    """Return an error message for an unacceptable value, else None."""
    if len(value) > MAX_VALUE_LENGTH:
        return f"{name} is longer than {MAX_VALUE_LENGTH} characters."
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        return f"{name} can't contain line breaks."
    if variable.get("choices") and value and value not in variable["choices"]:
        return f"{name} must be one of: {', '.join(variable['choices'])}."
    return None


def effective_env(manifest: dict, stored: dict, *, app_id: int, public_url: str) -> tuple[dict, list]:
    """Build the container environment. Returns (env, missing_required_names)."""
    env = {}
    missing = []
    for variable in manifest["env"]:
        value = stored.get(variable["name"])
        if value in (None, "") and variable["default"] is not None:
            value = variable["default"]
        if value in (None, ""):
            if variable["required"]:
                missing.append(variable["name"])
            continue
        env[variable["name"]] = value
    env.update(
        {
            "PORT": str(CONTAINER_PORT),
            "HOST": "0.0.0.0",  # noqa: S104 - inside the container only
            "DATA_DIR": DATA_MOUNT,
            "PUBLIC_URL": public_url,
            "TRUST_PROXY": "true",
            "WEBMANAGER_APP_ID": str(app_id),
        }
    )
    return env, missing


# ---------------------------------------------------------------------------
# Container runtime
# ---------------------------------------------------------------------------


class ContainerRuntime:
    """Thin wrapper around the docker/podman CLI with hardened defaults."""

    def __init__(self, binary: str, work_dir: Path, logger=None):
        self.binary = binary
        self.work_dir = Path(work_dir)
        self.logger = logger

    @staticmethod
    def detect(configured: str = "") -> str | None:
        candidates = [configured] if configured else ["docker", "podman"]
        for candidate in candidates:
            found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
            if found:
                return found
        return None

    # names -----------------------------------------------------------------
    @staticmethod
    def container_name(site_id: int, suffix: str = "") -> str:
        return f"webmanager-app-{site_id}{suffix}"

    @staticmethod
    def volume_name(site_id: int) -> str:
        return f"webmanager-app-{site_id}-data"

    @staticmethod
    def image_name(site_id: int, tag: str) -> str:
        safe = re.sub(r"[^a-z0-9_.-]", "", tag.lower())[:64] or "latest"
        return f"webmanager-app-{site_id}:{safe}"

    # helpers ---------------------------------------------------------------
    def _run(self, arguments, timeout=60, check=True):
        try:
            result = subprocess.run(
                [self.binary, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AppError(f"{self.binary} is not installed.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"{Path(self.binary).name} {arguments[0]} timed out after {timeout} seconds.") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise AppError(detail or f"{Path(self.binary).name} {arguments[0]} failed.")
        return result

    def available(self) -> tuple[bool, str]:
        try:
            result = self._run(["info", "--format", "{{.ServerVersion}}"], timeout=15, check=False)
        except AppError as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()[-300:] or "Container runtime is not reachable."
        return True, result.stdout.strip()

    # images ----------------------------------------------------------------
    def image_exists(self, image: str) -> bool:
        return self._run(["image", "inspect", image], timeout=30, check=False).returncode == 0

    def build(self, site_id: int, context: Path, tag: str, log_path: Path | None = None) -> str:
        image = self.image_name(site_id, tag)
        result = self._run(
            ["build", "--label", f"webmanager.app={site_id}", "-t", image, str(context)],
            timeout=900,
            check=False,
        )
        if log_path is not None:
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"\n--- build {image} ---\n{result.stdout}{result.stderr}\n")
            except OSError:
                pass
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise AppError(f"The app image failed to build:\n{detail}")
        return image

    def remove_images(self, site_id: int, keep: str | None = None):
        result = self._run(
            ["images", "--filter", f"label=webmanager.app={site_id}", "--format", "{{.Repository}}:{{.Tag}}"],
            timeout=30,
            check=False,
        )
        for image in result.stdout.split():
            if image != keep:
                self._run(["rmi", "-f", image], timeout=60, check=False)

    # containers ------------------------------------------------------------
    def inspect(self, name: str) -> dict | None:
        result = self._run(["inspect", "--type", "container", name], timeout=30, check=False)
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data[0] if data else None

    def status(self, name: str) -> str | None:
        info = self.inspect(name)
        return info["State"]["Status"] if info else None

    def run(self, *, site_id, image, name, host_port, env, memory_mb, cpus, config_hash):
        """Start a hardened container. Variables go through a 0600 env-file so
        secrets never appear in the process list."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        descriptor, env_path = tempfile.mkstemp(prefix=".env-", dir=self.work_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                for key, value in env.items():
                    handle.write(f"{key}={value}\n")
            os.chmod(env_path, 0o600)
            self._run(
                [
                    "run",
                    "--detach",
                    "--name", name,
                    "--label", f"webmanager.app={site_id}",
                    "--label", f"webmanager.config={config_hash}",
                    "--restart", "unless-stopped",
                    "--read-only",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                    "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges",
                    "--memory", f"{memory_mb}m",
                    "--memory-swap", f"{memory_mb}m",
                    "--cpus", str(cpus),
                    "--pids-limit", "256",
                    "--publish", f"127.0.0.1:{host_port}:{CONTAINER_PORT}",
                    "--volume", f"{self.volume_name(site_id)}:{DATA_MOUNT}",
                    "--env-file", env_path,
                    "--log-opt", "max-size=10m",
                    "--log-opt", "max-file=3",
                    image,
                ],
                timeout=120,
            )
        finally:
            Path(env_path).unlink(missing_ok=True)

    def stop(self, name: str, timeout: int = 10):
        if self.inspect(name):
            self._run(["stop", "--time", str(timeout), name], timeout=timeout + 30, check=False)

    def start(self, name: str):
        self._run(["start", name], timeout=60)

    def remove(self, name: str):
        self._run(["rm", "--force", name], timeout=60, check=False)

    def rename(self, old: str, new: str):
        self._run(["rename", old, new], timeout=30)

    def remove_volume(self, site_id: int):
        self._run(["volume", "rm", "--force", self.volume_name(site_id)], timeout=60, check=False)

    def logs(self, name: str, tail: int = 200) -> str:
        result = self._run(["logs", "--tail", str(tail), "--timestamps", name], timeout=30, check=False)
        return (result.stdout + result.stderr).strip()

    def backup_data(self, name: str, destination: Path) -> Path | None:
        """Copy /data out of a (running or stopped) container as a tar file."""
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"data-{time.strftime('%Y%m%d-%H%M%S')}.tar"
        try:
            with open(target, "wb") as handle:
                result = subprocess.run(
                    [self.binary, "cp", f"{name}:{DATA_MOUNT}", "-"],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    timeout=600,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            target.unlink(missing_ok=True)
            return None
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            return None
        backups = sorted(destination.glob("data-*.tar"))
        for old in backups[:-BACKUPS_TO_KEEP]:
            old.unlink(missing_ok=True)
        return target


def config_hash(image: str, env: dict, memory_mb: int, cpus: float, host_port: int) -> str:
    payload = json.dumps([image, sorted(env.items()), memory_mb, cpus, host_port])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def wait_until_healthy(host_port: int, path: str, timeout: float = 45.0, is_running=None) -> tuple[bool, str]:
    """Poll the app's health URL until it answers 2xx/3xx or time runs out."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{host_port}{path}"
    last = "no response"
    while time.monotonic() < deadline:
        if is_running is not None and not is_running():
            return False, "the container exited while starting"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed loopback URL
                if 200 <= response.status < 400:
                    return True, "healthy"
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        time.sleep(0.5)
    return False, f"health check {path} failed ({last})"
