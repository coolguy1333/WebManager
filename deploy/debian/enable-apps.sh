#!/usr/bin/env bash
# Turn WebManager app hosting (containers) on or off.
#
#   sudo bash deploy/debian/enable-apps.sh            # enable (asks first)
#   sudo bash deploy/debian/enable-apps.sh --yes      # enable without asking
#   sudo bash deploy/debian/enable-apps.sh --disable  # turn it off again
#
# Enabling installs Docker if needed, lets the webmanager service use it,
# blocks containers from reaching the cloud metadata address and your LAN
# (internet access only), and sets WEBMANAGER_APPS_ENABLED=1. See
# docs/APP_HOSTING.md.
set -Eeuo pipefail

CONFIG_FILE=/etc/webmanager/webmanager.env
DROPIN_DIR=/etc/systemd/system/webmanager.service.d
DROPIN_FILE=$DROPIN_DIR/apps.conf
FIREWALL_UNIT=/etc/systemd/system/webmanager-app-firewall.service

# Destinations app containers may not reach: the cloud metadata address and
# all private/link-local ranges (RFC 1918 + RFC 3927). Internet addresses are
# left open. If an app legitimately needs one specific LAN host, add a higher
# priority ACCEPT rule for it, e.g.:
#   iptables -I DOCKER-USER -d 192.168.1.50/32 -j ACCEPT
BLOCKED_RANGES="169.254.169.254/32 169.254.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"

ASSUME_YES=0
DISABLE=0
for argument in "$@"; do
    case $argument in
        --yes | -y) ASSUME_YES=1 ;;
        --disable) DISABLE=1 ;;
        -h | --help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $argument" >&2
            exit 2
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

if [[ ! -f $CONFIG_FILE ]]; then
    echo "WebManager is not installed ($CONFIG_FILE is missing). Run setup.sh first." >&2
    exit 1
fi

set_env() {
    local key=$1 value=$2 temp
    temp=$(mktemp)
    grep -v "^${key}=" "$CONFIG_FILE" >"$temp" || true
    printf '%s=%s\n' "$key" "$value" >>"$temp"
    install -o root -g webmanager -m 0640 "$temp" "$CONFIG_FILE"
    rm -f "$temp"
}

if [[ $DISABLE -eq 1 ]]; then
    set_env WEBMANAGER_APPS_ENABLED 0
    rm -f "$DROPIN_FILE"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart webmanager
    echo "App hosting is off. Existing app containers were not removed;"
    echo "stop or delete apps in WebManager first if you want them gone."
    echo "Docker itself was left installed."
    exit 0
fi

cat <<'EOF'
App hosting runs other people's code in containers on this server.

  * WebManager's service account will be added to the "docker" group.
    Access to Docker is equivalent to root on this machine, so anyone who
    can take over WebManager could take over the server.
  * Containers are hardened (read-only filesystem, no capabilities,
    memory/CPU/process limits). They can reach the internet but not the
    cloud metadata address or your LAN; only the published port on
    127.0.0.1 connects them to WebManager's Nginx.

Use a dedicated VM or LXC container for WebManager if that is a concern,
and only grant "Host apps" to people you trust.
EOF
if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "Enable app hosting? [y/N] " answer
    if [[ ! $answer =~ ^[Yy]$ ]]; then
        echo "Nothing changed."
        exit 0
    fi
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Installing Docker (docker.io)…"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
fi
systemctl enable --now docker

getent group docker >/dev/null || groupadd --system docker

mkdir -p "$DROPIN_DIR"
cat >"$DROPIN_FILE" <<'EOF'
# Added by enable-apps.sh: lets WebManager talk to the Docker daemon.
[Service]
SupplementaryGroups=docker
EOF

# Block containers from the cloud metadata service (it can hand out
# credentials for the whole machine on AWS/GCP/Azure/etc.) and from every
# private/link-local address range, so apps can reach the internet but not
# your LAN or the host's other services. Each range gets its own idempotent
# insert/delete so re-running this script or restarting the unit is safe.
{
    echo "[Unit]"
    echo "Description=WebManager app firewall (internet-only containers)"
    echo "After=docker.service"
    echo "PartOf=docker.service"
    echo
    echo "[Service]"
    echo "Type=oneshot"
    echo "RemainAfterExit=yes"
    for range in $BLOCKED_RANGES; do
        printf 'ExecStart=/bin/sh -c '\''iptables -C DOCKER-USER -d %s -j DROP 2>/dev/null || iptables -I DOCKER-USER -d %s -j DROP'\''\n' "$range" "$range"
    done
    for range in $BLOCKED_RANGES; do
        printf 'ExecStop=/bin/sh -c '\''iptables -D DOCKER-USER -d %s -j DROP 2>/dev/null || true'\''\n' "$range"
    done
    echo
    echo "[Install]"
    echo "WantedBy=docker.service"
} >"$FIREWALL_UNIT"

set_env WEBMANAGER_APPS_ENABLED 1
systemctl daemon-reload
# "restart" (not "enable --now") so re-running this script also re-applies
# the rule set for anyone upgrading from an older version of this unit that
# only blocked the metadata address; a already-active oneshot unit would
# otherwise skip ExecStart on a plain start.
systemctl enable webmanager-app-firewall.service
if ! systemctl restart webmanager-app-firewall.service; then
    echo "Warning: could not add the app firewall rules. See docs/APP_HOSTING.md." >&2
fi
systemctl restart webmanager

if runuser -u webmanager -g webmanager -G docker -- docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "App hosting is on. Check System in WebManager: it should say \"App hosting: On\"."
    echo "Then give people access with People & access > Teams > \"App hosts\"."
else
    echo "App hosting was enabled, but the webmanager account can't reach Docker yet." >&2
    echo "Check: systemctl status docker; getent group docker" >&2
    exit 1
fi
