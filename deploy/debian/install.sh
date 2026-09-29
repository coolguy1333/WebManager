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
UNINSTALL_COMMAND=/usr/local/sbin/webmanager-uninstall
LOGROTATE_FILE=/etc/logrotate.d/webmanager
UPDATER_ENV=/etc/webmanager/updater.env
UPDATER_STATE=/var/lib/webmanager-updater
UPDATER_STATUS=$UPDATER_STATE/status.json
DEFAULT_UPDATE_REPOSITORY=https://github.com/coolguy1333/WebManager.git
SELF_UPDATE=0
EXISTING_INSTALL=0
if [[ -e $SERVICE_FILE || -d $APP_DIR ]]; then
    EXISTING_INSTALL=1
fi
REPLICA_OF=
PEER_TOKEN=
PEERS=
SKIP_PRIMARY_CHECK=0
AUTO_UPDATE=keep
ANNOUNCE=0
ANNOUNCE_URL=
UPDATE_REPOSITORY=${WEBMANAGER_UPDATE_REPOSITORY:-}
UPDATE_BRANCH=${WEBMANAGER_UPDATE_BRANCH:-}
UPDATE_CONFIGURATION_EXPLICIT=0
if [[ ${WEBMANAGER_UPDATE_REPOSITORY+x} == x ]] \
    || [[ ${WEBMANAGER_UPDATE_BRANCH+x} == x ]]; then
    UPDATE_CONFIGURATION_EXPLICIT=1
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --self-update)
            SELF_UPDATE=1
            ;;
        --update-repository)
            shift
            if [[ $# -eq 0 ]]; then
                echo "--update-repository requires a GitHub URL." >&2
                exit 1
            fi
            UPDATE_REPOSITORY=$1
            UPDATE_CONFIGURATION_EXPLICIT=1
            ;;
        --update-branch)
            shift
            if [[ $# -eq 0 ]]; then
                echo "--update-branch requires a branch name." >&2
                exit 1
            fi
            UPDATE_BRANCH=$1
            UPDATE_CONFIGURATION_EXPLICIT=1
            ;;
        --replica-of)
            shift
            [[ $# -gt 0 ]] || { echo "--replica-of requires the primary's dashboard URL." >&2; exit 1; }
            REPLICA_OF=${1%/}
            ;;
        --peer-token)
            shift
            [[ $# -gt 0 ]] || { echo "--peer-token requires the shared token." >&2; exit 1; }
            PEER_TOKEN=$1
            ;;
        --skip-primary-check)
            SKIP_PRIMARY_CHECK=1
            ;;
        --announce)
            ANNOUNCE=1
            ;;
        --announce-url)
            shift
            [[ $# -gt 0 ]] || { echo "--announce-url requires this server's address." >&2; exit 1; }
            ANNOUNCE=1
            ANNOUNCE_URL=${1%/}
            ;;
        --auto-update)
            AUTO_UPDATE=on
            ;;
        --no-auto-update)
            AUTO_UPDATE=off
            ;;
        --peers)
            shift
            [[ $# -gt 0 ]] || { echo "--peers requires comma-separated server URLs." >&2; exit 1; }
            PEERS=$1
            ;;
        *)
            echo "Unknown installer option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ $SELF_UPDATE -eq 0 ]]; then
    exec 8>/run/lock/webmanager-update.lock
    flock 8
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_DIR=$(cd -- "$SCRIPT_DIR/../.." && pwd)

if [[ $EUID -ne 0 ]]; then
    echo "Run setup from the project root with: bash setup.sh" >&2
    exit 1
fi

for required in \
    run.py \
    requirements.txt \
    README.md \
    configure-google.sh \
    webmanager \
    deploy/debian/uninstall.sh \
    deploy/debian/webmanager-logrotate \
    deploy/debian/update.sh \
    deploy/debian/webmanager-update.service \
    deploy/debian/webmanager-update.timer \
    deploy/debian/webmanager-update.path \
    deploy/debian/nginx-sites.conf; do
    if [[ ! -e "$SOURCE_DIR/$required" ]]; then
        echo "Missing source item: $SOURCE_DIR/$required" >&2
        exit 1
    fi
done

if [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
    echo "Run the installer from a source checkout outside $APP_DIR." >&2
    exit 1
fi

# Something the admin should know about an otherwise successful install. An
# updater-driven update passes a file that the System page reads back.
note() {
    echo "Note: $*"
    if [[ -n ${WEBMANAGER_INSTALL_NOTES_FILE:-} ]]; then
        printf '%s\n' "$*" >>"$WEBMANAGER_INSTALL_NOTES_FILE" || true
    fi
}

# A new replica cannot start unless its primary answers with the shared
# secret key, so find out now - before anything on this server is changed -
# and say exactly what is wrong instead of leaving a service that crash-loops.
check_primary() {
    python3 - "$REPLICA_OF" "$PEER_TOKEN" <<'PY'
import sys
import urllib.error
import urllib.request

url, token = sys.argv[1].rstrip("/"), sys.argv[2]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


request = urllib.request.Request(
    url + "/mesh/secret-key", headers={"Authorization": "Bearer " + token}
)
try:
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        if response.status == 200 and response.read(4096).strip():
            print(f"The primary at {url} answered and accepted the token.")
            raise SystemExit(0)
    problem = "it answered, but not with a secret key"
except urllib.error.HTTPError as exc:
    if exc.code == 401:
        problem = (
            "it rejected the token. Set the same WEBMANAGER_PEER_TOKEN (16+ characters) in "
            "/etc/webmanager/webmanager.env on the primary, restart it, and use that value here"
        )
    elif exc.code == 404:
        problem = (
            "it answered 404, so that address does not reach a WebManager with replication. "
            "Use the primary's dashboard address (for example http://PRIMARY-IP:8080 or its "
            "public https:// address), and update the primary first: on it, run 'git pull && "
            "sudo bash setup.sh' and make sure WEBMANAGER_PEER_TOKEN is set there"
        )
    elif 300 <= exc.code < 400:
        problem = f"it redirected to {exc.headers.get('Location')}; use that final address instead"
    else:
        problem = f"it answered HTTP {exc.code}"
except (urllib.error.URLError, OSError) as exc:
    problem = f"could not connect ({getattr(exc, 'reason', exc)}). Check the address, the network and any firewall"
print(f"Cannot use {url} as the primary: {problem}.", file=sys.stderr)
print("Nothing was changed on this server. Fix the above and run setup again, or add", file=sys.stderr)
print("--skip-primary-check to install anyway.", file=sys.stderr)
raise SystemExit(1)
PY
}

if [[ $SELF_UPDATE -eq 0 && -n $REPLICA_OF && $SKIP_PRIMARY_CHECK -eq 0 ]]; then
    if command -v python3 >/dev/null 2>&1; then
        echo "[0/8] Checking that the primary is reachable"
        check_primary
    else
        echo "python3 is not installed yet; skipping the primary reachability check."
    fi
fi

NEW_VENV=
OLD_VENV=
VENV_SWAPPED=0
INSTALL_SUCCEEDED=0
cleanup_install() {
    local exit_code=$?
    local restored=0
    trap - EXIT
    set +e
    if [[ -n $NEW_VENV && -e $NEW_VENV ]]; then
        rm -rf "$NEW_VENV"
    fi
    if [[ $INSTALL_SUCCEEDED -ne 1 && $VENV_SWAPPED -eq 1 ]]; then
        systemctl stop webmanager 2>/dev/null || true
        if [[ -n $OLD_VENV && -e $OLD_VENV ]]; then
            if rm -rf "$APP_DIR/.venv" \
                && [[ ! -e "$APP_DIR/.venv" ]] \
                && mv "$OLD_VENV" "$APP_DIR/.venv"; then
                restored=1
                echo "Restored the previous Python environment after installation failure." >&2
            else
                echo "Could not restore the previous Python environment automatically." >&2
            fi
        elif [[ -x "$APP_DIR/.venv/bin/python" ]]; then
            echo "No previous Python environment existed; keeping the validated replacement." >&2
        fi
        if [[ $SELF_UPDATE -eq 0 && $restored -eq 1 ]]; then
            systemctl reset-failed webmanager 2>/dev/null || true
            systemctl restart webmanager 2>/dev/null || true
        fi
    fi
    exit "$exit_code"
}
trap cleanup_install EXIT

if [[ $SELF_UPDATE -eq 0 ]]; then
    echo "[1/8] Installing Debian packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        nginx \
        openssh-client \
        python3 \
        python3-pip \
        python3-venv \
        util-linux
else
    echo "[1/8] Debian packages already installed"
fi

echo "[2/8] Creating the webmanager service account"
if ! getent group webmanager >/dev/null; then
    addgroup --system webmanager
fi
if ! id webmanager >/dev/null 2>&1; then
    adduser \
        --system \
        --ingroup webmanager \
        --home "$DATA_DIR" \
        --no-create-home \
        --shell /usr/sbin/nologin \
        webmanager
fi

echo "[3/8] Installing application files"
REUSE_VENV=0
if [[ -x "$APP_DIR/.venv/bin/python" ]] \
    && [[ -r "$APP_DIR/requirements.txt" ]] \
    && cmp -s "$APP_DIR/requirements.txt" "$SOURCE_DIR/requirements.txt" \
    && "$APP_DIR/.venv/bin/python" -c \
        "import authlib, flask, requests, waitress" 2>/dev/null; then
    REUSE_VENV=1
fi
install -d -o root -g root -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/webmanager"
cp -a "$SOURCE_DIR/webmanager" "$APP_DIR/webmanager"
install -o root -g root -m 0644 "$SOURCE_DIR/run.py" "$APP_DIR/run.py"
install -o root -g root -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -o root -g root -m 0644 "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
install -o root -g root -m 0755 "$SOURCE_DIR/configure-google.sh" "$APP_DIR/configure-google.sh"
install -o root -g root -m 0644 "$SOURCE_DIR/deploy/debian/nginx-sites.conf" "$APP_DIR/nginx-sites.conf"
SOURCE_COMMIT=
if git -C "$SOURCE_DIR" diff --quiet 2>/dev/null \
    && git -C "$SOURCE_DIR" diff --cached --quiet 2>/dev/null \
    && [[ -z $(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null) ]]; then
    SOURCE_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)
fi
find "$APP_DIR/webmanager" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$APP_DIR/webmanager" -type f -name '*.pyc' -delete
find "$APP_DIR/webmanager" -type d -exec chmod 0755 {} +
find "$APP_DIR/webmanager" -type f -exec chmod 0644 {} +
chown -R root:root "$APP_DIR"

echo "[4/8] Preparing the Python virtual environment"
if [[ $REUSE_VENV -eq 1 ]]; then
    echo "Reusing the installed Python environment because requirements are unchanged."
else
    echo "Building a replacement Python environment before changing the active one."
    NEW_VENV=$(mktemp -d "$APP_DIR/.venv.build.XXXXXX")
    python3 -m venv "$NEW_VENV"
    "$NEW_VENV/bin/python" -m pip install \
        --disable-pip-version-check \
        --retries 5 \
        --timeout 30 \
        --upgrade pip
    "$NEW_VENV/bin/python" -m pip install \
        --disable-pip-version-check \
        --retries 5 \
        --timeout 30 \
        -r "$APP_DIR/requirements.txt"
    if ! "$NEW_VENV/bin/python" -c \
        "import authlib, flask, requests, waitress" 2>/dev/null; then
        echo "The replacement Python environment failed validation." >&2
        exit 1
    fi

    if [[ -e "$APP_DIR/.venv" ]]; then
        OLD_VENV="$APP_DIR/.venv.previous.$$"
        mv "$APP_DIR/.venv" "$OLD_VENV"
    fi
    if ! mv "$NEW_VENV" "$APP_DIR/.venv"; then
        if [[ -n $OLD_VENV && -e $OLD_VENV ]]; then
            mv "$OLD_VENV" "$APP_DIR/.venv"
        fi
        exit 1
    fi
    NEW_VENV=
    VENV_SWAPPED=1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]] \
    || ! "$APP_DIR/.venv/bin/python" -c \
        "import authlib, flask, requests, waitress" 2>/dev/null; then
    echo "The installed Python environment failed validation." >&2
    exit 1
