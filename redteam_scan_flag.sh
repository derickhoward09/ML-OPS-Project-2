#!/usr/bin/env bash
#
# CS553 case study 2 — read-only SSH access check.
# By default, tries the configured key against ports 22002-22025 on the class
# VM. Pass -test (or --test/-t) to check the configured key and try only port 22001.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/redteam_scan.log"

test_mode=false
for arg in "$@"; do
    case "$arg" in
        -test|--test|-t)
            test_mode=true
            ;;
        -h|--help)
            echo "Usage: $0 [-test|--test|-t]"
            echo "  -test      Check the configured SSH key and try only port 22001."
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            echo "Usage: $0 [-test|--test|-t]" >&2
            exit 2
            ;;
    esac
done

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

if "$test_mode"; then
    check_ntp_clock
fi

if ! "$test_mode"; then
    window_start="20260929120000"
    window_end="20261001120000"
    now="$(TZ="$TIMEZONE" date '+%Y%m%d%H%M%S')"
    if [[ "$now" < "$window_start" || "$now" > "$window_end" || "$now" == "$window_end" ]]; then
        echo "ERROR: full scans are allowed only from September 29, 2026 noon through October 1, 2026 noon (America/New_York)." >&2
        exit 1
    fi
fi

if [ ! -r "$KEY" ]; then
    echo "ERROR: cannot read SSH key: $KEY" >&2
    exit 1
fi

if "$test_mode"; then
    if [ ! -f "$KEY" ] || [ ! -s "$KEY" ]; then
        echo "ERROR: expected a non-empty key file at: $KEY" >&2
        exit 1
    fi
    echo "Test mode: SSH key is present and readable at $KEY"
    ports=(22001)
else
    # Build node 2-25's corresponding SSH ports, then shuffle them in place.
    ports=()
    for node in {2..25}; do
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
for ((index = 0; index < ${#ports[@]}; index++)); do
    port="${ports[$index]}"
    node=$((port - 22000))

    printf 'Trying node %s on port %s... ' "$node" "$port"
    if ssh "${ssh_opts[@]}" -p "$port" "${SSH_USER}@${HOST}" true >/dev/null 2>&1; then
        echo "login succeeded."
        success_nodes+=("node $node (port $port)")
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

echo
echo "Authenticated: $success_summary"
echo "Results appended to $LOG_FILE"
