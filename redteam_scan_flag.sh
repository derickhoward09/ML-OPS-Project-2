#!/usr/bin/env bash
#
# CS553 case study 2 — read-only SSH access check.
# By default, tries the configured key against ports 22001-22025 on the class
# VM. Pass -test (or --test/-t) to check the configured key and try TEST_NODE.
# Full scans are allowed only from September 29, 2026 noon through October 1,
# 2026 noon, America/New_York time. Test mode may be used at any time.
# Each node number maps to port 22000 + its number. The script issues only a
# remote `true` command; it does not install files or change remote settings.
# Results are appended to redteam_scan.log beside this script.

set -euo pipefail

HOST="paffenroth-23.dyn.wpi.edu"
SSH_USER="student-admin"
KEY="$HOME/.ssh/mlops/student-admin_key"
TIMEZONE="America/New_York"
NTP_SERVER="time.nist.gov"
NTP_TOLERANCE_SECONDS=60
# Node/group to check in -t mode. Change this to a value from 1 through 25.
TEST_NODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/redteam_scan.log"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/redteam-scan"
ENV_FILE="$SCRIPT_DIR/.env"
NOTIFICATION_STATE_FILE="$CONFIG_DIR/sent_notifications"
LAST_SCAN_FILE="$CONFIG_DIR/last_scan"

test_mode=false
init_discord=false
for arg in "$@"; do
    case "$arg" in
        -test|--test|-t)
            test_mode=true
            ;;
        --init-discord)
            init_discord=true
            ;;
        -h|--help)
            echo "Usage: $0 [--init-discord | -test|--test|-t]"
            echo "  --init-discord  Securely save a Discord webhook for notifications."
            echo "  -test            Check the configured key, try TEST_NODE, and send a Discord test."
            echo "  Full scans check nodes 1-25. Scheduled Discord pushes use America/New_York:"
            echo "    Sep 29, 2026 at 1 PM and 7 PM; Sep 30 at 10 AM;"
            echo "    Oct 1 at 11 AM and noon, with the latest completed scan results."
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            echo "Usage: $0 [--init-discord | -test|--test|-t]" >&2
            exit 2
            ;;
    esac
done

if "$init_discord" && "$test_mode"; then
    echo "ERROR: --init-discord cannot be combined with test mode." >&2
    exit 2
fi

