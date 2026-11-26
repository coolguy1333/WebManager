#!/usr/bin/env bash
# sudo webmanager-promote [--yes]
#
# Make this replica the primary: it stops mirroring, starts its apps and
# accepts changes itself, using the data it last copied. Use it when the
# primary is lost or being retired. It needs no sign-in, so it works even when
# the dashboard can't be reached. To undo it, join a primary again with the
# command from that server's System > Servers > Add a server.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

ENV_FILE=/etc/webmanager/webmanager.env
PRIMARY=$(sed -n 's/^WEBMANAGER_REPLICA_OF=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
DATA_DIR=$(sed -n 's/^WEBMANAGER_DATA_DIR=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
DATA_DIR=${DATA_DIR:-/var/lib/webmanager}

if [[ -z $PRIMARY ]]; then
    echo "This server is not a replica, so there is nothing to promote."
    exit 0
fi
if [[ -e $DATA_DIR/promoted ]]; then
    echo "This server was already promoted and is running as the primary."
    exit 0
fi
if [[ ${1:-} != "--yes" ]]; then
    echo "This server mirrors $PRIMARY."
    echo "Promoting it makes it the primary. Only do this if $PRIMARY is down or"
    echo "being retired: two primaries would each accept changes the other never sees."
    read -r -p "Promote this server now? [y/N] " answer
    [[ $answer == [yY]* ]] || { echo "Nothing was changed."; exit 1; }
fi

# What it took over from, and when: the dashboard shows a "failed over"
# warning with this until someone confirms they have checked things.
python3 -c 'import json, sys, time; print(json.dumps({"from": sys.argv[1], "by": "webmanager-promote", "at": int(time.time())}))' \
    "$PRIMARY" >"$DATA_DIR/promoted"
rm -f "$DATA_DIR/promoted-acknowledged"
chown webmanager:webmanager "$DATA_DIR/promoted"
chmod 0640 "$DATA_DIR/promoted"
systemctl restart webmanager
echo "This server is now the primary. Check it with: sudo webmanager-doctor"
echo "If $PRIMARY comes back, take it offline or join it to this server as a replica."
