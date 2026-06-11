#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE=/etc/webmanager/webmanager.env
NGINX_FILE=/etc/nginx/sites-available/webmanager
SITE_NGINX_FILE=/etc/nginx/sites-available/webmanager-sites
SITE_NGINX_LINK=/etc/nginx/sites-enabled/webmanager-sites

if [[ $EUID -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "Install sudo or run this script as root." >&2
        exit 1
    fi
    exec sudo bash "$0" "$@"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "WebManager is not installed. Run: bash setup.sh" >&2
    exit 1
fi

echo
echo "Google sign-in setup"
echo "===================="
echo
echo "Enter the public HTTPS address you want to use."
echo "This helper can configure Nginx and Let's Encrypt for the domain."
echo "Example: https://webmanager.example.com"
echo

read -r -p "Public WebManager URL: " PUBLIC_URL
PUBLIC_URL=${PUBLIC_URL%/}

if ! PUBLIC_URL=$(python3 - "$PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
parsed = urlsplit(value)
localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
    raise SystemExit("Use HTTPS. HTTP is allowed only for localhost development.")
if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
    raise SystemExit("Enter only the public origin, such as https://webmanager.example.com")
port = f":{parsed.port}" if parsed.port else ""
host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
print(f"{parsed.scheme}://{host}{port}")
PY
); then
    exit 1
fi

CALLBACK_URL="${PUBLIC_URL}/auth/google/callback"
PUBLIC_HOST=$(python3 - "$PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlsplit
print(urlsplit(sys.argv[1]).hostname)
PY
)
PUBLIC_PORT=$(python3 - "$PUBLIC_URL" <<'PY'
import sys
from urllib.parse import urlsplit
print(urlsplit(sys.argv[1]).port or "")
PY
)
SITE_PUBLIC_SCHEME=http

if [[ $PUBLIC_URL == https://* && $PUBLIC_HOST != "localhost" && $PUBLIC_HOST != "127.0.0.1" && $PUBLIC_HOST != "::1" ]]; then
    echo
    read -r -p "Configure HTTPS for $PUBLIC_HOST with Let's Encrypt? [Y/n]: " TLS_ANSWER
    case ${TLS_ANSWER:-Y} in
        [Yy]*)
            if [[ -n $PUBLIC_PORT && $PUBLIC_PORT != "443" ]]; then
                echo "Automatic HTTPS setup requires the normal HTTPS port (443)." >&2
                exit 1
            fi

            read -r -p "Email for Let's Encrypt renewal notices: " CERT_EMAIL
            if [[ $CERT_EMAIL != *@* ]]; then
                echo "Enter a valid email address." >&2
                exit 1
            fi

            APP_PORT=$(sed -n 's/^WEBMANAGER_PORT=//p' "$ENV_FILE" | tail -n 1)
            APP_PORT=${APP_PORT:-5000}
            BACKUP_FILE="${NGINX_FILE}.before-google-sso"
            cp "$NGINX_FILE" "$BACKUP_FILE"

            cat >"$NGINX_FILE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $PUBLIC_HOST;

    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$webmanager_site_client_ip;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
EOF

            export DEBIAN_FRONTEND=noninteractive
            apt-get update
            apt-get install -y certbot python3-certbot-nginx

            if ! nginx -t; then
                cp "$BACKUP_FILE" "$NGINX_FILE"
                nginx -t
                echo "The generated Nginx configuration was invalid; the previous file was restored." >&2
                exit 1
            fi
            systemctl reload nginx

            if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
                ufw allow 80/tcp
                ufw allow 443/tcp
            fi

            if ! certbot --nginx \
                --non-interactive \
                --agree-tos \
                --redirect \
                --no-eff-email \
                --email "$CERT_EMAIL" \
                -d "$PUBLIC_HOST"; then
                cp "$BACKUP_FILE" "$NGINX_FILE"
                nginx -t
                systemctl reload nginx
                echo "Certificate setup failed; the previous Nginx configuration was restored." >&2
                echo "Check that DNS points to this server and ports 80 and 443 are reachable." >&2
                exit 1
            fi
            ;;
    esac
fi

if [[ $PUBLIC_URL == https://* && $PUBLIC_HOST != "localhost" && $PUBLIC_HOST != "127.0.0.1" && $PUBLIC_HOST != "::1" ]]; then
    echo
    echo "The dashboard certificate does not automatically cover deployed site subdomains."
    echo "HTTPS site links require wildcard TLS coverage for *.$PUBLIC_HOST."
    echo
    echo "For Cloudflare Tunnel, add a SECOND public hostname:"
    echo
    echo "  *.$PUBLIC_HOST -> http://localhost:8080"
    echo
    echo "The existing $PUBLIC_HOST tunnel entry does not match site subdomains."
    echo "Cloudflare must also issue or provide TLS coverage for *.$PUBLIC_HOST."
    read -r -p "Is that wildcard tunnel hostname working with HTTPS? [y/N]: " SITE_TLS_ANSWER
    case $SITE_TLS_ANSWER in
        [Yy] | [Yy][Ee][Ss])
            SITE_PUBLIC_SCHEME=https
            ;;
    esac
fi

echo
echo "In Google Cloud, create an OAuth client of type 'Web application'."
echo "Use this exact Authorized redirect URI:"
echo
echo "  $CALLBACK_URL"
echo
echo "Google Cloud credentials: https://console.cloud.google.com/apis/credentials"
echo
read -r -p "Press Enter after the OAuth client is created..."

while true; do
    read -r -p "Google client ID: " CLIENT_ID
    CLIENT_ID=${CLIENT_ID//$'\r'/}
    CLIENT_ID="${CLIENT_ID#"${CLIENT_ID%%[![:space:]]*}"}"
    CLIENT_ID="${CLIENT_ID%"${CLIENT_ID##*[![:space:]]}"}"
    if [[ $CLIENT_ID =~ ^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$ ]]; then
        break
    fi
    echo "Enter the complete client ID ending in .apps.googleusercontent.com." >&2
done

while true; do
    read -r -s -p "Google client secret: " CLIENT_SECRET
    echo
    CLIENT_SECRET=${CLIENT_SECRET//$'\r'/}
    if [[ -n $CLIENT_SECRET ]]; then
        break
    fi
    echo "Client secret is required. Try again." >&2
done

while true; do
    echo
    echo "Restrict sign-in to trusted Google accounts."
    read -r -p "Allowed Google Workspace domains (comma-separated, blank for none): " ALLOWED_DOMAINS
    read -r -p "Allowed individual emails (comma-separated, blank for none): " ALLOWED_EMAILS
    ALLOWED_DOMAINS=${ALLOWED_DOMAINS//[[:space:]]/}
    ALLOWED_EMAILS=${ALLOWED_EMAILS//[[:space:]]/}
    if [[ -n $ALLOWED_DOMAINS || -n $ALLOWED_EMAILS ]]; then
        break
    fi

    echo
    echo "Warning: leaving both blank lets any verified Google account create an active user."
    read -r -p "Allow unrestricted Google sign-in? [y/N]: " OPEN_ACCESS
    case $OPEN_ACCESS in
        [Yy] | [Yy][Ee][Ss])
            break
            ;;
        *)
            echo "Enter at least one trusted domain or email address."
            ;;
    esac
done

for value in "$CLIENT_ID" "$CLIENT_SECRET" "$CALLBACK_URL" "$ALLOWED_DOMAINS" "$ALLOWED_EMAILS"; do
    if [[ $value == *$'\n'* || $value == *$'\r'* ]]; then
        echo "Configuration values may not contain line breaks." >&2
        exit 1
    fi
done

set_env() {
    local key=$1
    local value=$2
    local temporary
    temporary=$(mktemp)
    grep -v "^${key}=" "$ENV_FILE" >"$temporary" || true
    printf '%s=%s\n' "$key" "$value" >>"$temporary"
    install -o root -g webmanager -m 0640 "$temporary" "$ENV_FILE"
    rm -f "$temporary"
}

set_env WEBMANAGER_GOOGLE_CLIENT_ID "$CLIENT_ID"
set_env WEBMANAGER_GOOGLE_CLIENT_SECRET "$CLIENT_SECRET"
set_env WEBMANAGER_GOOGLE_REDIRECT_URI "$CALLBACK_URL"
set_env WEBMANAGER_GOOGLE_ALLOWED_DOMAINS "$ALLOWED_DOMAINS"
set_env WEBMANAGER_GOOGLE_ALLOWED_EMAILS "$ALLOWED_EMAILS"
set_env WEBMANAGER_SITE_BASE_DOMAIN "$PUBLIC_HOST"
set_env WEBMANAGER_SITE_PUBLIC_SCHEME "$SITE_PUBLIC_SCHEME"

SITE_GATEWAY_PORT=$(sed -n 's/^WEBMANAGER_SITE_GATEWAY_PORT=//p' "$ENV_FILE" | tail -n 1)
SITE_GATEWAY_PORT=${SITE_GATEWAY_PORT:-8090}
cat >"$SITE_NGINX_FILE" <<EOF
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
    server_name *.$PUBLIC_HOST;

    location / {
        proxy_pass http://127.0.0.1:$SITE_GATEWAY_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$webmanager_site_client_ip;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$webmanager_site_proto;
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
EOF
chown root:root "$SITE_NGINX_FILE"
chmod 0644 "$SITE_NGINX_FILE"
ln -sfn "$SITE_NGINX_FILE" "$SITE_NGINX_LINK"
nginx -t
systemctl reload nginx

if [[ $PUBLIC_URL == https://* ]]; then
    set_env WEBMANAGER_SESSION_COOKIE_SECURE 1
fi

systemctl restart webmanager

if ! systemctl is-active --quiet webmanager; then
    echo "WebManager failed to restart. Recent logs:" >&2
    journalctl -u webmanager -n 60 --no-pager >&2
    exit 1
fi

echo
echo "Google sign-in is configured."
echo "Open: $PUBLIC_URL/auth/login"
echo "Site addresses: $SITE_PUBLIC_SCHEME://<site-name>.$PUBLIC_HOST"
echo "Cloudflare Tunnel public hostname required:"
echo "  *.$PUBLIC_HOST -> http://localhost:8080"
if [[ $SITE_PUBLIC_SCHEME == http ]]; then
    echo "Site links use HTTP until wildcard HTTPS is configured for *.$PUBLIC_HOST."
fi
echo
