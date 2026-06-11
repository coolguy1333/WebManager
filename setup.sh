#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_UPDATE_REPOSITORY=https://github.com/coolguy1333/WebManager.git
DEFAULT_UPDATE_BRANCH=main

if [[ ! -r /etc/os-release ]]; then
    echo "This installer requires Debian Linux." >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != "debian" ]]; then
    echo "This installer supports Debian 12 or newer." >&2
    exit 1
fi

DEBIAN_VERSION=${VERSION_ID%%.*}
if [[ ! $DEBIAN_VERSION =~ ^[0-9]+$ ]] || (( DEBIAN_VERSION < 12 )); then
    echo "Debian 12 or newer is required." >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd is required." >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "Install sudo or run: su -c 'bash setup.sh'" >&2
        exit 1
    fi
    exec sudo bash "$SCRIPT_DIR/setup.sh" "$@"
fi

HAS_UPDATE_REPOSITORY=0
for argument in "$@"; do
    if [[ $argument == "--update-repository" ]]; then
        HAS_UPDATE_REPOSITORY=1
        break
    fi
done

if [[ $HAS_UPDATE_REPOSITORY -eq 0 ]] \
    && [[ ! -f /etc/webmanager/updater.env ]] \
    && ! git -C "$SCRIPT_DIR" remote get-url origin >/dev/null 2>&1; then
    set -- "$@" \
        --update-repository "$DEFAULT_UPDATE_REPOSITORY" \
        --update-branch "$DEFAULT_UPDATE_BRANCH"
fi

bash "$SCRIPT_DIR/deploy/debian/install.sh" "$@"

GOOGLE_CONFIG_COMPLETE=1
for key in \
    WEBMANAGER_GOOGLE_CLIENT_ID \
    WEBMANAGER_GOOGLE_CLIENT_SECRET \
    WEBMANAGER_GOOGLE_REDIRECT_URI; do
    if ! grep -Eq "^${key}=.+$" /etc/webmanager/webmanager.env 2>/dev/null; then
        GOOGLE_CONFIG_COMPLETE=0
        break
    fi
done

if [[ $GOOGLE_CONFIG_COMPLETE -eq 0 ]]; then
    echo
    echo "Google sign-in setup is required."
    echo "No Google credentials are preset; enter them securely during this step."
    if [[ -t 0 ]]; then
        bash "$SCRIPT_DIR/configure-google.sh"
    else
        echo "Interactive input is unavailable." >&2
        echo "Run from the project folder: bash configure-google.sh" >&2
    fi
else
    echo "Keeping the existing Google sign-in configuration."
fi
