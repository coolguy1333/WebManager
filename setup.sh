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

ENV_FILE=/etc/webmanager/webmanager.env
SITE_BASE_DOMAIN=$(sed -n 's/^WEBMANAGER_SITE_BASE_DOMAIN=//p' "$ENV_FILE" | tail -n 1)
if [[ -z $SITE_BASE_DOMAIN ]]; then
    if [[ ! -t 0 ]]; then
        echo "An initial deployment domain is required." >&2
        echo "Run setup interactively: bash setup.sh" >&2
        exit 1
    fi

    echo
    echo "Deployment domain setup"
    echo "======================="
    echo
    echo "Enter the base domain used for hosted sites."
    echo "For example, mhsit.club creates site addresses such as project.mhsit.club."
    while true; do
        read -r -p "Initial deployment domain: " SITE_BASE_DOMAIN
        if SITE_BASE_DOMAIN=$(python3 - "$SITE_BASE_DOMAIN" <<'PY'
import re
import sys

value = sys.argv[1].strip().lower().rstrip(".")
pattern = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
if not pattern.fullmatch(value):
    raise SystemExit("Enter a valid domain such as mhsit.club.")
print(value)
PY
        ); then
            break
        fi
    done

    DATA_DIR=$(sed -n 's/^WEBMANAGER_DATA_DIR=//p' "$ENV_FILE" | tail -n 1)
    DATA_DIR=${DATA_DIR:-/var/lib/webmanager}
    DOMAIN_READY=0
    for _ in {1..30}; do
        if runuser -u webmanager -- python3 - "$DATA_DIR/webmanager.sqlite3" "$SITE_BASE_DOMAIN" <<'PY'
import sqlite3
import sys

database = sqlite3.connect(sys.argv[1])
try:
    blocked = {
        row[0]
        for row in database.execute("SELECT name FROM blocked_domains").fetchall()
    }
    domain = sys.argv[2]
    if any(domain == denied or domain.endswith(f".{denied}") for denied in blocked):
        print(
            f"{domain} is blocked by the deployment-domain blocklist.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    database.execute("BEGIN IMMEDIATE")
    database.execute("UPDATE domains SET is_default = 0")
    database.execute(
        """
        INSERT INTO domains (name, is_default)
        VALUES (?, 1)
        ON CONFLICT(name) DO UPDATE SET is_default = 1
        """,
        (domain,),
    )
    database.commit()
except sqlite3.OperationalError:
    database.rollback()
    raise SystemExit(1)
finally:
    database.close()
PY
        then
            DOMAIN_READY=1
            break
        else
            DOMAIN_STATUS=$?
            if [[ $DOMAIN_STATUS -eq 2 ]]; then
                exit 1
            fi
        fi
        sleep 1
    done
    if [[ $DOMAIN_READY -ne 1 ]]; then
        echo "The initial deployment domain was not created in the database." >&2
        journalctl -u webmanager -n 60 --no-pager >&2
        exit 1
    fi

    TEMP_ENV=$(mktemp)
    grep -v '^WEBMANAGER_SITE_BASE_DOMAIN=' "$ENV_FILE" >"$TEMP_ENV" || true
    printf 'WEBMANAGER_SITE_BASE_DOMAIN=%s\n' "$SITE_BASE_DOMAIN" >>"$TEMP_ENV"
    install -o root -g webmanager -m 0640 "$TEMP_ENV" "$ENV_FILE"
    rm -f "$TEMP_ENV"
    systemctl restart webmanager
    if ! systemctl is-active --quiet webmanager; then
        echo "WebManager failed to restart after adding the deployment domain." >&2
        journalctl -u webmanager -n 60 --no-pager >&2
        exit 1
    fi
    echo "Default deployment domain added: $SITE_BASE_DOMAIN"
else
    echo "Keeping existing deployment domain: $SITE_BASE_DOMAIN"
fi

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
