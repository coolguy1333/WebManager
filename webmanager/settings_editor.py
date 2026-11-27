"""WebManager settings a super admin can change on the System page.

Only the settings listed here can be changed from the web; everything else in
/etc/webmanager/webmanager.env (secrets, addresses, the replica setup, the
Google client) stays a shell-only change. The web app never writes the file:
it files a request (server_control.request_settings) and the root helper
``webmanager-control`` (deploy/debian/control.sh) checks it again with this
same module, run as a script, writes the file, restarts WebManager and puts
the old file back if WebManager doesn't come up.

Standard library only: the root helper runs this file with the system Python.
Set WEBMANAGER_DASHBOARD_SETTINGS=0 in the file to turn this off.
"""

import json
import os
import re
import sys
import tempfile

EMAIL_RE = re.compile(r"^[^@\s,=\"'\\]+@[^@\s,=\"'\\]+\.[^@\s,=\"'\\]+$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
MAX_REQUEST_BYTES = 16 * 1024


def _setting(key, label, kind, help_text, group, *, config=None, low=None, high=None, choices=None, apps=False):
    return {
        "key": key, "label": label, "kind": kind, "help": help_text, "group": group,
        "config": config, "low": low, "high": high, "choices": choices, "apps": apps,
    }


SETTINGS = [
    _setting("WEBMANAGER_GOOGLE_ALLOWED_EMAILS", "Allowed Google emails", "emails",
             "Comma-separated. Only these accounts (and Workspace domains below) can sign in. "
             "With both empty, only existing accounts can sign in.",
             "Sign-in", config="GOOGLE_ALLOWED_EMAILS"),
    _setting("WEBMANAGER_GOOGLE_ALLOWED_DOMAINS", "Allowed Google Workspace domains", "domains",
             "Comma-separated, for example example.com.", "Sign-in", config="GOOGLE_ALLOWED_DOMAINS"),
    _setting("WEBMANAGER_SESSION_COOKIE_SECURE", "Secure sign-in cookies", "flag",
             "Send the sign-in cookie only over https. Turn on when the dashboard is always opened over https; "
             "signing in at an http:// address then stops working.",
             "Sign-in", config="SESSION_COOKIE_SECURE"),
    _setting("WEBMANAGER_TRUST_PROXY", "Behind a proxy on this machine", "flag",
             "Believe X-Forwarded-* headers from the local Nginx (the normal setup).",
             "Sign-in", config="TRUST_PROXY"),
    _setting("WEBMANAGER_SITE_PUBLIC_SCHEME", "Sites are served over", "choice",
             "https when visitors reach the sites over https (for example through Cloudflare Tunnel). "
             "Running apps need a restart to get the new PUBLIC_URL.",
             "Sites", config="SITE_PUBLIC_SCHEME", choices=("http", "https")),
    _setting("WEBMANAGER_AUTO_REFRESH_ENABLED", "Scheduled source update checks", "flag",
             "Check Git sources for updates on their schedules.", "Sites", config="AUTO_REFRESH_ENABLED"),
    _setting("WEBMANAGER_MAX_REPOSITORY_MB", "Largest Git source (MB)", "int",
             "Bigger repositories are refused.", "Sites", config="MAX_REPOSITORY_BYTES", low=1, high=102400),
    _setting("WEBMANAGER_BACKUPS_ENABLED", "Daily backups", "flag",
             "Back up the database, keys and settings every day.", "Keeping it running", config="BACKUPS_ENABLED"),
    _setting("WEBMANAGER_BACKUP_KEEP", "Backups to keep", "int", "Older ones are deleted.",
             "Keeping it running", config="BACKUP_KEEP", low=1, high=365),
    _setting("WEBMANAGER_WATCHDOG_ENABLED", "Watchdog", "flag",
             "Restart what died and track uptime.", "Keeping it running", config="WATCHDOG_ENABLED"),
    _setting("WEBMANAGER_APP_DEFAULT_MEMORY_MB", "Default memory per app (MB)", "int", "For new apps.",
             "Apps", config="APP_DEFAULT_MEMORY_MB", low=32, high=262144, apps=True),
    _setting("WEBMANAGER_APP_CPUS", "Default CPUs per app", "float", "For new apps, for example 0.5.",
             "Apps", config="APP_CPUS", low=0.05, high=256, apps=True),
    _setting("WEBMANAGER_APP_MAX_MEMORY_MB", "Most memory an owner may give an app (MB)", "int",
             "Super admins can go up to what the machine has.", "Apps", config="APP_MAX_MEMORY_MB",
             low=32, high=262144, apps=True),
    _setting("WEBMANAGER_APP_MAX_CPUS", "Most CPUs an owner may give an app", "float", "",
             "Apps", config="APP_MAX_CPUS", low=0.05, high=256, apps=True),
    _setting("WEBMANAGER_APP_START_TIMEOUT", "App start timeout (seconds)", "int",
             "How long an app may take to start answering.", "Apps", config="APP_START_TIMEOUT",
             low=10, high=900, apps=True),
]
BY_KEY = {setting["key"]: setting for setting in SETTINGS}


def normalize(key: str, raw) -> str:
    """The value as it goes into the file. Raises ValueError with a message."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise ValueError(f"{key} can't be changed from the dashboard.")
    value = str(raw if raw is not None else "").strip()
    label = setting["label"]
    kind = setting["kind"]
    if kind == "flag":
        if value.lower() in {"1", "true", "on", "yes"}:
            return "1"
        if value.lower() in {"0", "false", "off", "no", ""}:
            return "0"
        raise ValueError(f"{label}: use on or off.")
    if kind == "choice":
        if value.lower() in setting["choices"]:
            return value.lower()
        raise ValueError(f"{label}: choose {' or '.join(setting['choices'])}.")
    if kind in ("int", "float"):
        try:
            number = int(value) if kind == "int" else float(value)
        except ValueError:
            raise ValueError(f"{label}: enter a number.") from None
        if not setting["low"] <= number <= setting["high"]:
            raise ValueError(f"{label}: must be between {setting['low']:g} and {setting['high']:g}.")
        return str(number) if kind == "int" else f"{number:g}"
    items = []
    for item in value.replace(";", ",").replace(" ", ",").split(","):
        item = item.strip().lower()
        if not item or item in items:
            continue
        pattern = EMAIL_RE if kind == "emails" else DOMAIN_RE
        if len(item) > 254 or not pattern.fullmatch(item):
            raise ValueError(f"{label}: {item} isn't a valid {'email address' if kind == 'emails' else 'domain'}.")
        items.append(item)
    if len(items) > 200:
        raise ValueError(f"{label}: at most 200 entries.")
    return ",".join(items)


def validate(changes) -> dict:
    """{key: file value} for a request. Raises ValueError."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("No settings were given.")
    return {key: normalize(key, value) for key, value in changes.items()}


def current_values(config) -> dict:
    """{key: file value} as the running WebManager has them."""
    values = {}
    for setting in SETTINGS:
        value = config.get(setting["config"])
        if setting["key"] == "WEBMANAGER_MAX_REPOSITORY_MB":
            value = int(value or 0) // (1024 * 1024)
        elif setting["kind"] == "flag":
            value = "1" if value else "0"
        try:
            values[setting["key"]] = normalize(setting["key"], value)
        except ValueError:
            values[setting["key"]] = str(value if value is not None else "")
    return values


def apply_to_file(path: str, changes: dict) -> list[str]:
    """Write the changes into the settings file, keeping everything else
    (other settings, comments, order, owner and mode). Returns the keys whose
    value changed."""
    changes = validate(changes)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    changed, seen = [], set()
    output = []
    for line in lines:
        key, sep, old = line.partition("=")
        key = key.strip()
        if sep and key in changes and not line.lstrip().startswith("#"):
            if key in seen:
                continue  # a later duplicate would override the new value
            seen.add(key)
            if old.strip().strip("'\"") != changes[key]:
                changed.append(key)
            output.append(f"{key}={changes[key]}")
        else:
            output.append(line)
    for key, value in changes.items():
        if key not in seen:
            output.append(f"{key}={value}")
            changed.append(key)
    status = os.stat(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".webmanager.env.", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, status.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(temporary, status.st_uid, status.st_gid)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return changed


def main(argv) -> int:
    """webmanager-control runs: settings_editor.py apply REQUEST_FILE ENV_FILE"""
    if len(argv) != 4 or argv[1] != "apply":
        print("usage: settings_editor.py apply REQUEST_FILE ENV_FILE", file=sys.stderr)
        return 2
    try:
        with open(argv[2], encoding="utf-8") as handle:
            text = handle.read(MAX_REQUEST_BYTES + 1)
        if len(text) > MAX_REQUEST_BYTES:
            raise ValueError("The request is too large.")
        try:
            request = json.loads(text)
        except ValueError:
            raise ValueError("The request was not understood.") from None
        changed = apply_to_file(argv[3], request)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
