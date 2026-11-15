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
# Check with the same extra groups the service has (app hosting adds "docker"),
# or the container check would fail here while working in the service.
GROUP_ARGS=()
for group in $(systemctl show webmanager -p SupplementaryGroups --value 2>/dev/null || true); do
    GROUP_ARGS+=(-G "$group")
done
if [[ ${#GROUP_ARGS[@]} -gt 0 ]]; then
    GROUP_ARGS=(-g webmanager "${GROUP_ARGS[@]}")
fi
exec runuser -u webmanager ${GROUP_ARGS[@]+"${GROUP_ARGS[@]}"} --preserve-environment -- \
    /opt/webmanager/.venv/bin/python /opt/webmanager/run.py doctor