if "$test_mode"; then
    if ! [[ "$TEST_NODE" =~ ^[0-9]+$ ]]; then
        echo "ERROR: TEST_NODE must be a group number from 1 through 25." >&2
        exit 2
    fi
    TEST_NODE_NUMBER=$((10#$TEST_NODE))
    if ((TEST_NODE_NUMBER < 1 || TEST_NODE_NUMBER > 25)); then
        echo "ERROR: TEST_NODE must be a group number from 1 through 25." >&2
        exit 2
    fi
    TEST_NODE="$TEST_NODE_NUMBER"
fi

ensure_config_dir() {
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
}

valid_webhook_url() {
    [[ "$1" =~ ^https://discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$ ]]
}

initialize_discord() {
    local webhook="" temp_file=""
    if [[ ! -t 0 ]]; then
        echo "ERROR: --init-discord needs an interactive terminal." >&2
        return 1
    fi

    printf "Discord webhook URL (input hidden): "
    IFS= read -r -s webhook || {
        printf '\n'
        echo "ERROR: could not read the webhook URL." >&2
        return 1
    }
    printf '\n'

    if ! valid_webhook_url "$webhook"; then
        unset webhook
        echo "ERROR: enter a Discord webhook URL from discord.com or discordapp.com." >&2
        return 1
    fi

    if [[ -L "$ENV_FILE" || ( -e "$ENV_FILE" && ! -f "$ENV_FILE" ) ]]; then
        unset webhook
        echo "ERROR: $ENV_FILE must be a regular file, not a symlink." >&2
        return 1
    fi

    if ! temp_file="$(umask 077; mktemp "$SCRIPT_DIR/.env.XXXXXX")"; then
        unset webhook
        echo "ERROR: could not create a private .env file beside the script." >&2
        return 1
    fi

    if [[ -e "$ENV_FILE" ]] && ! awk 'index($0, "DISCORD_WEBHOOK_URL=") != 1' "$ENV_FILE" > "$temp_file"; then
        rm -f "$temp_file"
        unset webhook
        echo "ERROR: could not preserve the existing .env contents." >&2
        return 1
    fi
    if ! printf 'DISCORD_WEBHOOK_URL=%s\n' "$webhook" >> "$temp_file" || ! chmod 600 "$temp_file" || ! mv -f "$temp_file" "$ENV_FILE"; then
        rm -f "$temp_file"
        unset webhook
        echo "ERROR: could not save the Discord webhook to $ENV_FILE." >&2
        return 1
    fi

    unset webhook
    echo "Discord webhook saved to $ENV_FILE with mode 600."
}

load_discord_webhook() {
    local candidate="" line=""
    if [[ -L "$ENV_FILE" || ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
        return 1
    fi
    if ! chmod 600 "$ENV_FILE"; then
        return 1
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == DISCORD_WEBHOOK_URL=* ]]; then
            candidate="${line#DISCORD_WEBHOOK_URL=}"
        fi
    done < "$ENV_FILE"
    if ! valid_webhook_url "$candidate"; then
        unset candidate
        return 1
    fi
    DISCORD_WEBHOOK="$candidate"
    unset candidate
}

send_discord_message() {
    local message="$1"
    local payload http_status

    if ! load_discord_webhook; then
        echo "ERROR: Discord webhook is missing or invalid. Run $0 --init-discord." >&2
        return 1
    fi
    if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: Discord notifications require curl and python3." >&2
        unset DISCORD_WEBHOOK
        return 1
    fi

    if ! payload="$(python3 -c 'import json, sys; print(json.dumps({"content": sys.argv[1]}))' "$message" 2>/dev/null)"; then
        echo "ERROR: could not encode the Discord message." >&2
        unset DISCORD_WEBHOOK
        return 1
    fi

    # Read the secret URL through curl's stdin config so it is not exposed in argv.
    if ! http_status="$(curl --silent --show-error --connect-timeout 8 --max-time 15 --output /dev/null --write-out '%{http_code}' --request POST --header 'Content-Type: application/json' --data "$payload" --config - 2>/dev/null <<EOF
url = "$DISCORD_WEBHOOK"
EOF
)"; then
        echo "ERROR: Discord webhook request failed." >&2
        unset DISCORD_WEBHOOK
        return 1
    fi
    unset DISCORD_WEBHOOK

    if [[ "$http_status" != 2?? ]]; then
        echo "ERROR: Discord returned HTTP status $http_status." >&2
        return 1
    fi
}

if "$init_discord"; then
    initialize_discord
    exit $?
fi

record_ntp_failure() {
    local reason="$1"
    local timestamp

    reason="$(printf '%s' "$reason" | tr '\r\n' '  ')"
    timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "WARNING: NTP clock check unavailable ($reason); continuing test mode." >&2
    printf '%s | NTP check warning: unavailable (%s)\n' "$timestamp" "$reason" >> "$LOG_FILE"
}

check_ntp_clock() {
    local result timestamp status offset abs_offset direction local_utc ntp_utc round_trip reason

    if ! command -v python3 >/dev/null 2>&1; then
        record_ntp_failure "python3 is required for the read-only NTP query"
        return 0
    fi

    if ! result="$(python3 "$SCRIPT_DIR/scripts/ntp_clock_check.py" \
        "$NTP_SERVER" "$NTP_TOLERANCE_SECONDS" 2>&1)"; then
        record_ntp_failure "$result"
        return 0
    fi

    IFS=$'\t' read -r status offset abs_offset direction local_utc ntp_utc round_trip <<< "$result"
    if [[ "$status" == "warning" ]]; then
        timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
        echo "WARNING: local clock is $direction NIST by ${abs_offset}s (over ${NTP_TOLERANCE_SECONDS}s)." >&2
        echo "  Local UTC: $local_utc; NIST UTC: $ntp_utc; NTP round trip: ${round_trip}s."
        printf '%s | NTP clock warning: local %s NIST by %ss | local_utc=%s | nist_utc=%s | round_trip=%ss\n' \
            "$timestamp" "$direction" "$offset" "$local_utc" "$ntp_utc" "$round_trip" >> "$LOG_FILE"
    else
        echo "NTP check: local clock is within ${NTP_TOLERANCE_SECONDS}s of NIST (offset ${offset}s)."
    fi
}

timestamp_to_epoch() {
    local stamp="$1" date_part time_part parsed
    if parsed="$(TZ="$TIMEZONE" date -j -f '%Y%m%d%H%M' "$stamp" '+%s' 2>/dev/null)"; then
        printf '%s\n' "$parsed"
        return 0
    fi

    date_part="${stamp:0:4}-${stamp:4:2}-${stamp:6:2}"
    time_part="${stamp:8:2}:${stamp:10:2}:00"
    TZ="$TIMEZONE" date -d "$date_part $time_part" '+%s' 2>/dev/null
}

store_latest_scan() {
    local scan_time="$1" scan_count="$2" scan_nodes="$3" temp_file
    ensure_config_dir
    temp_file="$LAST_SCAN_FILE.$$"
    (umask 077; printf '%s|%s|%s\n' "$scan_time" "$scan_count" "$scan_nodes" > "$temp_file")
    chmod 600 "$temp_file"
    mv -f "$temp_file" "$LAST_SCAN_FILE"
}

record_notification_issue() {
    local event_id="$1" timestamp
    timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
    printf '%s | Discord notification failed for event %s; retrying for up to 10 minutes.\n' \
        "$timestamp" "$event_id" >> "$LOG_FILE"
}

send_scheduled_notifications() {
    local now_epoch event_epoch age event_id event_label scan_time scan_count scan_nodes message timestamp
    local event_ids=(202609291300 202609291900 202609301000 202610011100 202610011200)
    local event_times=(202609291300 202609291900 202609301000 202610011100 202610011200)
    local event_labels=(
        "1 PM check-in on September 29"
        "7 PM check-in on September 29"
        "10 AM check-in on September 30"
        "1 hour before the October 1 noon scan deadline"
        "October 1 noon scan deadline"
    )

    now_epoch="$(TZ="$TIMEZONE" date '+%s')"
    ensure_config_dir

    for ((event_index = 0; event_index < ${#event_ids[@]}; event_index++)); do
        event_id="${event_ids[$event_index]}"
        if [[ -r "$NOTIFICATION_STATE_FILE" ]] && grep -Fqx -- "$event_id" "$NOTIFICATION_STATE_FILE"; then
            continue
        fi

        if ! event_epoch="$(timestamp_to_epoch "${event_times[$event_index]}")"; then
            continue
        fi
        age=$((now_epoch - event_epoch))
        if ((age < 0 || age > 600)); then
            continue
        fi

        scan_time="unavailable"
        scan_count="unavailable"
        scan_nodes="unavailable"
        if [[ -r "$LAST_SCAN_FILE" ]]; then
            IFS='|' read -r scan_time scan_count scan_nodes < "$LAST_SCAN_FILE" || true
        fi

        event_label="${event_labels[$event_index]}"
        message="Redteam timeline: $event_label. Latest full scan at $scan_time: $scan_count/25 groups authenticated (nodes: $scan_nodes)."
        if ! send_discord_message "$message"; then
            record_notification_issue "$event_id"
            continue
        fi

        (umask 077; printf '%s\n' "$event_id" >> "$NOTIFICATION_STATE_FILE")
        timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
        printf '%s | Discord notification sent for event %s.\n' "$timestamp" "$event_id" >> "$LOG_FILE"
    done
}

if "$test_mode"; then
    if ! load_discord_webhook; then
        echo "ERROR: Discord webhook is missing or invalid. Run $0 --init-discord." >&2
        exit 1
    fi
    unset DISCORD_WEBHOOK
    check_ntp_clock
fi

if ! "$test_mode"; then
    window_start="20260929120000"
    window_end="20261001120000"
    now="$(TZ="$TIMEZONE" date '+%Y%m%d%H%M%S')"
    if [[ "$now" < "$window_start" ]]; then
        echo "ERROR: full scans are allowed only from September 29, 2026 noon through October 1, 2026 noon (America/New_York)." >&2
        exit 1
    fi
    if [[ "$now" > "$window_end" || "$now" == "$window_end" ]]; then
        # The noon deadline push runs outside the scan window and uses the
        # latest result persisted by a scan that completed before noon.
        send_scheduled_notifications
        exit 0
    fi
fi

if [ ! -r "$KEY" ]; then
    if "$test_mode"; then
        if ! send_discord_message "Redteam test notification: node $TEST_NODE SSH test could not run because the configured key is unreadable."; then
            echo "ERROR: Discord test notification could not be sent." >&2
            exit 1
        fi
    else
        send_scheduled_notifications
    fi
    echo "ERROR: cannot read SSH key: $KEY" >&2
    exit 1
fi

if "$test_mode"; then
    if [ ! -f "$KEY" ] || [ ! -s "$KEY" ]; then
        if ! send_discord_message "Redteam test notification: node $TEST_NODE SSH test could not run because the configured key is missing or empty."; then
            echo "ERROR: Discord test notification could not be sent." >&2
            exit 1
        fi
        echo "ERROR: expected a non-empty key file at: $KEY" >&2
        exit 1
    fi
    echo "Test mode: SSH key is present and readable at $KEY; checking node $TEST_NODE"
    ports=("$((22000 + TEST_NODE))")
else
    # Build nodes 1-25's corresponding SSH ports, then shuffle them in place.
    ports=()
    for node in {1..25}; do
        ports+=("$((22000 + node))")
    done

    for ((i = ${#ports[@]} - 1; i > 0; i--)); do
        j=$((RANDOM % (i + 1)))
        temp="${ports[$i]}"
        ports[$i]="${ports[$j]}"
        ports[$j]="$temp"
    done
fi

ssh_opts=(
    -T
    -i "$KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o ConnectTimeout=5
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
)

success_nodes=()
success_node_ids=()
for ((index = 0; index < ${#ports[@]}; index++)); do
    port="${ports[$index]}"
    node=$((port - 22000))

    printf 'Trying node %s on port %s... ' "$node" "$port"
    if ssh "${ssh_opts[@]}" -p "$port" "${SSH_USER}@${HOST}" true >/dev/null 2>&1; then
        echo "login succeeded."
        success_nodes+=("node $node (port $port)")
        success_node_ids+=("$node")
    else
        echo "login failed."
    fi

    if ((index < ${#ports[@]} - 1)); then
        delay=$((RANDOM % 3 + 1))
        sleep "$delay"
    fi
done

if [ "${#success_nodes[@]}" -eq 0 ]; then
    success_summary="none"
else
    success_summary="${success_nodes[0]}"
    for ((index = 1; index < ${#success_nodes[@]}; index++)); do
        success_summary+=", ${success_nodes[$index]}"
    done
fi

timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
printf '%s | authenticated: %s\n' "$timestamp" "$success_summary" >> "$LOG_FILE"

success_count="${#success_nodes[@]}"
if [ "$success_count" -eq 0 ]; then
    success_node_ids_summary="none"
else
    success_node_ids_summary="${success_node_ids[0]}"
    for ((index = 1; index < ${#success_node_ids[@]}; index++)); do
        success_node_ids_summary+=", ${success_node_ids[$index]}"
    done
fi

echo
echo "Authenticated: $success_summary"
echo "Results appended to $LOG_FILE"

if "$test_mode"; then
    if [ "$success_count" -eq 0 ]; then
        test_result="failed"
    else
        test_result="succeeded"
    fi
    if ! send_discord_message "Redteam test notification: node $TEST_NODE SSH login test $test_result."; then
        echo "ERROR: Discord test notification could not be sent." >&2
        exit 1
    fi
    echo "Discord test notification sent."
else
    store_latest_scan "$timestamp" "$success_count" "$success_node_ids_summary"
    send_scheduled_notifications
fi
