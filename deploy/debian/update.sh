#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/webmanager
DATA_DIR=/var/lib/webmanager
CONFIG_DIR=/etc/webmanager
STATE_DIR=/var/lib/webmanager-updater
STATUS_FILE=$STATE_DIR/status.json
REQUEST_FILE=$STATE_DIR/requests/install.commit
CHECK_REQUEST_FILE=$STATE_DIR/requests/check
BACKUP_ROOT=$STATE_DIR/backups
LOCK_FILE=/run/lock/webmanager-update.lock
SERVICE_FILE=/etc/systemd/system/webmanager.service
ENV_FILE=/etc/webmanager/updater.env
NGINX_AVAILABLE=/etc/nginx/sites-available/webmanager
SITE_NGINX_AVAILABLE=/etc/nginx/sites-available/webmanager-sites
APP_ENV_FILE=/etc/webmanager/webmanager.env
# Present when a super admin switched on installing new versions by themselves.
AUTO_FILE=$STATE_DIR/requests/auto-install
# What went wrong the last time a version was tried (.commit/.message/.detail).
# It keeps the System page showing why, instead of going back to "update
# available", and stops an automatic install hammering a version that fails.
FAILURE_RECORD=$STATE_DIR/last-failure
AUTO_RETRY_MINUTES=360
# install.sh appends anything worth telling the admin here (for example that a
# new Nginx configuration was refused and the old one kept).
NOTES_FILE=$STATE_DIR/install-notes
# Big data that an update cannot damage and that can be rebuilt (repository
# checkouts, logs, container data backups) is left out of the pre-update
# backup; the database, keys and settings are what a rollback needs.
DATA_BACKUP_EXCLUDES="repositories logs app-work app-backups"
REPOSITORY=
BRANCH=

if [[ $EUID -ne 0 ]]; then
    echo "Run the WebManager updater as root or through systemd." >&2
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another WebManager update check is already running."
    exit 0
fi

install -d -o root -g webmanager -m 0710 "$STATE_DIR"
install -d -o webmanager -g webmanager -m 0750 "$STATE_DIR/requests"
install -d -o root -g root -m 0750 "$BACKUP_ROOT"
rm -f "$CHECK_REQUEST_FILE"

write_status() {
    local state=$1
    local installed=${2:-}
    local available=${3:-}
    local message=${4:-}
    local detail=${5:-}
    local automatic=0
    local temporary
    [[ -e $AUTO_FILE ]] && automatic=1
    temporary=$(mktemp "$STATE_DIR/status.XXXXXX")
    python3 - "$temporary" "$state" "$installed" "$available" "$message" \
        "$detail" "$REPOSITORY" "$BRANCH" "$automatic" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, state, installed, available, message, detail, repository, branch, automatic = sys.argv[1:]
payload = {
    "state": state,
    "installed_commit": installed or None,
    "available_commit": available or None,
    "update_available": bool(available and available != installed),
    "message": message,
    # What went wrong, for the System page (the tail of the failing output).
    "detail": detail[-3500:] or None,
    # The source this updater follows, so the app checks the same one.
    "repository": repository or None,
    "branch": branch or None,
    "auto_install": automatic == "1",
    # No "UTC"/"Z" suffix: matches the plain "YYYY-MM-DD HH:MM:SS" convention
    # used for every other stored timestamp, which the "ago" filter and
    # admin._auto_request_program_check() parse with datetime.fromisoformat().
    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
    handle.write("\n")
os.chmod(path, 0o640)
PY
    chown root:webmanager "$temporary"
    mv -f "$temporary" "$STATUS_FILE"
}

wait_for_webmanager() {
    local app_host app_port health_host
    app_host=$(sed -n 's/^WEBMANAGER_HOST=//p' "$APP_ENV_FILE" | tail -n 1)
    app_port=$(sed -n 's/^WEBMANAGER_PORT=//p' "$APP_ENV_FILE" | tail -n 1)
    app_host=${app_host:-127.0.0.1}
    app_port=${app_port:-5000}
    case "$app_host" in
        0.0.0.0 | "::")
            health_host=127.0.0.1
            ;;
        *:*)
            health_host="[$app_host]"
            ;;
        *)
            health_host=$app_host
            ;;
    esac

    for _ in {1..30}; do
        if systemctl is-active --quiet webmanager \
            && "$APP_DIR/.venv/bin/python" -c \
                "import urllib.request; urllib.request.urlopen('http://${health_host}:${app_port}/healthz', timeout=2).read()" \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

