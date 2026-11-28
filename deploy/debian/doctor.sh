#!/usr/bin/env bash
# sudo webmanager-doctor: check that everything WebManager depends on works,
# and say what to do about anything that doesn't. Works even when the web page
# or sign-in is broken.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

# --- load_env_file: begin (tests/test_settings_editor.py runs this function) ---
# Export the settings the way systemd's EnvironmentFile= hands them to the
# service, without ever running the file as shell code: `source` would
# execute anything in it (a value like a@b.co&reboot) as root. Blank lines and
# comments are skipped, keys must look like NAME_1, and one pair of matching
# outer quotes is removed. Nothing in a value is expanded.
load_env_file() {
    local line key value
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        # Leading and trailing whitespace, as systemd ignores it.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z $line || $line == "#"* || $line == ";"* ]] && continue
        [[ $line == *=* ]] || continue
        key=${line%%=*}
        value=${line#*=}
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        [[ $key =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        if [[ ${#value} -ge 2 && ( ( $value == \"*\" ) || ( $value == \'*\' ) ) ]]; then
            value=${value:1:${#value}-2}
        fi
        export "$key=$value"
    done <"$1"
}
# --- load_env_file: end ---

load_env_file /etc/webmanager/webmanager.env
export HOME=/var/lib/webmanager

echo "Service: $(systemctl is-active webmanager 2>/dev/null || true)"
cd /opt/webmanager
# Check with the same extra groups the service has (app hosting adds "docker"),
# or the container check would fail here while working in the service.
GROUP_ARGS=()
for group in $(systemctl show webmanager -p SupplementaryGroups --value 2>/dev/null || true); do
    GROUP_ARGS+=(-G "$group")
done
if [[ ${#GROUP_ARGS[@]} -gt 0 ]]; then
    GROUP_ARGS=(-g webmanager "${GROUP_ARGS[@]}")
fi
exec runuser -u webmanager ${GROUP_ARGS[@]+"${GROUP_ARGS[@]}"} --preserve-environment -- \
    /opt/webmanager/.venv/bin/python /opt/webmanager/run.py doctor
