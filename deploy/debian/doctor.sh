#!/usr/bin/env bash
# sudo webmanager-doctor: check that everything WebManager depends on works,
# and say what to do about anything that doesn't. Works even when the web page
# or sign-in is broken.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

set -a
# shellcheck disable=SC1091
source /etc/webmanager/webmanager.env
set +a
export HOME=/var/lib/webmanager

echo "Service: $(systemctl is-active webmanager 2>/dev/null || true)"
cd /opt/webmanager
exec runuser -u webmanager --preserve-environment -- /opt/webmanager/.venv/bin/python /opt/webmanager/run.py doctor