sync_data_backup() {
    local destination=$1
    # shellcheck disable=SC2086
    python3 - "$DATA_DIR" "$destination" $DATA_BACKUP_EXCLUDES <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
# Top-level entries that are not part of the backup (large and rebuildable).
excluded = set(sys.argv[3:])


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def copy_owner(source_path, destination_path, follow_symlinks=True):
    details = source_path.stat() if follow_symlinks else source_path.lstat()
    os.chown(
        destination_path,
        details.st_uid,
        details.st_gid,
        follow_symlinks=follow_symlinks,
    )


def sync_directory(source_dir, destination_dir, top_level=False):
    if destination_dir.is_symlink() or (
        destination_dir.exists() and not destination_dir.is_dir()
    ):
        remove_path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    skipped = excluded if top_level else set()
    source_names = {
        entry.name for entry in source_dir.iterdir() if entry.name not in skipped
    }
    for old_entry in destination_dir.iterdir():
        if old_entry.name not in source_names:
            remove_path(old_entry)

    for source_entry in source_dir.iterdir():
        if source_entry.name in skipped:
            continue
        destination_entry = destination_dir / source_entry.name
        if source_entry.is_symlink():
            target = os.readlink(source_entry)
            if not destination_entry.is_symlink() or os.readlink(destination_entry) != target:
                remove_path(destination_entry)
                destination_entry.symlink_to(target)
            copy_owner(source_entry, destination_entry, follow_symlinks=False)
        elif source_entry.is_dir():
            sync_directory(source_entry, destination_entry)
        elif source_entry.is_file():
            source_stat = source_entry.stat()
            force_copy = source_entry.name.startswith("webmanager.sqlite3")
            unchanged = False
            if destination_entry.is_symlink():
                remove_path(destination_entry)
            if destination_entry.is_file() and not destination_entry.is_symlink():
                destination_stat = destination_entry.stat()
                unchanged = (
                    source_stat.st_size == destination_stat.st_size
                    and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
                )
            if force_copy or not unchanged:
                if destination_entry.exists() and not destination_entry.is_file():
                    remove_path(destination_entry)
                shutil.copy2(source_entry, destination_entry)
            copy_owner(source_entry, destination_entry)

    shutil.copystat(source_dir, destination_dir)
    copy_owner(source_dir, destination_dir)


destination.mkdir(parents=True, exist_ok=True)
sync_directory(source, destination, top_level=True)
PY
}

is_excluded_from_data_backup() {
    [[ " $DATA_BACKUP_EXCLUDES " == *" $1 "* ]]
}

# How much disk the data backup will take (KB), leaving out what is excluded.
data_backup_size_kb() {
    local total=0 entry kb
    for entry in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
        [[ -e $entry || -L $entry ]] || continue
        is_excluded_from_data_backup "$(basename "$entry")" && continue
        kb=$(du -sk -- "$entry" 2>/dev/null | cut -f1 || true)
        total=$((total + ${kb:-0}))
    done
    echo "$total"
}

