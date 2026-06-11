#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/webmanager
DATA_DIR=/var/lib/webmanager
CONFIG_DIR=/etc/webmanager
STATE_DIR=/var/lib/webmanager-updater
STATUS_FILE=$STATE_DIR/status.json
REQUEST_FILE=$STATE_DIR/requests/install.commit
BACKUP_ROOT=$STATE_DIR/backups
LOCK_FILE=/run/lock/webmanager-update.lock
SERVICE_FILE=/etc/systemd/system/webmanager.service
ENV_FILE=/etc/webmanager/updater.env

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

write_status() {
    local state=$1
    local installed=${2:-}
    local available=${3:-}
    local message=${4:-}
    local temporary
    temporary=$(mktemp "$STATE_DIR/status.XXXXXX")
    python3 - "$temporary" "$state" "$installed" "$available" "$message" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, state, installed, available, message = sys.argv[1:]
payload = {
    "state": state,
    "installed_commit": installed or None,
    "available_commit": available or None,
    "update_available": bool(available and available != installed),
    "message": message,
    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
    handle.write("\n")
os.chmod(path, 0o640)
PY
    chown root:webmanager "$temporary"
    mv -f "$temporary" "$STATUS_FILE"
}

INSTALLED_COMMIT=
NEW_COMMIT=
APPROVED_COMMIT=
record_failure() {
    local exit_code=$?
    trap - ERR
    if [[ -n $APPROVED_COMMIT ]]; then
        rm -f "$REQUEST_FILE"
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The approved update failed validation or testing and was not installed."
    else
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The GitHub update check failed. Review the updater service logs."
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
    rm -rf "$WORK_DIR"
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
    "$SOURCE_DIR"

NEW_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD)

if [[ -n $INSTALLED_COMMIT && $NEW_COMMIT != "$INSTALLED_COMMIT" ]]; then
    if ! git -C "$SOURCE_DIR" cat-file -e "${INSTALLED_COMMIT}^{commit}" 2>/dev/null; then
        write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "The installed commit is not in fetched history; manual review is required."
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
        write_status "current" "$INSTALLED_COMMIT" "$NEW_COMMIT" "WebManager is current."
    else
        write_status "available" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
            "An update is available and waiting for super-admin approval."
    fi
    exit 0
fi

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
python3 -m venv "$WORK_DIR/check-venv"
"$WORK_DIR/check-venv/bin/python" -m pip install \
    --disable-pip-version-check \
    -q \
    -r "$SOURCE_DIR/requirements.txt"
"$WORK_DIR/check-venv/bin/python" -m unittest discover \
    -s "$SOURCE_DIR/tests" \
    -t "$SOURCE_DIR" \
    -v

BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-${INSTALLED_COMMIT:-unknown}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
cp -a "$APP_DIR" "$BACKUP_DIR/app"
cp -a "$CONFIG_DIR" "$BACKUP_DIR/config"
if [[ -f $SERVICE_FILE ]]; then
    cp -a "$SERVICE_FILE" "$BACKUP_DIR/webmanager.service"
fi
for updater_file in \
    /usr/local/sbin/webmanager-update \
    /etc/systemd/system/webmanager-update.service \
    /etc/systemd/system/webmanager-update.timer \
    /etc/systemd/system/webmanager-update.path; do
    if [[ -f $updater_file ]]; then
        cp -a "$updater_file" "$BACKUP_DIR/$(basename "$updater_file")"
    fi
done

DATA_BACKUP_COMPLETE=0
rollback() {
    local exit_code=$?
    local message
    trap - ERR
    echo "Update failed; restoring the previous working state." >&2
    systemctl stop webmanager 2>/dev/null || true
    rm -rf "$APP_DIR"
    cp -a "$BACKUP_DIR/app" "$APP_DIR"
    if [[ $DATA_BACKUP_COMPLETE -eq 1 ]]; then
        rm -rf "$DATA_DIR"
        tar -C /var/lib -xzf "$BACKUP_DIR/webmanager-data.tar.gz"
        message="Update failed. Application and persistent data were restored."
    else
        message="Update stopped before a complete data backup was created. Existing data was left untouched."
    fi
    rm -rf "$CONFIG_DIR"
    cp -a "$BACKUP_DIR/config" "$CONFIG_DIR"
    if [[ -f "$BACKUP_DIR/webmanager.service" ]]; then
        cp -a "$BACKUP_DIR/webmanager.service" "$SERVICE_FILE"
    fi
    for name in webmanager-update webmanager-update.service webmanager-update.timer webmanager-update.path; do
        if [[ -f "$BACKUP_DIR/$name" ]]; then
            case "$name" in
                webmanager-update)
                    cp -a "$BACKUP_DIR/$name" /usr/local/sbin/webmanager-update
                    ;;
                *)
                    cp -a "$BACKUP_DIR/$name" "/etc/systemd/system/$name"
                    ;;
            esac
        fi
    done
    systemctl daemon-reload
    systemctl restart webmanager
    rm -f "$REQUEST_FILE"
    write_status "error" "$INSTALLED_COMMIT" "$NEW_COMMIT" "$message"
    exit "$exit_code"
}
trap rollback ERR

write_status "backing_up" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Stopping WebManager and backing up all persistent data."
systemctl stop webmanager
tar -C /var/lib -czf "$BACKUP_DIR/webmanager-data.tar.gz" webmanager
DATA_BACKUP_COMPLETE=1

write_status "installing" "$INSTALLED_COMMIT" "$NEW_COMMIT" \
    "Installing the approved update."
WEBMANAGER_UPDATE_REPOSITORY="$REPOSITORY" \
WEBMANAGER_UPDATE_BRANCH="$BRANCH" \
    bash "$SOURCE_DIR/deploy/debian/install.sh" --self-update

printf '%s\n' "$NEW_COMMIT" >"$APP_DIR/.installed-commit"
chmod 0644 "$APP_DIR/.installed-commit"
rm -f "$REQUEST_FILE"
trap - ERR
write_status "current" "$NEW_COMMIT" "$NEW_COMMIT" \
    "The approved update was installed successfully. Persistent data was preserved."

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr \
    | tail -n +4 \
    | cut -d' ' -f2- \
    | xargs -r rm -rf
