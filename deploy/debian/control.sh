#!/usr/bin/env bash
# webmanager-control: the few root-only changes a super admin can ask for on
# the System page (or from another server in the group), so nobody needs a
# shell for them. Started by webmanager-control.path when the web app writes a
# request file. Two requests exist:
#
# - requests/apps: turning app hosting on or off. One word ("on" or "off");
#   anything else is ignored. WEBMANAGER_DASHBOARD_APP_CONTROL=0 in
#   /etc/webmanager/webmanager.env keeps it a shell-only decision (then use:
#   sudo webmanager-enable-apps).
# - requests/settings: saving settings to /etc/webmanager/webmanager.env. Only
#   the settings listed in webmanager/settings_editor.py, checked again here by
#   that same (root-owned) file. WebManager is restarted and the old file put
#   back if it doesn't come up. WEBMANAGER_DASHBOARD_SETTINGS=0 turns it off.
#
# The web app can only ask; this decides.
set -Eeuo pipefail

STATE_DIR=/var/lib/webmanager-updater
REQUEST_FILE=$STATE_DIR/requests/apps
SETTINGS_REQUEST_FILE=$STATE_DIR/requests/settings
SETTINGS_EDITOR=/opt/webmanager/webmanager/settings_editor.py
SETTINGS_BACKUP=$STATE_DIR/webmanager.env.before-settings
STATUS_FILE=$STATE_DIR/control-status.json
LOG_FILE=$STATE_DIR/control.log
ENV_FILE=/etc/webmanager/webmanager.env
ENABLE_APPS=/usr/local/sbin/webmanager-enable-apps

write_status() {
    # write_status STATE ACTION MESSAGE [DETAIL]
    local temporary
    temporary=$(mktemp "$STATE_DIR/control-status.XXXXXX")
    python3 - "$1" "$2" "$3" "${4:-}" >"$temporary" <<'PY'
import json, sys, time
state, action, message, detail = sys.argv[1:5]
print(json.dumps({
    "state": state, "action": action, "message": message,
    "detail": detail[-4000:] or None,
    "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
}))
PY
    chown root:webmanager "$temporary"
    chmod 0640 "$temporary"
    mv -f "$temporary" "$STATUS_FILE"
}

env_setting() {
    sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n 1
}

webmanager_answers() {
    local host port
    host=$(env_setting WEBMANAGER_HOST)
    port=$(env_setting WEBMANAGER_PORT)
    case "${host:-127.0.0.1}" in
        0.0.0.0 | "::") host=127.0.0.1 ;;
        *:*) host="[$host]" ;;
    esac
    for _ in {1..45}; do
        if systemctl is-active --quiet webmanager \
            && python3 -c "import urllib.request; urllib.request.urlopen('http://${host:-127.0.0.1}:${port:-5000}/healthz', timeout=2).read()" \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

apply_settings() {
    local request changed
    request=$(mktemp "$STATE_DIR/settings-request.XXXXXX")
    head -c 16385 "$SETTINGS_REQUEST_FILE" >"$request"
    rm -f "$SETTINGS_REQUEST_FILE"
    if [[ $(env_setting WEBMANAGER_DASHBOARD_SETTINGS) == "0" ]]; then
        rm -f "$request"
        write_status error settings \
            "Changing settings from the dashboard is turned off on this server (WEBMANAGER_DASHBOARD_SETTINGS=0). Edit /etc/webmanager/webmanager.env on the server."
        return 1
    fi
    if [[ ! -f $SETTINGS_EDITOR ]]; then
        rm -f "$request"
        write_status error settings "$SETTINGS_EDITOR is missing. Re-run setup.sh on this server."
        return 1
    fi
    write_status running settings "Saving the settings and restarting WebManager."
    cp -p "$ENV_FILE" "$SETTINGS_BACKUP"
    if ! changed=$(python3 -I "$SETTINGS_EDITOR" apply "$request" "$ENV_FILE" 2>"$LOG_FILE"); then
        rm -f "$request"
        write_status error settings "Nothing was changed: $(head -c 500 "$LOG_FILE")"
        return 1
    fi
    rm -f "$request"
    if [[ -z $changed ]]; then
        write_status done settings "The settings were already like that; nothing changed."
        return 0
    fi
    systemctl restart webmanager >>"$LOG_FILE" 2>&1 || true
    if webmanager_answers; then
        write_status done settings "Saved ${changed}. WebManager restarted."
        return 0
    fi
    local journal
    journal=$(journalctl -u webmanager -n 30 --no-pager 2>/dev/null || true)
    cp -p "$SETTINGS_BACKUP" "$ENV_FILE"
    systemctl restart webmanager >>"$LOG_FILE" 2>&1 || true
    webmanager_answers || true
    write_status error settings \
        "WebManager didn't start with the new settings (${changed}), so the previous settings were put back." \
        "$journal"
    return 1
}

# Only a plain file the web app wrote; never follow a link to somewhere else.
SETTINGS_RESULT=0
if [[ -L $SETTINGS_REQUEST_FILE ]]; then
    rm -f "$SETTINGS_REQUEST_FILE"
elif [[ -f $SETTINGS_REQUEST_FILE ]]; then
    apply_settings || SETTINGS_RESULT=1
fi

if [[ ! -f $REQUEST_FILE || -L $REQUEST_FILE ]]; then
    rm -f "$REQUEST_FILE"
    exit "$SETTINGS_RESULT"
fi
REQUEST=$(head -c 16 "$REQUEST_FILE" | tr -d '[:space:]')
rm -f "$REQUEST_FILE"

case $REQUEST in
    on) ACTION=apps_on ;;
    off) ACTION=apps_off ;;
    *)
        write_status error unknown "The request was not understood, so nothing was changed."
        exit 1
        ;;
esac

ALLOWED=$(sed -n 's/^WEBMANAGER_DASHBOARD_APP_CONTROL=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
if [[ ${ALLOWED:-1} == "0" ]]; then
    write_status error "$ACTION" \
        "Turning app hosting on or off from the dashboard is switched off on this server (WEBMANAGER_DASHBOARD_APP_CONTROL=0). On the server run: sudo webmanager-enable-apps"
    exit 1
fi
if [[ ! -x $ENABLE_APPS ]]; then
    write_status error "$ACTION" "$ENABLE_APPS is missing. Re-run setup.sh on this server."
    exit 1
fi

if [[ $ACTION == apps_on ]]; then
    write_status running "$ACTION" "Turning app hosting on: installing Docker if needed, then restarting WebManager."
    arguments=(--yes)
else
    write_status running "$ACTION" "Turning app hosting off and restarting WebManager."
    arguments=(--disable)
fi

if "$ENABLE_APPS" "${arguments[@]}" >"$LOG_FILE" 2>&1; then
    if [[ $ACTION == apps_on ]]; then
        write_status done "$ACTION" "App hosting is on." "$(tail -n 20 "$LOG_FILE")"
    else
        write_status done "$ACTION" "App hosting is off. Existing app containers were left in place." "$(tail -n 20 "$LOG_FILE")"
    fi
else
    write_status error "$ACTION" \
        "Turning app hosting $REQUEST failed. The details below say why; on the server, sudo webmanager-enable-apps shows the same steps." \
        "$(tail -n 40 "$LOG_FILE")"
    exit 1
fi
