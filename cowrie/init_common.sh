#!/usr/bin/env bash
# Shared validation and notification checks for the two Cowrie init scripts.
set -Eeuo pipefail
umask 077

if (( $# < 1 || $# > 2 )); then
    echo "Usage: $0 minute|persistent [--dry-run]" >&2
    exit 2
fi
mode="$1"
dry_run=false
if [[ "$mode" != minute && "$mode" != persistent ]]; then
    echo "Usage: $0 minute|persistent [--dry-run]" >&2
    exit 2
fi
if (( $# == 2 )); then
    if [[ "$2" == --dry-run ]]; then
        dry_run=true
    else
        echo "Usage: $0 minute|persistent [--dry-run]" >&2
        exit 2
    fi
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if "$dry_run"; then
    exec /bin/bash "$SCRIPT_DIR/install_cron.sh" --mode "$mode" --dry-run
fi

for command_name in curl python3 crontab; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done
if crontab -l 2>/dev/null | grep -Eq '# (cowrie-sharded-monitor|redteam-sharded-scan)([[:space:]]|$)'; then
    echo "ERROR: deactivate and remove the sharded schedule on all three nodes before selecting a legacy mode." >&2
    exit 1
fi
if [[ -L "$ENV_FILE" || ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE must be a readable regular file. Configure the notification URLs before initializing." >&2
    exit 1
fi

discord_url=''
healthchecks_url=''
redteam_healthchecks_url=''
while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
        DISCORD_WEBHOOK_URL=*) discord_url="${line#DISCORD_WEBHOOK_URL=}" ;;
        HEALTHCHECKS_PING_URL=*) healthchecks_url="${line#HEALTHCHECKS_PING_URL=}" ;;
        REDTEAM_HEALTHCHECKS_PING_URL=*) redteam_healthchecks_url="${line#REDTEAM_HEALTHCHECKS_PING_URL=}" ;;
    esac
done < "$ENV_FILE"

if [[ ! "$discord_url" =~ ^https://discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: DISCORD_WEBHOOK_URL is missing or invalid in $ENV_FILE; configure it with redteam_scan_flag.sh --init-discord." >&2
    exit 1
fi
if [[ ! "$healthchecks_url" =~ ^https://hc-ping\.com/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
    echo "ERROR: HEALTHCHECKS_PING_URL is missing or invalid in $ENV_FILE." >&2
    exit 1
fi
if [[ ! "$redteam_healthchecks_url" =~ ^https://hc-ping\.com/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
    echo "ERROR: REDTEAM_HEALTHCHECKS_PING_URL is missing or invalid in $ENV_FILE; configure it with redteam_scan_flag.sh --init-cron." >&2
    exit 1
fi

if [[ "$mode" == persistent ]]; then
    if [[ ! -f "$SCRIPT_DIR/persistent.sh" ]]; then
        echo "ERROR: persistent checker is missing: $SCRIPT_DIR/persistent.sh" >&2
        exit 1
    fi
fi

ping_healthchecks() {
    local url="$1" label="$2" status
    status="$(curl --silent --show-error --connect-timeout 3 --max-time 7 \
        --output /dev/null --write-out '%{http_code}' --config - <<EOF
url = "$url"
EOF
)" || {
        echo "ERROR: $label Healthchecks test ping failed." >&2
        return 1
    }
    if [[ "$status" != 2?? ]]; then
        echo "ERROR: $label Healthchecks returned HTTP $status." >&2
        return 1
    fi
}

send_discord_test() {
    local payload status
    payload="$(python3 -c 'import json, sys; print(json.dumps({"content": sys.argv[1]}))' \
        "Cowrie $mode monitor initialization test: Discord notifications are working.")" || return 1
    status="$(curl --silent --show-error --connect-timeout 3 --max-time 7 \
        --output /dev/null --write-out '%{http_code}' --request POST \
        --header 'Content-Type: application/json' --data "$payload" --config - <<EOF
url = "$discord_url"
EOF
)" || {
        echo "ERROR: Discord test notification failed." >&2
        return 1
    }
    if [[ "$status" != 2?? ]]; then
        echo "ERROR: Discord test notification returned HTTP $status." >&2
        return 1
    fi
}

ping_healthchecks "$healthchecks_url" Cowrie
ping_healthchecks "$redteam_healthchecks_url" Redteam
send_discord_test
if [[ "$mode" == persistent ]]; then
    cron_snapshot_dir="$(mktemp -d)"
    trap 'rm -rf -- "$cron_snapshot_dir"' EXIT
    if ! crontab -l > "$cron_snapshot_dir/current" 2> "$cron_snapshot_dir/error"; then
        if ! grep -qi 'no crontab' "$cron_snapshot_dir/error"; then
            cat "$cron_snapshot_dir/error" >&2
            exit 1
        fi
        : > "$cron_snapshot_dir/current"
    fi
    prior_persistent=false
    if awk '!/^[[:space:]]*#/ && /# cowrie-persistent-monitor([[:space:]]|$)/ { found=1 } END { exit !found }' \
        "$cron_snapshot_dir/current"; then
        prior_persistent=true
    fi

    rollback_new_master() {
        if "$prior_persistent"; then
            return 0
        fi
        if ! /bin/bash "$SCRIPT_DIR/persistent.sh" --stop; then
            echo "ERROR: failed to stop the new persistent SSH master during rollback." >&2
            return 1
        fi
    }

    if ! /bin/bash "$SCRIPT_DIR/persistent.sh" --initialize; then
        echo "ERROR: persistent SSH initialization failed; existing cron was left unchanged." >&2
        rollback_new_master || true
        exit 1
    fi
    if ! /bin/bash "$SCRIPT_DIR/install_cron.sh" --mode persistent; then
        echo "ERROR: persistent cron installation failed." >&2
        rollback_new_master || true
        exit 1
    fi
else
    if [[ ! -f "$SCRIPT_DIR/persistent.sh" ]]; then
        echo "ERROR: persistent checker is missing: $SCRIPT_DIR/persistent.sh" >&2
        exit 1
    fi
    if ! /bin/bash "$SCRIPT_DIR/persistent.sh" --stop; then
        echo "ERROR: could not stop the persistent SSH master; existing cron was left unchanged." >&2
        exit 1
    fi
    /bin/bash "$SCRIPT_DIR/install_cron.sh" --mode minute
fi
echo "Cowrie $mode initialization complete; notification tests passed."
