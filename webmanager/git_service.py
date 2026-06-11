import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urlunparse


IGNORED_FOLDERS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
}
INDEX_NAMES = ("index.html", "index.htm")
SCP_STYLE_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\s]+$")


class GitError(RuntimeError):
    pass


def validate_repo_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise GitError("Repository URL is required.")
    if len(url) > 2048:
        raise GitError("Repository URL is too long.")

    if SCP_STYLE_RE.fullmatch(url):
        return url

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
        raise GitError("Use an HTTP(S), SSH, or Git repository URL.")
    return url


def display_repo_url(url: str) -> str:
    if SCP_STYLE_RE.fullmatch(url):
        return url
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunparse((parsed.scheme, hostname, parsed.path, "", "", ""))


def repo_name_from_url(url: str) -> str:
    path = url.split(":", 1)[1] if SCP_STYLE_RE.fullmatch(url) else urlparse(url).path
    name = Path(path.rstrip("/")).name
    if name.lower().endswith(".git"):
        name = name[:-4]
    return name or "repository"


def clone_repository(
    url: str,
    target: Path,
    branch: str | None = None,
    validate_staging=None,
):
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.clone-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"

    command = [
        "git",
        "-c",
        "protocol.file.allow=never",
        "clone",
        "--depth",
        "1",
        "--no-tags",
    ]
    if branch:
        command.extend(("--branch", branch, "--single-branch"))
    command.extend(("--", url, str(staging)))

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("Git is not installed or is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("The repository clone timed out after 3 minutes.") from exc
    except OSError as exc:
        raise GitError(f"Could not run Git: {exc}") from exc
    finally:
        if "result" not in locals() or result.returncode != 0:
            try:
                _remove_path(staging)
            except OSError:
                pass

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "Authentication failed" in detail or "could not read Username" in detail:
            detail = "Authentication failed. Configure Git credentials or SSH keys on this server."
        raise GitError(detail[-1200:] or "Git clone failed.")

    if validate_staging is not None:
        try:
            validate_staging(staging)
        except GitError:
            _remove_path(staging)
            raise
        except Exception as exc:
            _remove_path(staging)
            raise GitError(f"Repository validation failed: {exc}") from exc

    activate_repository(staging, target, backup=backup)


def activate_repository(staging: Path, target: Path, backup: Path | None = None):
    if not staging.is_dir():
        raise GitError("The staged repository update is missing.")
    backup = backup or target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    try:
        if target.exists() or target.is_symlink():
            target.rename(backup)
        staging.rename(target)
    except OSError as exc:
        try:
            _remove_path(staging)
        except OSError:
            pass
        if backup.exists() and not target.exists():
            try:
                backup.rename(target)
            except OSError as restore_exc:
                raise GitError(
                    f"Could not activate or restore the repository: {restore_exc}"
                ) from restore_exc
        raise GitError(f"Could not activate the refreshed repository: {exc}") from exc
    else:
        try:
            _remove_path(backup)
        except OSError:
            pass


def repository_commit(path: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(path), "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"Could not read the repository revision: {exc}") from exc
    commit = result.stdout.strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise GitError(
            (result.stderr or result.stdout).strip()[-1200:]
            or "Could not read the repository revision."
        )
    return commit


def remove_repository_path(path: Path):
    _remove_path(path)


def _remove_path(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def find_index_folders(repository_path: Path) -> list[dict]:
    matches = []
    for current_name, dirs, files in os.walk(repository_path):
        current = Path(current_name)
        dirs[:] = sorted(
            directory
            for directory in dirs
            if directory not in IGNORED_FOLDERS and not directory.startswith(".")
        )
        lower_files = {filename.lower(): filename for filename in files}
        index_name = next((lower_files[name] for name in INDEX_NAMES if name in lower_files), None)
        if not index_name:
            continue

        relative = current.relative_to(repository_path)
        folder = "." if relative == Path(".") else relative.as_posix()
        matches.append(
            {
                "folder": folder,
                "index_file": index_name,
                "depth": 0 if folder == "." else len(PurePosixPath(folder).parts),
            }
        )

    matches.sort(key=lambda item: (item["depth"], item["folder"].lower()))
    return matches


def resolve_folder(repository_path: Path, folder: str) -> Path:
    relative = PurePosixPath(folder or ".")
    if relative.is_absolute() or ".." in relative.parts:
        raise GitError("Invalid repository folder.")

    root = repository_path.resolve()
    selected = (root / Path(*relative.parts)).resolve()
    if selected != root and root not in selected.parents:
        raise GitError("Selected folder is outside the repository.")
    if not selected.is_dir():
        raise GitError("Selected folder no longer exists.")
    return selected
