#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR=/opt/webmanager
DATA_DIR=/var/lib/webmanager
CONFIG_DIR=/etc/webmanager
SERVICE_FILE=/etc/systemd/system/webmanager.service
NGINX_AVAILABLE=/etc/nginx/sites-available/webmanager
NGINX_ENABLED=/etc/nginx/sites-enabled/webmanager
SITE_NGINX_AVAILABLE=/etc/nginx/sites-available/webmanager-sites
SITE_NGINX_ENABLED=/etc/nginx/sites-enabled/webmanager-sites
UPDATER_SCRIPT=/usr/local/sbin/webmanager-update
UPDATER_SERVICE=/etc/systemd/system/webmanager-update.service
UPDATER_TIMER=/etc/systemd/system/webmanager-update.timer
UPDATER_PATH=/etc/systemd/system/webmanager-update.path
UPDATER_STATE=/var/lib/webmanager-updater
PURGE_DATA=0

if [[ ${1:-} == "--purge" ]]; then
    PURGE_DATA=1
elif [[ $# -gt 0 ]]; then
    echo "Usage: sudo $0 [--purge]" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Run this uninstaller as root." >&2
    exit 1
fi

systemctl disable --now webmanager 2>/dev/null || true
systemctl disable --now webmanager-update.timer 2>/dev/null || true
systemctl disable --now webmanager-update.path 2>/dev/null || true
rm -f "$SERVICE_FILE" "$UPDATER_SERVICE" "$UPDATER_TIMER" "$UPDATER_PATH" "$UPDATER_SCRIPT"
systemctl daemon-reload

rm -f "$NGINX_ENABLED" "$NGINX_AVAILABLE" "$SITE_NGINX_ENABLED" "$SITE_NGINX_AVAILABLE"
if command -v nginx >/dev/null 2>&1 && nginx -t; then
    systemctl reload nginx 2>/dev/null || true
fi

rm -rf "$APP_DIR"
rm -rf "$CONFIG_DIR"

if [[ $PURGE_DATA -eq 1 ]]; then
    rm -rf "$DATA_DIR"
    rm -rf "$UPDATER_STATE"
    deluser webmanager 2>/dev/null || true
    delgroup webmanager 2>/dev/null || true
    echo "WebManager and all stored data were removed."
else
    echo "WebManager was removed. Stored data remains in $DATA_DIR."
    echo "Updater rollback files remain in $UPDATER_STATE."
    echo "Run with --purge to remove data and the service account too."
fi
