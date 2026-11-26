#!/usr/bin/env bash
# webmanager-control: the few root-only changes a super admin can ask for on
# the System page (or from another server in the group), so nobody needs a
# shell for them. Started by webmanager-control.path when the web app writes a
# request file; today the only request is turning app hosting on or off.
#
# The web app can only ask; this decides. A request is one word ("on" or
# "off") and anything else is ignored. Set WEBMANAGER_DASHBOARD_APP_CONTROL=0
# in /etc/webmanager/webmanager.env to keep app hosting a shell-only decision
# (then use: sudo webmanager-enable-apps).
set -Eeuo pipefail

STATE_DIR=/var/lib/webmanager-updater
REQUEST_FILE=$STATE_DIR/requests/apps
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

# Only a plain file the web app wrote; never follow a link to somewhere else.
if [[ ! -f $REQUEST_FILE || -L $REQUEST_FILE ]]; then
    rm -f "$REQUEST_FILE"
    exit 0
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