fi
# mktemp creates the replacement environment's top directory with mode 0700.
# The service runs as webmanager and must be able to traverse that directory.
chmod 0755 "$APP_DIR/.venv"
chown -R root:root "$APP_DIR/.venv"

echo "[5/8] Preparing persistent data and configuration"
install -d -o webmanager -g webmanager -m 0750 "$DATA_DIR"
install -d -o webmanager -g webmanager -m 0750 \
    "$DATA_DIR/repositories" \
    "$DATA_DIR/nginx" \
    "$DATA_DIR/logs"
install -d -o root -g webmanager -m 0710 "$UPDATER_STATE"
install -d -o webmanager -g webmanager -m 0750 "$UPDATER_STATE/requests"
# The System page's "Install updates automatically" switch is this file
# (the web app runs as webmanager, so it must be able to create and remove it).
case "$AUTO_UPDATE" in
    on)
        install -o webmanager -g webmanager -m 0640 /dev/null "$UPDATER_STATE/requests/auto-install"
        ;;
    off)
        rm -f "$UPDATER_STATE/requests/auto-install"
        ;;
esac
install -d -o root -g webmanager -m 0750 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/webmanager.env" ]]; then
    install -o root -g webmanager -m 0640 \
        "$SCRIPT_DIR/webmanager.env" \
        "$CONFIG_DIR/webmanager.env"
