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
DEFAULT_UPDATE_REPOSITORY=https://github.com/coolguy1333/WebManager.git
SELF_UPDATE=0
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
    deploy/debian/webmanager-update.path; do
    if [[ ! -e "$SOURCE_DIR/$required" ]]; then
        echo "Missing source item: $SOURCE_DIR/$required" >&2
        exit 1
    fi
done

if [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
    echo "Run the installer from a source checkout outside $APP_DIR." >&2
    exit 1
fi

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
SOURCE_COMMIT=
if git -C "$SOURCE_DIR" diff --quiet 2>/dev/null \
    && git -C "$SOURCE_DIR" diff --cached --quiet 2>/dev/null \
    && [[ -z $(git -C "$SOURCE_DIR" status --porcelain 2>/dev/null) ]]; then
    SOURCE_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)
fi
if [[ -n $SOURCE_COMMIT ]]; then
    printf '%s\n' "$SOURCE_COMMIT" >"$APP_DIR/.installed-commit"
    chmod 0644 "$APP_DIR/.installed-commit"
else
    rm -f "$APP_DIR/.installed-commit"
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
    python3 -m venv "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check \
        --retries 5 \
        --timeout 30 \
        --upgrade pip
    "$APP_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check \
        --retries 5 \
        --timeout 30 \
        -r "$APP_DIR/requirements.txt"
fi
chown -R root:root "$APP_DIR/.venv"

echo "[5/8] Preparing persistent data and configuration"
install -d -o webmanager -g webmanager -m 0750 "$DATA_DIR"
install -d -o webmanager -g webmanager -m 0750 \
    "$DATA_DIR/repositories" \
    "$DATA_DIR/nginx" \
    "$DATA_DIR/logs"
install -d -o root -g webmanager -m 0710 "$UPDATER_STATE"
install -d -o webmanager -g webmanager -m 0750 "$UPDATER_STATE/requests"
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

set_env_value() {
    local key=$1
    local value=$2
    local temporary
    temporary=$(mktemp)
    grep -v "^${key}=" "$CONFIG_DIR/webmanager.env" >"$temporary" || true
    printf '%s=%s\n' "$key" "$value" >>"$temporary"
    install -o root -g webmanager -m 0640 "$temporary" "$CONFIG_DIR/webmanager.env"
    rm -f "$temporary"
}

SITE_BASE_DOMAIN=$(sed -n 's/^WEBMANAGER_SITE_BASE_DOMAIN=//p' "$CONFIG_DIR/webmanager.env" | tail -n 1)
GOOGLE_REDIRECT_URI=$(sed -n 's/^WEBMANAGER_GOOGLE_REDIRECT_URI=//p' "$CONFIG_DIR/webmanager.env" | tail -n 1)
if [[ -z $SITE_BASE_DOMAIN && -n $GOOGLE_REDIRECT_URI ]]; then
    SITE_BASE_DOMAIN=$(python3 - "$GOOGLE_REDIRECT_URI" <<'PY'
import sys
from urllib.parse import urlsplit
print(urlsplit(sys.argv[1]).hostname or "")
PY
)
    if [[ -n $SITE_BASE_DOMAIN ]]; then
        set_env_value WEBMANAGER_SITE_BASE_DOMAIN "$SITE_BASE_DOMAIN"
    fi
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
if [[ -n $SITE_BASE_DOMAIN ]]; then
    SITE_NGINX_TEMP=$(mktemp)
    cat >"$SITE_NGINX_TEMP" <<EOF
map \$http_cf_connecting_ip \$webmanager_site_client_ip {
    default \$http_cf_connecting_ip;
    "" \$remote_addr;
}

map \$http_x_forwarded_proto \$webmanager_site_proto {
    default \$http_x_forwarded_proto;
    "" \$scheme;
}

server {
    listen 80;
    listen [::]:80;
    listen 8080;
    listen [::]:8080;
    server_name *.$SITE_BASE_DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$SITE_GATEWAY_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$webmanager_site_client_ip;
        proxy_set_header X-Forwarded-For \$webmanager_site_client_ip;
        proxy_set_header X-Forwarded-Proto \$webmanager_site_proto;
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
EOF
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
nginx -t

echo "[7/8] Starting services"
systemctl daemon-reload
systemctl enable --now nginx
systemctl reload nginx
systemctl enable webmanager
systemctl restart webmanager
if grep -q '^WEBMANAGER_UPDATE_ENABLED=1$' "$UPDATER_ENV"; then
    systemctl enable --now webmanager-update.timer
    systemctl enable --now webmanager-update.path
    if [[ $SELF_UPDATE -eq 0 ]]; then
        systemctl start webmanager-update.service
    fi
else
    systemctl disable --now webmanager-update.timer 2>/dev/null || true
    systemctl disable --now webmanager-update.path 2>/dev/null || true
fi

echo "[8/8] Configuring UFW when it is already active"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
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

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
SERVER_IP=${SERVER_IP:-SERVER_IP}

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
