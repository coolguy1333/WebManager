#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

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
    && ! git -C "$SCRIPT_DIR" remote get-url origin >/dev/null 2>&1 \
    && [[ -t 0 ]]; then
    echo
    echo "Automatic update checks need this project's HTTPS GitHub URL."
    echo "Example: https://github.com/OWNER/WebManager.git"
    read -r -p "GitHub repository URL (blank to disable update checks): " UPDATE_REPOSITORY
    if [[ -n $UPDATE_REPOSITORY ]]; then
        read -r -p "Update branch [main]: " UPDATE_BRANCH
        UPDATE_BRANCH=${UPDATE_BRANCH:-main}
        set -- "$@" \
            --update-repository "$UPDATE_REPOSITORY" \
            --update-branch "$UPDATE_BRANCH"
    fi
fi

bash "$SCRIPT_DIR/deploy/debian/install.sh" "$@"

if ! grep -Eq '^WEBMANAGER_GOOGLE_CLIENT_ID=.+$' /etc/webmanager/webmanager.env 2>/dev/null; then
    echo
    echo "Google sign-in still needs a Google OAuth client and a public HTTPS URL."
    if [[ -t 0 ]]; then
        read -r -p "Configure Google sign-in now? [Y/n]: " answer
        case ${answer:-Y} in
            [Yy]*)
                bash "$SCRIPT_DIR/configure-google.sh"
                ;;
            *)
                echo "Later, run: bash configure-google.sh"
                ;;
        esac
    else
        echo "Run from the project folder: bash configure-google.sh"
    fi
fi