else
    echo "Keeping existing $CONFIG_DIR/webmanager.env"
fi

ensure_env() {
    local key=$1
    local value=$2
    if ! grep -q "^${key}=" "$CONFIG_DIR/webmanager.env"; then
        printf '%s=%s\n' "$key" "$value" >>"$CONFIG_DIR/webmanager.env"
    fi
}

ensure_env WEBMANAGER_GOOGLE_CLIENT_ID ""
ensure_env WEBMANAGER_GOOGLE_CLIENT_SECRET ""
ensure_env WEBMANAGER_GOOGLE_REDIRECT_URI ""
ensure_env WEBMANAGER_GOOGLE_ALLOWED_DOMAINS ""
ensure_env WEBMANAGER_GOOGLE_ALLOWED_EMAILS ""
ensure_env WEBMANAGER_SITE_GATEWAY_PORT "8090"
ensure_env WEBMANAGER_SITE_BASE_DOMAIN ""
ensure_env WEBMANAGER_SITE_PUBLIC_SCHEME "http"
ensure_env WEBMANAGER_AUTO_REFRESH_ENABLED "1"
ensure_env WEBMANAGER_AUTO_REFRESH_POLL_SECONDS "30"
ensure_env WEBMANAGER_MAX_REPOSITORY_MB "1024"
ensure_env WEBMANAGER_APPS_ENABLED "0"
ensure_env WEBMANAGER_PEERS ""
ensure_env WEBMANAGER_PEER_TOKEN ""
ensure_env WEBMANAGER_REPLICA_OF ""