# Put the data back the way it was before the update: what the backup holds
# returns, what the update newly created at the top level goes, and the
# excluded (never backed up, never touched) directories stay exactly as they are.
restore_data_backup() {
    local source=$1
    local destination=$2
    local entry name
    install -d "$destination"
    for entry in "$destination"/* "$destination"/.[!.]*; do
        [[ -e $entry || -L $entry ]] || continue
        name=$(basename "$entry")
        is_excluded_from_data_backup "$name" && continue
        if [[ ! -e "$source/$name" && ! -L "$source/$name" ]]; then
            rm -rf -- "$entry"
        fi
    done
    for entry in "$source"/* "$source"/.[!.]*; do
        [[ -e $entry || -L $entry ]] || continue
        name=$(basename "$entry")
        rm -rf -- "${destination:?}/$name"
        cp -a -- "$entry" "$destination/$name"
    done
}

restore_directory_contents() {
    local source=$1
    local destination=$2
    install -d "$destination"
    find "$destination" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    cp -a "$source"/. "$destination"/
}

INSTALLED_COMMIT=
NEW_COMMIT=
APPROVED_COMMIT=
UPDATE_STAGE=checking
TEST_LOG=
PIP_LOG=
INSTALL_LOG=
WORK_DIR=
FIRST_TEST_FAILURE=
FAILURE_MESSAGE=
AUTO_INSTALL=0
SERVICE_WAS_STOPPED=0
UPDATE_COMPLETED=0

# The failing tests (if any), then the end of a log: what the System page shows
# an admin who wants to know why an update was not installed.
summarize_log() {
    local log=$1
    [[ -s $log ]] || return 0
    {
        grep -E '^(FAIL|ERROR): ' "$log" | head -n 15 || true
        echo '---'
        tail -n 30 "$log"
    } | cut -c1-300 | tail -c 3500
}

remember_failure() {
    local message=$1
    local detail=${2:-}
    if [[ -n $NEW_COMMIT ]]; then
        printf '%s\n' "$message" >"$FAILURE_RECORD.message" || true
        printf '%s\n' "$detail" >"$FAILURE_RECORD.detail" || true
        printf '%s\n' "$NEW_COMMIT" >"$FAILURE_RECORD.commit" || true
    fi
}

forget_failure() {
    rm -f "$FAILURE_RECORD.commit" "$FAILURE_RECORD.message" "$FAILURE_RECORD.detail"
}

record_failure() {
    local exit_code=$?
    local message
    local detail=
    trap - ERR
    if [[ -n $APPROVED_COMMIT ]]; then
        rm -f "$REQUEST_FILE"
        case "$UPDATE_STAGE" in
            preparing_test)
                message="The approved update could not create its isolated test environment."
                ;;
            installing_dependencies)
                message="The approved update could not install its test dependencies. Check network and Python package logs."
                detail=$(summarize_log "$PIP_LOG")
                if [[ -n $FIRST_TEST_FAILURE ]]; then
                    # The real reason is the failed tests, not the retry's pip run.
                    message="The approved update failed its application tests, and a retry in a clean Python environment could not be set up (its dependencies could not be installed)."
                    detail=$FIRST_TEST_FAILURE
                fi
                ;;
            running_tests)
                message="The approved update failed its application test suite, so it was not installed."
                detail=$(summarize_log "$TEST_LOG")
                ;;
            checking_space)
                message=$FAILURE_MESSAGE
                ;;
            preflighting_install)
                message="The approved update could not start safely with a copy of the installed data. The running installation was not stopped."
                detail=$(summarize_log "$TEST_LOG")
                ;;
            verifying_install)
                message="The approved update installed, but WebManager did not pass its final health check. The previous version will be restored."
                ;;
            *)
                message="The approved update failed validation before installation. Review the updater journal."
                ;;
        esac
        remember_failure "$message" "$detail"
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" "$message" "$detail"
    else
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The GitHub update check failed. Review the updater service logs." \
            "$(summarize_log "$WORK_DIR/check.log" 2>/dev/null || true)"
    fi
    exit "$exit_code"
}
trap record_failure ERR

if [[ ! -r "$ENV_FILE" ]]; then
    write_status "disabled" "" "" "Updater configuration is missing."
    exit 0
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

REPOSITORY=${WEBMANAGER_UPDATE_REPOSITORY:-}
BRANCH=${WEBMANAGER_UPDATE_BRANCH:-main}
ENABLED=${WEBMANAGER_UPDATE_ENABLED:-1}

if [[ $ENABLED != "1" ]]; then
    write_status "disabled" "" "" "GitHub update checks are disabled."
    exit 0
fi
if [[ ! $REPOSITORY =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]; then
    write_status "error" "" "" "The configured update repository is invalid."
    exit 1
fi
if [[ ! $BRANCH =~ ^[A-Za-z0-9._/-]+$ ]] || [[ $BRANCH == -* ]] || [[ $BRANCH == *..* ]]; then
    write_status "error" "" "" "The configured update branch is invalid."
    exit 1
fi

WORK_DIR=$(mktemp -d "$STATE_DIR/check.XXXXXX")
cleanup() {
    local exit_code=$?
    set +e
    rm -rf "$WORK_DIR"
    if [[ $SERVICE_WAS_STOPPED -eq 1 && $UPDATE_COMPLETED -ne 1 ]] \
        && ! systemctl is-active --quiet webmanager; then
        echo "Updater exited while WebManager was offline; attempting emergency recovery." >&2
        systemctl reset-failed webmanager 2>/dev/null || true
        systemctl restart webmanager 2>/dev/null || true
        if ! wait_for_webmanager; then
            echo "Emergency restart did not restore WebManager. Recent logs:" >&2
            journalctl -u webmanager -n 80 --no-pager >&2 || true
        fi
    fi
    exit "$exit_code"
}
trap cleanup EXIT

SOURCE_DIR="$WORK_DIR/source"
if [[ -r "$APP_DIR/.installed-commit" ]]; then
    INSTALLED_COMMIT=$(tr -d '[:space:]' <"$APP_DIR/.installed-commit")
fi
git -c protocol.file.allow=never clone \
    --depth 200 \
    --single-branch \
    --branch "$BRANCH" \
    --no-tags \
    -- \
    "$REPOSITORY" \
    "$SOURCE_DIR" 2>&1 | tee "$WORK_DIR/check.log"

NEW_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD)

if [[ -n $INSTALLED_COMMIT && $NEW_COMMIT != "$INSTALLED_COMMIT" ]]; then
    if ! git -C "$SOURCE_DIR" cat-file -e "${INSTALLED_COMMIT}^{commit}" 2>/dev/null; then
        # The clone is shallow, and a server that has not updated for a while
        # can be further behind than it reaches. Fetch the rest before judging.
        git -c protocol.file.allow=never -C "$SOURCE_DIR" fetch --quiet --unshallow --no-tags origin \
            "$BRANCH" 2>/dev/null || true
    fi
    if ! git -C "$SOURCE_DIR" cat-file -e "${INSTALLED_COMMIT}^{commit}" 2>/dev/null; then
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The installed commit is not in $REPOSITORY's $BRANCH history, so WebManager cannot tell whether the update is safe. Install it once by hand: git pull && sudo bash setup.sh"
        exit 1
    fi
    if ! git -C "$SOURCE_DIR" merge-base --is-ancestor "$INSTALLED_COMMIT" "$NEW_COMMIT"; then
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The configured branch rewrote history; automatic installation was refused."
        exit 1
    fi
fi

if [[ ! -r $REQUEST_FILE ]]; then
    if [[ -n $INSTALLED_COMMIT && $NEW_COMMIT == "$INSTALLED_COMMIT" ]]; then
        forget_failure
        write_status "current" "$INSTALLED_COMMIT" "$NEW_COMMIT" "WebManager is current."
        exit 0
    fi
    FAILED_COMMIT=
    if [[ -r "$FAILURE_RECORD.commit" ]]; then
        FAILED_COMMIT=$(tr -d '[:space:]' <"$FAILURE_RECORD.commit")
    fi
    if [[ -n $FAILED_COMMIT && $FAILED_COMMIT != "$NEW_COMMIT" ]]; then
        forget_failure    # a newer version has replaced the one that failed
        FAILED_COMMIT=
    fi
    if [[ -n $FAILED_COMMIT ]] \
        && { [[ ! -e $AUTO_FILE ]] \
            || [[ -n $(find "$FAILURE_RECORD.commit" -mmin "-$AUTO_RETRY_MINUTES" 2>/dev/null) ]]; }; then
        # This very version was tried and failed. Keep saying so (with the
        # reason) rather than offering it as if nothing had happened.
        RETRY_HINT="Approve it again to retry."
        if [[ -e $AUTO_FILE ]]; then
            RETRY_HINT="It is not retried automatically for a few hours; use Try again to run it now."
        fi
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "$(cat "$FAILURE_RECORD.message" 2>/dev/null || true) $RETRY_HINT" \
            "$(cat "$FAILURE_RECORD.detail" 2>/dev/null || true)"
        exit 0
    fi
    if [[ ! -e $AUTO_FILE ]]; then
        write_status "available" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "An update is available and waiting for super-admin approval."
        exit 0
    fi
    # Automatic installation is on: a new version counts as approved.
    AUTO_INSTALL=1
    APPROVED_COMMIT=$NEW_COMMIT
else
    APPROVED_COMMIT=$(tr -d '[:space:]' <"$REQUEST_FILE")
    if [[ ! $APPROVED_COMMIT =~ ^[0-9a-f]{40}$ ]]; then
        rm -f "$REQUEST_FILE"
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" "The update approval was invalid."
        exit 1
    fi
    if [[ $APPROVED_COMMIT != "$NEW_COMMIT" ]]; then
        rm -f "$REQUEST_FILE"
        write_status "available" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "A newer commit appeared after approval. Review and approve the new commit."
        exit 0
    fi
fi
if [[ -n $INSTALLED_COMMIT && $NEW_COMMIT == "$INSTALLED_COMMIT" ]]; then
    rm -f "$REQUEST_FILE"
    write_status "current" "$INSTALLED_COMMIT" "$NEW_COMMIT" "WebManager is current."
    exit 0
fi

for required in \
    run.py \
    requirements.txt \
    README.md \
    webmanager \
    tests \
    deploy/debian/install.sh \
    deploy/debian/uninstall.sh \
    deploy/debian/webmanager-logrotate \
    deploy/debian/update.sh \
    deploy/debian/webmanager-update.service \
    deploy/debian/webmanager-update.timer \
    deploy/debian/webmanager-update.path; do
    if [[ ! -e "$SOURCE_DIR/$required" ]]; then
        rm -f "$REQUEST_FILE"
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The approved update is missing required application files."
        exit 1
    fi
done

write_status "testing" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Testing the super-admin-approved update."
TEST_PYTHON=
TEST_LOG="$WORK_DIR/application-tests.log"
PIP_LOG="$WORK_DIR/pip.log"
run_application_tests() {
    local python=$1
    local name
    local clean_environment=()
    # The tests must see a blank slate, not this server's own settings (the
    # service's environment file exports WEBMANAGER_* values to this script).
    while IFS= read -r name; do
        clean_environment+=(-u "$name")
    done < <(compgen -e | grep '^WEBMANAGER_' || true)
    env ${clean_environment[@]+"${clean_environment[@]}"} "$python" -m unittest discover \
        -s "$SOURCE_DIR/tests" \
        -t "$SOURCE_DIR" \
        -v 2>&1 | tee "$TEST_LOG"
}
create_test_environment() {
    UPDATE_STAGE=preparing_test
    rm -rf "$WORK_DIR/check-venv"
    python3 -m venv "$WORK_DIR/check-venv"
    TEST_PYTHON="$WORK_DIR/check-venv/bin/python"
    UPDATE_STAGE=installing_dependencies
    "$TEST_PYTHON" -m pip install \
        --disable-pip-version-check \
        --retries 5 \
        --timeout 30 \
        -q \
        -r "$SOURCE_DIR/requirements.txt" 2>&1 | tee "$PIP_LOG"
}

if [[ -x "$APP_DIR/.venv/bin/python" ]] \
    && [[ -r "$APP_DIR/requirements.txt" ]] \
    && cmp -s "$APP_DIR/requirements.txt" "$SOURCE_DIR/requirements.txt"; then
    TEST_PYTHON="$APP_DIR/.venv/bin/python"
    echo "Reusing installed Python dependencies because requirements are unchanged."
    UPDATE_STAGE=running_tests
    if ! run_application_tests "$TEST_PYTHON"; then
        # Keep why it failed: if the clean environment cannot even be built,
        # this (not the pip error) is what the admin needs to see.
        FIRST_TEST_FAILURE=$(summarize_log "$TEST_LOG")
        echo "Tests failed with installed dependencies; retrying in a clean environment."
        create_test_environment
        UPDATE_STAGE=running_tests
        run_application_tests "$TEST_PYTHON"
    fi
else
    create_test_environment
    UPDATE_STAGE=running_tests
    run_application_tests "$TEST_PYTHON"
fi

UPDATE_STAGE=preflighting_install
write_status "testing" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Testing the approved update with a safe copy of installed data."
PREFLIGHT_DIR="$WORK_DIR/preflight"
PREFLIGHT_DATABASE="$PREFLIGHT_DIR/webmanager.sqlite3"
mkdir -p "$PREFLIGHT_DIR"
TEST_LOG="$WORK_DIR/preflight.log"
if [[ -f "$DATA_DIR/webmanager.sqlite3" ]]; then
    "$TEST_PYTHON" - "$DATA_DIR/webmanager.sqlite3" "$PREFLIGHT_DATABASE" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
fi
"$TEST_PYTHON" - \
    "$SOURCE_DIR" \
    "$PREFLIGHT_DATABASE" \
    "$PREFLIGHT_DIR" \
    "$APP_ENV_FILE" >"$TEST_LOG" 2>&1 <<'PY'
import os
import sys
from pathlib import Path

source_dir, database_path, preflight_dir, env_path = sys.argv[1:]
sys.path.insert(0, source_dir)

settings = {}
try:
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key] = value
except OSError:
    pass

root = Path(preflight_dir)
from webmanager import create_app

app = create_app(
    {
        "TESTING": True,
        "SECRET_KEY": "updater-preflight-only",
        "DATABASE": database_path,
        "REPOSITORY_ROOT": str(root / "repositories"),
        "NGINX_ROOT": str(root / "nginx"),
        "LOG_ROOT": str(root / "logs"),
        "NGINX_BINARY": str(root / "nginx-disabled"),
        "SITE_GATEWAY_PORT": int(
            settings.get("WEBMANAGER_SITE_GATEWAY_PORT", "8090")
        ),
        "SITE_BASE_DOMAIN": settings.get("WEBMANAGER_SITE_BASE_DOMAIN", ""),
        "SITE_PUBLIC_SCHEME": settings.get(
            "WEBMANAGER_SITE_PUBLIC_SCHEME", "http"
        ),
        "GOOGLE_REDIRECT_URI": settings.get(
            "WEBMANAGER_GOOGLE_REDIRECT_URI", ""
        ),
        "AUTO_REFRESH_ENABLED": False,
        "PROGRAM_UPDATE_STATUS_FILE": str(root / "status.json"),
        "PROGRAM_UPDATE_REQUEST_FILE": str(root / "install.commit"),
        "PROGRAM_UPDATE_CHECK_REQUEST_FILE": str(root / "check"),
    }
)
with app.app_context():
    app.extensions["runtime_manager"].sync_nginx_configs()
response = app.test_client().get("/healthz")
if response.status_code != 200 or response.get_json() != {"status": "ok"}:
    raise SystemExit(
        f"Candidate health check returned {response.status_code}: "
        f"{response.get_data(as_text=True)[:500]}"
    )
print("Candidate data migration, Nginx generation, and health check passed.")
PY
cat "$TEST_LOG"

UPDATE_STAGE=checking_space
# Older backups go first (the two newest stay, so three with this one), then
# make sure this one and the installation itself will fit before touching anything.
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | tail -n +3 \
    | cut -d' ' -f2- \
    | xargs -r rm -rf
needed_kb=$(( $(du -sk "$APP_DIR" | cut -f1) + $(du -sk "$CONFIG_DIR" | cut -f1) + $(data_backup_size_kb) ))
needed_kb=$(( needed_kb * 12 / 10 + 262144 ))
available_kb=$(df -Pk "$BACKUP_ROOT" | awk 'NR == 2 {print $4}')
if (( available_kb < needed_kb )); then
    FAILURE_MESSAGE="Not enough free disk space to back WebManager up before updating (about $((needed_kb / 1024)) MB needed, $((available_kb / 1024)) MB free for $BACKUP_ROOT). Free some space and approve the update again. Nothing was changed."
    false
fi

UPDATE_STAGE=backing_up
BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-${INSTALLED_COMMIT:-unknown}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
cp -a "$APP_DIR" "$BACKUP_DIR/app"
cp -a "$CONFIG_DIR" "$BACKUP_DIR/config"
if [[ -f $SERVICE_FILE ]]; then
    cp -a "$SERVICE_FILE" "$BACKUP_DIR/webmanager.service"
fi
for nginx_file in "$NGINX_AVAILABLE" "$SITE_NGINX_AVAILABLE"; do
    if [[ -f $nginx_file ]]; then
        cp -a "$nginx_file" "$BACKUP_DIR/$(basename "$nginx_file").nginx"
    fi
done
for updater_file in \
    /usr/local/sbin/webmanager-update \
    /usr/local/sbin/webmanager-uninstall \
    /etc/logrotate.d/webmanager \
    /etc/systemd/system/webmanager-update.service \
    /etc/systemd/system/webmanager-update.timer \
    /etc/systemd/system/webmanager-update.path; do
    if [[ -f $updater_file ]]; then
        backup_name=$(basename "$updater_file")
        if [[ $updater_file == /etc/logrotate.d/webmanager ]]; then
            backup_name=webmanager-logrotate
        fi
        cp -a "$updater_file" "$BACKUP_DIR/$backup_name"
    fi
done

write_status "backing_up" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Preparing the data backup while WebManager remains online."
DATA_BACKUP_DIR="$BACKUP_DIR/data"
sync_data_backup "$DATA_BACKUP_DIR"

DATA_BACKUP_COMPLETE=0
rollback() {
    local exit_code=$?
    local message
    trap - ERR
    set +e
    echo "Update failed; restoring the previous working state." >&2
    systemctl stop webmanager 2>/dev/null || true
    if ! restore_directory_contents "$BACKUP_DIR/app" "$APP_DIR"; then
        echo "Could not restore the application directory." >&2
        message="Update failed and the application directory could not be restored."
    fi
    if [[ $DATA_BACKUP_COMPLETE -eq 1 ]]; then
        if restore_data_backup "$DATA_BACKUP_DIR" "$DATA_DIR"; then
            message=${message:-"Update failed. Application and persistent data were restored."}
        else
            echo "Could not restore the persistent data directory." >&2
            message="Update failed and persistent data could not be restored automatically."
        fi
    else
        message="Update stopped before a complete data backup was created. Existing data was left untouched."
    fi
    if ! restore_directory_contents "$BACKUP_DIR/config" "$CONFIG_DIR"; then
        echo "Could not restore the configuration directory." >&2
        message="$message Configuration restoration also failed."
    fi
    if [[ -f "$BACKUP_DIR/webmanager.service" ]]; then
        cp -a "$BACKUP_DIR/webmanager.service" "$SERVICE_FILE"
    fi
    if [[ -f "$BACKUP_DIR/webmanager.nginx" ]]; then
        cp -a "$BACKUP_DIR/webmanager.nginx" "$NGINX_AVAILABLE" || true
    fi
    if [[ -f "$BACKUP_DIR/webmanager-sites.nginx" ]]; then
        cp -a "$BACKUP_DIR/webmanager-sites.nginx" "$SITE_NGINX_AVAILABLE" || true
    fi
    for name in webmanager-update webmanager-uninstall webmanager-logrotate webmanager-update.service webmanager-update.timer webmanager-update.path; do
        if [[ -f "$BACKUP_DIR/$name" ]]; then
            case "$name" in
                webmanager-update)
                    cp -a "$BACKUP_DIR/$name" /usr/local/sbin/webmanager-update
                    ;;
                webmanager-uninstall)
                    cp -a "$BACKUP_DIR/$name" /usr/local/sbin/webmanager-uninstall
                    ;;
                webmanager-logrotate)
                    cp -a "$BACKUP_DIR/$name" /etc/logrotate.d/webmanager
                    ;;
                *)
                    cp -a "$BACKUP_DIR/$name" "/etc/systemd/system/$name"
                    ;;
            esac
        fi
    done
    systemctl daemon-reload
    if command -v nginx >/dev/null 2>&1 && nginx -t; then
        systemctl reload nginx 2>/dev/null || true
    fi
    systemctl reset-failed webmanager 2>/dev/null || true
    systemctl restart webmanager 2>/dev/null || true
    if ! wait_for_webmanager; then
        echo "Rollback restored the files, but WebManager did not become healthy." >&2
        journalctl -u webmanager -n 80 --no-pager >&2 || true
        message="$message WebManager did not restart successfully; inspect journalctl -u webmanager."
    fi
    rm -f "$REQUEST_FILE" "$NOTES_FILE"
    remember_failure "$message" "$(summarize_log "$INSTALL_LOG")"
    write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" "$message" "$(summarize_log "$INSTALL_LOG")"
    exit "$exit_code"
}
trap rollback ERR

write_status "backing_up" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Pausing WebManager briefly to finalize the prepared data backup."
systemctl stop webmanager
SERVICE_WAS_STOPPED=1
sync_data_backup "$DATA_BACKUP_DIR"
DATA_BACKUP_COMPLETE=1

write_status "installing" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Installing the approved update."
INSTALL_LOG="$WORK_DIR/install.log"
rm -f "$NOTES_FILE"
WEBMANAGER_UPDATE_REPOSITORY="$REPOSITORY" \
WEBMANAGER_UPDATE_BRANCH="$BRANCH" \
WEBMANAGER_INSTALL_NOTES_FILE="$NOTES_FILE" \
    bash "$SOURCE_DIR/deploy/debian/install.sh" --self-update 2>&1 | tee "$INSTALL_LOG"

UPDATE_STAGE=verifying_install
write_status "installing" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Verifying the restarted WebManager service."
if ! wait_for_webmanager; then
    journalctl -u webmanager -n 80 --no-pager >&2 || true
    false
fi

printf '%s\n' "$NEW_COMMIT" >"$APP_DIR/.installed-commit"
chmod 0644 "$APP_DIR/.installed-commit"
rm -f "$REQUEST_FILE"
forget_failure
UPDATE_COMPLETED=1
trap - ERR
SUCCESS_MESSAGE="The approved update was installed successfully. Persistent data was preserved."
if [[ $AUTO_INSTALL -eq 1 ]]; then
    SUCCESS_MESSAGE="The update was installed automatically. Persistent data was preserved."
fi
INSTALL_NOTES=
if [[ -r $NOTES_FILE ]]; then
    INSTALL_NOTES=$(tr '\n' ' ' <"$NOTES_FILE")
fi
rm -f "$NOTES_FILE"
if [[ -n ${INSTALL_NOTES// /} ]]; then
    SUCCESS_MESSAGE="$SUCCESS_MESSAGE Note: ${INSTALL_NOTES% }"
fi
write_status "current" "$NEW_COMMIT" "$NEW_COMMIT" "$SUCCESS_MESSAGE"

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | tail -n +4 \
    | cut -d' ' -f2- \
    | xargs -r rm -rf
