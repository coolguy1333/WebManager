#!/usr/bin/env bash
# sudo webmanager-restore BACKUP.tar.gz [--with-settings]
#
# Puts a WebManager backup (System page > Backups, or <data>/backups) back:
# the database and the keys that protect it. Git sources are cloned again when
# WebManager starts. The database being replaced is kept next to the new one
# as webmanager.sqlite3.before-restore.
#
# --with-settings also replaces /etc/webmanager/webmanager.env with the one in
# the backup (the old file is kept as webmanager.env.before-restore). Use it
# when rebuilding a server from nothing; leave it out when the settings on
# this server are already right.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

BACKUP=
WITH_SETTINGS=0
for argument in "$@"; do
    case $argument in
        --with-settings) WITH_SETTINGS=1 ;;
        -h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "Unknown option: $argument" >&2; exit 2 ;;
        *) BACKUP=$argument ;;
    esac
done
if [[ -z $BACKUP || ! -f $BACKUP ]]; then
    echo "Usage: sudo webmanager-restore BACKUP.tar.gz [--with-settings]" >&2
    echo "Backups made on this server are in /var/lib/webmanager/backups:" >&2
    ls -1t /var/lib/webmanager/backups/webmanager-*.tar.gz 2>/dev/null | head -n 5 >&2 || true
    exit 2
fi

ENV_FILE=/etc/webmanager/webmanager.env
DATA_DIR=$(sed -n 's/^WEBMANAGER_DATA_DIR=//p' "$ENV_FILE" | tail -n 1)
DATA_DIR=${DATA_DIR:-/var/lib/webmanager}

# The service user must be able to read the file wherever it was copied to.
STAGED_DIR=$(mktemp -d "$DATA_DIR/.restore.XXXXXX")
trap 'rm -rf "$STAGED_DIR"' EXIT
STAGED=$STAGED_DIR/$(basename -- "$BACKUP")
cp -- "$BACKUP" "$STAGED"
chown -R webmanager:webmanager "$STAGED_DIR"
chmod 0700 "$STAGED_DIR"
chmod 0600 "$STAGED"

echo "Stopping WebManager"
systemctl stop webmanager
if ! WEBMANAGER_DATA_DIR="$DATA_DIR" HOME="$DATA_DIR" \
    runuser -u webmanager --preserve-environment -- \
    /opt/webmanager/.venv/bin/python /opt/webmanager/run.py restore "$STAGED"; then
    echo "Nothing was changed. Starting WebManager again." >&2
    systemctl start webmanager
    exit 1
fi

RESTORED_SETTINGS=$DATA_DIR/restored-settings.env
if [[ -f $RESTORED_SETTINGS ]]; then
    if [[ $WITH_SETTINGS -eq 1 ]]; then
        cp -a "$ENV_FILE" "$ENV_FILE.before-restore"
        install -o root -g webmanager -m 0640 "$RESTORED_SETTINGS" "$ENV_FILE"
        rm -f "$RESTORED_SETTINGS"
        echo "Settings replaced (the old file is $ENV_FILE.before-restore)."
    else
        echo "This server's settings were left as they are. To use the backup's, run again with --with-settings."
        rm -f "$RESTORED_SETTINGS"
    fi
fi

echo "Starting WebManager"
systemctl start webmanager
echo "Done. Sites come back as their Git sources are cloned again; check with: sudo webmanager-doctor"