# --replica-of / --peer-token / --peers: join an existing WebManager server.
set_env() {
    local key=$1 value=$2 temporary
    temporary=$(mktemp)
    grep -v "^${key}=" "$CONFIG_DIR/webmanager.env" >"$temporary" || true
    printf '%s=%s\n' "$key" "$value" >>"$temporary"
    install -o root -g webmanager -m 0640 "$temporary" "$CONFIG_DIR/webmanager.env"
    rm -f "$temporary"
}
if [[ -n $REPLICA_OF$PEERS ]] && [[ -z $PEER_TOKEN ]]; then
    echo "--replica-of and --peers need --peer-token." >&2
    exit 1
fi
if [[ -n $PEER_TOKEN ]]; then
    if [[ ! $PEER_TOKEN =~ ^[A-Za-z0-9._~+/=-]{16,}$ ]]; then
        echo "--peer-token must be 16+ characters of letters, digits or ._~+/=- (try: openssl rand -hex 24)." >&2
        exit 1
    fi
    set_env WEBMANAGER_PEER_TOKEN "$PEER_TOKEN"
fi
if [[ -n $REPLICA_OF ]]; then
    if [[ ! $REPLICA_OF =~ ^https?://[][A-Za-z0-9.:_-]+(/[A-Za-z0-9._~/-]*)?$ ]]; then
        echo "--replica-of must be the primary's dashboard URL, e.g. https://server-a.example.com" >&2
        exit 1
    fi
    set_env WEBMANAGER_REPLICA_OF "$REPLICA_OF"
fi
if [[ -n $PEERS ]]; then
    if [[ ! $PEERS =~ ^https?://[][A-Za-z0-9.:_/,-]+$ ]]; then
        echo "--peers must be comma-separated http(s) URLs." >&2
        exit 1
    fi
    set_env WEBMANAGER_PEERS "$PEERS"
fi

if [[ -n ${WEBMANAGER_INITIAL_SITE_BASE_DOMAIN:-} ]] \
    && ! grep -Eq '^WEBMANAGER_SITE_BASE_DOMAIN=.+$' "$CONFIG_DIR/webmanager.env"; then
    TEMP_ENV=$(mktemp)
    grep -v '^WEBMANAGER_SITE_BASE_DOMAIN=' "$CONFIG_DIR/webmanager.env" >"$TEMP_ENV" || true
    printf 'WEBMANAGER_SITE_BASE_DOMAIN=%s\n' \
        "$WEBMANAGER_INITIAL_SITE_BASE_DOMAIN" >>"$TEMP_ENV"
    install -o root -g webmanager -m 0640 "$TEMP_ENV" "$CONFIG_DIR/webmanager.env"
    rm -f "$TEMP_ENV"
fi

chown root:webmanager "$CONFIG_DIR/webmanager.env"
chmod 0640 "$CONFIG_DIR/webmanager.env"

if [[ -f $UPDATER_ENV && $UPDATE_CONFIGURATION_EXPLICIT -eq 0 ]]; then
    UPDATE_REPOSITORY=$(sed -n 's/^WEBMANAGER_UPDATE_REPOSITORY=//p' "$UPDATER_ENV" | tail -n 1)
    UPDATE_BRANCH=$(sed -n 's/^WEBMANAGER_UPDATE_BRANCH=//p' "$UPDATER_ENV" | tail -n 1)
    echo "Keeping existing $UPDATER_ENV"
else
    if [[ -z $UPDATE_REPOSITORY ]]; then
        UPDATE_REPOSITORY=$(git -C "$SOURCE_DIR" remote get-url origin 2>/dev/null || true)
    fi
    UPDATE_REPOSITORY=${UPDATE_REPOSITORY:-$DEFAULT_UPDATE_REPOSITORY}
    if [[ $UPDATE_REPOSITORY =~ ^git@github\.com:([^/]+)/(.+)$ ]]; then
        UPDATE_REPOSITORY="https://github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    elif [[ $UPDATE_REPOSITORY =~ ^ssh://git@github\.com/([^/]+)/(.+)$ ]]; then
        UPDATE_REPOSITORY="https://github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    fi
    if [[ -z $UPDATE_BRANCH ]]; then
        UPDATE_BRANCH=$(git -C "$SOURCE_DIR" branch --show-current 2>/dev/null || true)
    fi
    UPDATE_BRANCH=${UPDATE_BRANCH:-main}

    if [[ -n $UPDATE_REPOSITORY ]]; then
        if [[ ! $UPDATE_REPOSITORY =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]; then
            echo "Automatic updates require an HTTPS github.com repository URL." >&2
            exit 1
        fi
        if [[ ! $UPDATE_BRANCH =~ ^[A-Za-z0-9._/-]+$ ]] || [[ $UPDATE_BRANCH == -* ]] || [[ $UPDATE_BRANCH == *..* ]]; then
            echo "Automatic update branch is invalid." >&2
            exit 1
        fi
        cat >"$UPDATER_ENV" <<EOF
WEBMANAGER_UPDATE_ENABLED=1
WEBMANAGER_UPDATE_REPOSITORY=$UPDATE_REPOSITORY
WEBMANAGER_UPDATE_BRANCH=$UPDATE_BRANCH
EOF
        chown root:root "$UPDATER_ENV"
        chmod 0600 "$UPDATER_ENV"
    elif [[ ! -f $UPDATER_ENV ]]; then
        cat >"$UPDATER_ENV" <<EOF
WEBMANAGER_UPDATE_ENABLED=0
WEBMANAGER_UPDATE_REPOSITORY=
WEBMANAGER_UPDATE_BRANCH=main
EOF
        chown root:root "$UPDATER_ENV"
        chmod 0600 "$UPDATER_ENV"
    fi
fi

# The web app checks GitHub itself too, and can't read updater.env (root only):
# give it the same repository and branch so both always look at the same thing.
if [[ -n $UPDATE_REPOSITORY ]]; then
    set_env WEBMANAGER_UPDATE_REPOSITORY "$UPDATE_REPOSITORY"
    set_env WEBMANAGER_UPDATE_BRANCH "$UPDATE_BRANCH"
fi

env_value() {
    sed -n "s/^$1=//p" "$CONFIG_DIR/webmanager.env" | tail -n 1
}

APP_HOST=$(env_value WEBMANAGER_HOST)
APP_PORT=$(env_value WEBMANAGER_PORT)
SITE_PORT_MIN=$(env_value WEBMANAGER_SITE_PORT_MIN)
SITE_PORT_MAX=$(env_value WEBMANAGER_SITE_PORT_MAX)
SITE_GATEWAY_PORT=$(env_value WEBMANAGER_SITE_GATEWAY_PORT)
SITE_BASE_DOMAIN=$(env_value WEBMANAGER_SITE_BASE_DOMAIN)
GOOGLE_REDIRECT_URI=$(env_value WEBMANAGER_GOOGLE_REDIRECT_URI)
APP_HOST=${APP_HOST:-127.0.0.1}
APP_PORT=${APP_PORT:-5000}
SITE_PORT_MIN=${SITE_PORT_MIN:-8100}
SITE_PORT_MAX=${SITE_PORT_MAX:-8999}
SITE_GATEWAY_PORT=${SITE_GATEWAY_PORT:-8090}
for port_value in "$SITE_GATEWAY_PORT" "$APP_PORT" "$SITE_PORT_MIN" "$SITE_PORT_MAX"; do
    if [[ ! $port_value =~ ^[0-9]{1,5}$ ]]; then
        echo "Invalid port in $CONFIG_DIR/webmanager.env: '$port_value'." >&2
        exit 1
    fi
done
DASHBOARD_HOST=
if [[ -n $GOOGLE_REDIRECT_URI ]]; then
    DASHBOARD_HOST=$(python3 - "$GOOGLE_REDIRECT_URI" <<'PY'
import sys
from urllib.parse import urlsplit

print((urlsplit(sys.argv[1]).hostname or "").lower())
PY
)
fi

echo "[6/8] Installing systemd and Nginx configuration"
install -o root -g root -m 0644 "$SCRIPT_DIR/webmanager.service" "$SERVICE_FILE"
install -o root -g root -m 0755 "$SCRIPT_DIR/update.sh" "$UPDATER_SCRIPT"
install -o root -g root -m 0644 "$SCRIPT_DIR/webmanager-update.service" "$UPDATER_SERVICE"
install -o root -g root -m 0644 "$SCRIPT_DIR/webmanager-update.timer" "$UPDATER_TIMER"
install -o root -g root -m 0644 "$SCRIPT_DIR/webmanager-update.path" "$UPDATER_PATH"
if ! install -o root -g root -m 0755 "$SCRIPT_DIR/uninstall.sh" "$UNINSTALL_COMMAND"; then
    if [[ $SELF_UPDATE -eq 1 ]]; then
        echo "The existing updater sandbox deferred $UNINSTALL_COMMAND until the next update."
    else
        exit 1
    fi
fi
if ! install -o root -g root -m 0644 "$SCRIPT_DIR/webmanager-logrotate" "$LOGROTATE_FILE"; then
    if [[ $SELF_UPDATE -eq 1 ]]; then
        echo "The existing updater sandbox deferred $LOGROTATE_FILE until the next update."
    else
        exit 1
    fi
fi
# Nginx is the one part of an update that can be refused by something outside
# WebManager. Keep the current files so an update can put them back and still
# install the new version, instead of failing (and undoing) the whole update.
NGINX_SNAPSHOT=$(mktemp -d)
for nginx_name in webmanager webmanager-sites; do
    if [[ -f "/etc/nginx/sites-available/$nginx_name" ]]; then
        cp -a "/etc/nginx/sites-available/$nginx_name" "$NGINX_SNAPSHOT/$nginx_name"
    fi
done
restore_nginx_files() {
    local nginx_name
    for nginx_name in webmanager webmanager-sites; do
        if [[ -f "$NGINX_SNAPSHOT/$nginx_name" ]]; then
            cp -a "$NGINX_SNAPSHOT/$nginx_name" "/etc/nginx/sites-available/$nginx_name"
            ln -sfn "/etc/nginx/sites-available/$nginx_name" "/etc/nginx/sites-enabled/$nginx_name"
        else
            rm -f "/etc/nginx/sites-enabled/$nginx_name" "/etc/nginx/sites-available/$nginx_name"
        fi
    done
    return 0
}

# Hosts whose kernel has no IPv6 cannot open "listen [::]:port" sockets, and
# Nginx refuses to start at all when one is configured.
drop_ipv6_listeners() {
    local nginx_file
    for nginx_file in "$@"; do
        if [[ -f $nginx_file ]]; then
            sed -i '/^[[:space:]]*listen[[:space:]]\+\[::\]/d' "$nginx_file"
        fi
    done
    return 0
}

# Servers installed before replication existed proxy the dashboard without a
# /replication/ location; add it (long timeouts, no buffering) so other servers
# can pull large data snapshots. Files that already have one, or don't proxy
# the dashboard the usual way, are left alone.
ensure_replication_location() {
    local nginx_file=$1 port=$2
    [[ -f $nginx_file ]] || return 0
    python3 - "$nginx_file" "$port" <<'PY' || true
import re
import sys
from pathlib import Path

path, port = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
if "/replication/" in text:
    raise SystemExit(0)
proxy = f"proxy_pass http://127.0.0.1:{port}"


def matching_brace(source, opening):
    depth = 0
    index = opening
    while index < len(source):
        char = source[index]
        if char == "#":
            index = source.find("\n", index)
            if index < 0:
                return -1
        elif char in "\"'":
            index = source.find(char, index + 1)
            if index < 0:
                return -1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


result = []
position = 0
for match in re.finditer(r"^([ \t]*)location\s+/\s*\{", text, re.M):
    if match.start() < position:
        continue
    opening = match.end() - 1
    closing = matching_brace(text, opening)
    if closing < 0 or proxy not in text[opening:closing]:
        continue
    indent = match.group(1)
    block = (
        "\n\n{i}# Other WebManager servers pull site/app data snapshots here; they can be\n"
        "{i}# large and slow to produce.\n"
        "{i}location ^~ /replication/ {{\n"
        "{i}    proxy_pass http://127.0.0.1:{p};\n"
        "{i}    proxy_http_version 1.1;\n"
        "{i}    proxy_set_header Host $http_host;\n"
        "{i}    proxy_set_header X-Forwarded-Host $http_host;\n"
        "{i}    proxy_set_header X-Real-IP $remote_addr;\n"
        "{i}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "{i}    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "{i}    proxy_buffering off;\n"
        "{i}    proxy_read_timeout 900s;\n"
        "{i}    proxy_send_timeout 900s;\n"
        "{i}}}"
    ).format(i=indent, p=port)
    result.append(text[position:closing + 1] + block)
    position = closing + 1
if result:
    path.write_text("".join(result) + text[position:], encoding="utf-8")
PY
}

if [[ ! -f "$NGINX_AVAILABLE" ]]; then
    install -o root -g root -m 0644 "$SCRIPT_DIR/nginx-dashboard.conf" "$NGINX_AVAILABLE"
else
    echo "Keeping existing $NGINX_AVAILABLE"
fi
if [[ -n $DASHBOARD_HOST ]]; then
    DASHBOARD_NGINX_TEMP=$(mktemp)
    sed -E \
        "s/^([[:space:]]*)server_name[[:space:]]+[^;]+;/\\1server_name $DASHBOARD_HOST;/" \
        "$NGINX_AVAILABLE" >"$DASHBOARD_NGINX_TEMP"
    install -o root -g root -m 0644 "$DASHBOARD_NGINX_TEMP" "$NGINX_AVAILABLE"
    rm -f "$DASHBOARD_NGINX_TEMP"
fi
ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"
if [[ -n $SITE_BASE_DOMAIN || -n $DASHBOARD_HOST ]]; then
    SITE_NGINX_TEMP=$(mktemp)
    sed \
        -e "s|@SITE_GATEWAY_PORT@|$SITE_GATEWAY_PORT|g" \
        -e "s|@APP_PORT@|$APP_PORT|g" \
        "$SCRIPT_DIR/nginx-sites.conf" >"$SITE_NGINX_TEMP"
    if install -o root -g root -m 0644 "$SITE_NGINX_TEMP" "$SITE_NGINX_AVAILABLE" \
        && ln -sfn "$SITE_NGINX_AVAILABLE" "$SITE_NGINX_ENABLED"; then
        :
    elif [[ $SELF_UPDATE -eq 1 ]]; then
        echo "The existing updater sandbox deferred wildcard Nginx changes until the next update."
    else
        rm -f "$SITE_NGINX_TEMP"
        exit 1
    fi
    rm -f "$SITE_NGINX_TEMP"
else
    if ! rm -f "$SITE_NGINX_ENABLED" "$SITE_NGINX_AVAILABLE"; then
        if [[ $SELF_UPDATE -eq 1 ]]; then
            echo "The existing updater sandbox deferred wildcard Nginx cleanup."
        else
            exit 1
        fi
    fi
fi
ensure_replication_location "$NGINX_AVAILABLE" "$APP_PORT"
if [[ ! -e /proc/net/if_inet6 ]]; then
    drop_ipv6_listeners "$NGINX_AVAILABLE" "$SITE_NGINX_AVAILABLE"
fi
NGINX_OK=1
if ! NGINX_TEST_OUTPUT=$(nginx -t 2>&1); then
    printf '%s\n' "$NGINX_TEST_OUTPUT" >&2
    if [[ $SELF_UPDATE -ne 1 ]]; then
        exit 1
    fi
    NGINX_PROBLEM=$(printf '%s\n' "$NGINX_TEST_OUTPUT" | grep -m1 -E '\[(emerg|alert|crit)\]' | cut -c1-240 || true)
    restore_nginx_files
    if nginx -t >/dev/null 2>&1; then
        note "The new Nginx configuration was refused by nginx -t (${NGINX_PROBLEM:-no details}) and was not applied; the previous one is still in use."
    else
        NGINX_OK=0
        note "Nginx's configuration test fails (${NGINX_PROBLEM:-no details}) even with the previous files, so Nginx was left as it is."
    fi
else
    printf '%s\n' "$NGINX_TEST_OUTPUT"
fi
rm -rf "$NGINX_SNAPSHOT"

echo "[7/8] Starting services"
systemctl daemon-reload
if [[ $NGINX_OK -eq 1 ]]; then
    systemctl enable --now nginx
    if ! systemctl reload nginx; then
        if [[ $SELF_UPDATE -eq 1 ]]; then
            note "Nginx could not be reloaded, so it is still using its previous configuration."
        else
            exit 1
        fi
    fi
fi
systemctl enable webmanager
systemctl restart webmanager
if grep -q '^WEBMANAGER_UPDATE_ENABLED=1$' "$UPDATER_ENV"; then
    if [[ $SELF_UPDATE -eq 1 ]]; then
        echo "Leaving updater triggers unchanged during the active self-update."
    else
        # WEBMANAGER_UPDATE_ENABLED=1 is the switch. (Following the units'
        # previous state instead trapped older installs, whose units were
        # never enabled, with the updater off forever: the System page
        # asked for update checks that nothing ever answered.)
        systemctl enable --now webmanager-update.timer
        systemctl enable --now webmanager-update.path
        systemctl start webmanager-update.service
    fi
elif [[ $SELF_UPDATE -eq 0 ]]; then
    systemctl disable --now webmanager-update.timer 2>/dev/null || true
    systemctl disable --now webmanager-update.path 2>/dev/null || true
fi

echo "[8/8] Configuring UFW when it is already active"
if [[ $SELF_UPDATE -eq 1 ]]; then
    # The updater's sandbox cannot change firewall rules, and an update does not
    # change which ports WebManager uses.
    echo "Firewall rules are left as they are during an update."
elif command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 8080/tcp
    if [[ -n $SITE_BASE_DOMAIN ]]; then
        ufw allow 80/tcp
        ufw allow 443/tcp
        ufw --force delete allow "${SITE_PORT_MIN}:${SITE_PORT_MAX}/tcp" 2>/dev/null || true
    else
        ufw allow "${SITE_PORT_MIN}:${SITE_PORT_MAX}/tcp"
    fi
fi

case "$APP_HOST" in
    0.0.0.0 | "::")
        HEALTH_HOST=127.0.0.1
        ;;
    *:*)
        HEALTH_HOST="[$APP_HOST]"
        ;;
    *)
        HEALTH_HOST=$APP_HOST
        ;;
esac

READY=0
for _ in {1..30}; do
    if "$APP_DIR/.venv/bin/python" -c \
        "import urllib.request; urllib.request.urlopen('http://${HEALTH_HOST}:${APP_PORT}/healthz', timeout=2).read()" \
        >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done

if [[ $READY -ne 1 ]] || ! systemctl is-active --quiet webmanager; then
    echo "WebManager failed to start. Recent logs:" >&2
    journalctl -u webmanager -n 60 --no-pager >&2
    exit 1
fi

if [[ $SELF_UPDATE -eq 0 ]]; then
    rm -f \
        "$UPDATER_STATE/requests/install.commit" \
        "$UPDATER_STATE/requests/check"
fi

if [[ -n $SOURCE_COMMIT ]]; then
    printf '%s\n' "$SOURCE_COMMIT" >"$APP_DIR/.installed-commit"
    chmod 0644 "$APP_DIR/.installed-commit"
    if [[ $SELF_UPDATE -eq 0 ]]; then
        STATUS_TEMP=$(mktemp "$UPDATER_STATE/status.XXXXXX")
        python3 - "$STATUS_TEMP" "$SOURCE_COMMIT" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, commit = sys.argv[1:]
payload = {
    "state": "current",
    "installed_commit": commit,
    "available_commit": commit,
    "update_available": False,
    "message": "WebManager was installed successfully by manual setup.",
    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
    handle.write("\n")
os.chmod(path, 0o640)
PY
        chown root:webmanager "$STATUS_TEMP"
        mv -f "$STATUS_TEMP" "$UPDATER_STATUS"
    fi
else
    rm -f "$APP_DIR/.installed-commit"
fi

INSTALL_SUCCEEDED=1
trap - EXIT
if [[ -n $OLD_VENV && -e $OLD_VENV ]]; then
    rm -rf "$OLD_VENV" \
        || echo "Warning: could not remove the previous Python environment." >&2
fi

# Only for the closing message. `hostname -I` needs a netlink socket, which the
# updater's sandbox forbids; under `set -e -o pipefail` that failure used to
# make every automatic update fail (and roll back) after it had installed.
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
SERVER_IP=${SERVER_IP:-SERVER_IP}

# Tell the server this one joined about our address, so it lists us without
# anyone typing it in there. Never fatal: the admin can add us by hand.
if [[ $SELF_UPDATE -eq 0 && $ANNOUNCE -eq 1 ]]; then
    ANNOUNCE_TARGET=${REPLICA_OF:-${PEERS%%,*}}
    if [[ -z $ANNOUNCE_URL && $SERVER_IP != SERVER_IP ]]; then
        ANNOUNCE_URL="http://$SERVER_IP:8080"
    fi
    if [[ -z $ANNOUNCE_TARGET || -z $ANNOUNCE_URL ]]; then
        echo "Could not work out this server's address to announce; add it in the other server's Servers panel."
    elif python3 - "$ANNOUNCE_TARGET" "$PEER_TOKEN" "$ANNOUNCE_URL" <<'PY'
import json
import sys
import urllib.error
import urllib.request

target, token, own = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
request = urllib.request.Request(
    target + "/mesh/register",
    data=json.dumps({"url": own}).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=20).read()
except urllib.error.HTTPError as exc:
    try:
        detail = json.loads(exc.read().decode()).get("error", "")
    except Exception:
        detail = ""
    print(f"The other server refused the announcement (HTTP {exc.code}). {detail}", file=sys.stderr)
    raise SystemExit(1)
except Exception as exc:
    print(f"Could not reach the other server to announce: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
    then
        echo "Announced $ANNOUNCE_URL to $ANNOUNCE_TARGET; it now appears in that server's Servers panel."
    else
        echo "Could not announce this server. Add $ANNOUNCE_URL in the other server's Servers panel instead."
    fi
fi

echo
echo "============================================================"
echo " WebManager is ready"
echo "============================================================"
echo
echo "Open: http://$SERVER_IP:8080"
echo
echo "Then:"
echo "  1. Configure Google sign-in if setup prompts you."
echo "  2. Sign in with Google."
echo "  3. Paste a Git repository URL."
echo "  4. Choose the folder containing index.html and deploy."
echo
if [[ -n $SITE_BASE_DOMAIN ]]; then
    echo "Deployed sites: <site-name>.$SITE_BASE_DOMAIN"
    echo "DNS required: *.$SITE_BASE_DOMAIN must point to this server"
else
    echo "Deployed site ports: $SITE_PORT_MIN-$SITE_PORT_MAX"
fi
echo "Logs: journalctl -u webmanager -f"
echo "Settings: $CONFIG_DIR/webmanager.env"
if grep -q '^WEBMANAGER_UPDATE_ENABLED=1$' "$UPDATER_ENV"; then
    echo "Program updates: checks enabled for $UPDATE_REPOSITORY ($UPDATE_BRANCH)"
    echo "Installation requires super-admin approval in the WebManager interface."
else
    echo "Program updates: disabled; configure $UPDATER_ENV to enable them"
fi
