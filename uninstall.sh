#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ $EUID -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "Install sudo or run this script as root." >&2
        exit 1
    fi
    exec sudo bash "$SCRIPT_DIR/uninstall.sh" "$@"
fi

exec bash "$SCRIPT_DIR/deploy/debian/uninstall.sh" "$@"

