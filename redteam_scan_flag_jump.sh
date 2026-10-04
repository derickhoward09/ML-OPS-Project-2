#!/usr/bin/env bash
#
# CS553 case study 2 — read-only SSH access check.
# By default, tries the student-admin key against ports 22002-22025 on the class
# VM. Pass -test (or --test/-t) to try TEST_NODE. Node 24 is checked on public
# port 23001 with both the student-admin and group keys.
# Full scans are allowed only from September 29, 2026 noon through October 1,
# 2026 noon, America/New_York time. Test mode may be used at any time.
# Class scan ports use 22000 + the node number. The script issues only a
# remote `true` command; it does not install files or change remote settings.
# Results are appended to redteam_scan.log beside this script.

set -euo pipefail

HOST="paffenroth-23.dyn.wpi.edu"
SSH_USER="student-admin"
KEY="$HOME/.ssh/mlops/student-admin_key"
GROUP_KEY="$HOME/.ssh/mlops/id_ed25519_group_key"
TIMEZONE="America/New_York"
NTP_SERVER="time.nist.gov"
NTP_TOLERANCE_SECONDS=60
# Node/group to check in -t mode. Node 24 uses port 23001 and both keys.
TEST_NODE=24
TEST_PORT=23001

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
LOG_FILE="$SCRIPT_DIR/redteam_scan.log"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/redteam-scan"
ENV_FILE="$SCRIPT_DIR/.env"
REDTEAM_HEALTHCHECKS_KEY="REDTEAM_HEALTHCHECKS_PING_URL"
MILESTONE_STATE_FILE="$CONFIG_DIR/logged_milestones"
LEGACY_MILESTONE_STATE_FILE="$CONFIG_DIR/sent_notifications"
LAST_SCAN_FILE="$CONFIG_DIR/last_scan"
SCHEDULE_SLOT_FILE="$CONFIG_DIR/last_scheduled_slot"
SCHEDULE_LOCK_DIR="$CONFIG_DIR/schedule.lock"
CRON_SETUP_TMP_DIR=""
SCHEDULE_LOCK_HELD=false

test_mode=false
init_cron=false
run_scheduled=false
for arg in "$@"; do
    case "$arg" in
        -test|--test|-t)
            test_mode=true
            ;;
        --init-cron|-init-cron)
            init_cron=true
            ;;
        --run-scheduled)
            run_scheduled=true
            ;;
        -h|--help)
            echo "Usage: $0 [--init-cron | -init-cron | --run-scheduled | -test|--test|-t]"
            echo "  --init-cron     Securely save a Healthchecks URL and install 45-minute scans."
            echo "  --run-scheduled Internal cron entrypoint; do not run directly."
            echo "  -test            Try TEST_NODE and log SSH results (node 24 uses both keys on port 23001)."
            echo "  Full scans check nodes 2-25. Local milestone summaries use America/New_York:"
            echo "    Sep 29, 2026 at 1 PM and 7 PM; Sep 30 at 10 AM;"
            echo "    Oct 1 at 11 AM and noon, with the latest completed scan results."
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            echo "Usage: $0 [--init-cron | -init-cron | --run-scheduled | -test|--test|-t]" >&2
            exit 2
            ;;
    esac
done

action_count=0
for action in "$init_cron" "$run_scheduled" "$test_mode"; do
    if [[ "$action" == true ]]; then
        ((action_count += 1))
    fi
done
if (( action_count > 1 )); then
    echo "ERROR: initialization, scheduled, and test modes cannot be combined." >&2
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
    TEST_KEYS=("$KEY")
    if (( TEST_NODE == 24 )); then
        TEST_KEYS+=("$GROUP_KEY")
    fi
fi

ensure_config_dir() {
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
}

valid_healthchecks_url() {
    [[ "$1" =~ ^https://hc-ping\.com/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]
}

save_redteam_healthchecks_url() {
    local url="$1" temp_file=""

    if [[ -L "$ENV_FILE" || ( -e "$ENV_FILE" && ! -f "$ENV_FILE" ) ]]; then
        echo "ERROR: $ENV_FILE must be a regular file, not a symlink." >&2
        return 1
    fi
    if [[ -e "$ENV_FILE" && ! -r "$ENV_FILE" ]]; then
        echo "ERROR: cannot read existing configuration at $ENV_FILE." >&2
        return 1
    fi

    if ! temp_file="$(umask 077; mktemp "$SCRIPT_DIR/.env.XXXXXX")"; then
        echo "ERROR: could not create a private temporary .env file." >&2
        return 1
    fi

    if [[ -e "$ENV_FILE" ]] && ! awk -v key="$REDTEAM_HEALTHCHECKS_KEY" 'index($0, key "=") != 1' "$ENV_FILE" > "$temp_file"; then
        rm -f -- "$temp_file"
        echo "ERROR: could not preserve the existing .env contents." >&2
        return 1
    fi
    if ! printf '%s=%s\n' "$REDTEAM_HEALTHCHECKS_KEY" "$url" >> "$temp_file" ||
       ! chmod 600 "$temp_file" || ! mv -f -- "$temp_file" "$ENV_FILE"; then
        rm -f -- "$temp_file"
        echo "ERROR: could not save the Healthchecks URL to $ENV_FILE." >&2
        return 1
    fi
}

install_redteam_cron() {
    local current_file="$1" new_file="$2" remove_legacy_ping_url=false

    if awk '!/^[[:space:]]*#/ && /redteam_scan_flag[.]sh/ && !/# redteam-scan-cron/ { found=1 } END { exit !found }' "$current_file"; then
        remove_legacy_ping_url=true
    fi

    awk -v remove_legacy_ping_url="$remove_legacy_ping_url" '
        /# redteam-scan-cron([[:space:]]|$)/ { next }
        /^[[:space:]]*#/ { print; next }
        remove_legacy_ping_url == "true" && /^[[:space:]]*PING_URL=https:\/\/hc-ping[.]com\// { next }
        /redteam_scan_flag[.]sh/ { next }
        { print }
    ' "$current_file" > "$new_file"

    # Poll every quarter hour so a locked run can retry; run_scheduled_scan
    # gates full scans to at least three quarter-hour slots (45 minutes) apart.
    printf '*/15 * * * * /bin/bash "%s" --run-scheduled >> "%s/redteam_scan_cron.log" 2>&1 # redteam-scan-cron\n' \
        "$SCRIPT_PATH" "$SCRIPT_DIR" >> "$new_file"
}

initialize_cron() {
    local ping_url="" temp_dir="" current_file error_file new_file

    if [[ ! -t 0 ]]; then
        echo "ERROR: --init-cron needs an interactive terminal." >&2
        return 1
    fi
    for command_name in crontab curl; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "ERROR: --init-cron requires $command_name." >&2
            return 1
        fi
    done
    if [[ -L "$ENV_FILE" || ( -e "$ENV_FILE" && ! -f "$ENV_FILE" ) ]]; then
        echo "ERROR: $ENV_FILE must be a regular file, not a symlink." >&2
        return 1
    fi

    if ! temp_dir="$(mktemp -d)"; then
        echo "ERROR: could not create temporary files for crontab setup." >&2
        return 1
    fi
    CRON_SETUP_TMP_DIR="$temp_dir"
    trap 'if [[ -n "${CRON_SETUP_TMP_DIR:-}" ]]; then rm -rf "$CRON_SETUP_TMP_DIR"; fi' EXIT
    current_file="$temp_dir/current"
    error_file="$temp_dir/error"
    new_file="$temp_dir/new"

    if ! crontab -l > "$current_file" 2> "$error_file"; then
        if ! grep -qi 'no crontab' "$error_file"; then
            cat "$error_file" >&2
            return 1
        fi
        : > "$current_file"
    fi

    printf 'Healthchecks.io ping URL (input hidden): '
    IFS= read -r -s ping_url || {
        printf '\n'
        echo "ERROR: could not read the Healthchecks URL." >&2
        return 1
    }
    printf '\n'
    if ! valid_healthchecks_url "$ping_url"; then
        unset ping_url
        echo "ERROR: enter a Healthchecks ping URL in the form https://hc-ping.com/<UUID>." >&2
        return 1
    fi

    install_redteam_cron "$current_file" "$new_file"
    if ! save_redteam_healthchecks_url "$ping_url"; then
        unset ping_url
        return 1
    fi
    unset ping_url

    if ! crontab "$new_file"; then
        echo "ERROR: the Healthchecks URL was saved, but the crontab could not be installed." >&2
        return 1
    fi
    echo "Installed redteam scans every 45 minutes for $(id -un)."
    echo "The Healthchecks URL is saved privately in $ENV_FILE."
}

load_redteam_healthchecks_url() {
    local line candidate=""

    if [[ -L "$ENV_FILE" || ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
        return 1
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$REDTEAM_HEALTHCHECKS_KEY="* ]]; then
            candidate="${line#*=}"
        fi
    done < "$ENV_FILE"
    if ! valid_healthchecks_url "$candidate"; then
        unset candidate
        return 1
    fi
    REDTEAM_HEALTHCHECKS_URL="$candidate"
    unset candidate
}

ping_redteam_healthchecks() {
    local suffix="$1" http_status

    http_status="$(curl --silent --show-error --connect-timeout 3 --max-time 7 \
        --output /dev/null --write-out '%{http_code}' --config - 2>/dev/null <<EOF
url = "$REDTEAM_HEALTHCHECKS_URL$suffix"
EOF
    )" || return 1
    [[ "$http_status" == 2?? ]]
}

release_scheduled_lock() {
    local owner=""

    if [[ "$SCHEDULE_LOCK_HELD" == true && -f "$SCHEDULE_LOCK_DIR/pid" && ! -L "$SCHEDULE_LOCK_DIR/pid" ]]; then
        IFS= read -r owner < "$SCHEDULE_LOCK_DIR/pid" || true
        if [[ "$owner" == "$$" ]]; then
            rm -rf "$SCHEDULE_LOCK_DIR"
        fi
    fi
}

acquire_scheduled_lock() {
    local owner="" attempt

    for attempt in 1 2 3 4 5; do
        if mkdir "$SCHEDULE_LOCK_DIR" 2>/dev/null; then
            if ! (umask 077; printf '%s\n' "$$" > "$SCHEDULE_LOCK_DIR/pid"); then
                rmdir "$SCHEDULE_LOCK_DIR" 2>/dev/null || true
                return 2
            fi
            SCHEDULE_LOCK_HELD=true
            trap 'release_scheduled_lock' EXIT
            return 0
        fi

        if [[ ! -d "$SCHEDULE_LOCK_DIR" ]]; then
            sleep 0.05
            continue
        fi
        owner=""
        if [[ -f "$SCHEDULE_LOCK_DIR/pid" && ! -L "$SCHEDULE_LOCK_DIR/pid" ]]; then
            IFS= read -r owner < "$SCHEDULE_LOCK_DIR/pid" || true
        else
            # A new owner may be between mkdir and writing its PID.
            sleep 0.05
            continue
        fi
        if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
            return 1
        fi

        # Claim stale-lock cleanup inside the directory so only one contender
        # can remove it while the others wait.
        if mkdir "$SCHEDULE_LOCK_DIR/reaper" 2>/dev/null; then
            owner=""
            if [[ -f "$SCHEDULE_LOCK_DIR/pid" && ! -L "$SCHEDULE_LOCK_DIR/pid" ]]; then
                IFS= read -r owner < "$SCHEDULE_LOCK_DIR/pid" || true
            fi
            if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
                rmdir "$SCHEDULE_LOCK_DIR/reaper" 2>/dev/null || true
                return 1
            fi
            rm -rf "$SCHEDULE_LOCK_DIR"
        else
            sleep 0.05
        fi
    done

    if [[ -d "$SCHEDULE_LOCK_DIR" ]]; then
        return 1
    fi
    return 2
}

run_scheduled_scan() {
    local slot last_slot="" temp_file="" scan_status=0 healthcheck_status=0 lock_status

    for command_name in curl; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "ERROR: scheduled scans require $command_name." >&2
            return 1
        fi
    done
    ensure_config_dir
    if acquire_scheduled_lock; then
        :
    else
        lock_status=$?
        if (( lock_status == 1 )); then
            # Keep the current scheduled slot pending; the next quarter-hour tick retries.
            return 0
        fi
        echo "ERROR: could not acquire the redteam schedule lock." >&2
        return 1
    fi

    slot=$(( $(date '+%s') / 900 ))
    if [[ -f "$SCHEDULE_SLOT_FILE" && ! -L "$SCHEDULE_SLOT_FILE" ]]; then
        IFS= read -r last_slot < "$SCHEDULE_SLOT_FILE" || true
    fi
    if [[ "$last_slot" =~ ^[0-9]+$ ]] && (( slot - last_slot < 3 )); then
        return 0
    fi

    if ! load_redteam_healthchecks_url; then
        echo "ERROR: redteam Healthchecks URL is missing or invalid in $ENV_FILE; run $SCRIPT_PATH --init-cron." >&2
        return 1
    fi

    if ! temp_file="$(umask 077; mktemp "$CONFIG_DIR/.last_scheduled_slot.XXXXXX")"; then
        echo "ERROR: could not update the redteam schedule state." >&2
        unset REDTEAM_HEALTHCHECKS_URL
        return 1
    fi
    if ! printf '%s\n' "$slot" > "$temp_file" || ! chmod 600 "$temp_file" || ! mv -f -- "$temp_file" "$SCHEDULE_SLOT_FILE"; then
        rm -f -- "$temp_file"
        echo "ERROR: could not update the redteam schedule state." >&2
        unset REDTEAM_HEALTHCHECKS_URL
        return 1
    fi

    if ! ping_redteam_healthchecks '/start'; then
        echo "ERROR: Healthchecks start ping failed; running the scan anyway." >&2
    fi
    if bash "$SCRIPT_PATH"; then
        scan_status=0
    else
        scan_status=$?
    fi

    if (( scan_status == 0 )); then
        if ! ping_redteam_healthchecks ''; then
            echo "ERROR: Healthchecks completion ping failed." >&2
            healthcheck_status=1
        fi
    else
        if ! ping_redteam_healthchecks '/fail'; then
            echo "ERROR: Healthchecks failure ping failed." >&2
            healthcheck_status=1
        fi
    fi
    unset REDTEAM_HEALTHCHECKS_URL

    if (( scan_status != 0 )); then
        return "$scan_status"
    fi
    return "$healthcheck_status"
}

if "$init_cron"; then
    initialize_cron
    exit $?
fi

if "$run_scheduled"; then
    run_scheduled_scan
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

    if ! result="$(python3 "$SCRIPT_DIR/ntp_clock_check.py" \
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

log_scheduled_milestones() (
    local now_epoch event_epoch age event_id event_label scan_time scan_count scan_nodes message timestamp temp_file lock_status event_index state_file
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

    # Serialize milestone import/logging independently of the parent cron lock.
    SCHEDULE_LOCK_DIR="$CONFIG_DIR/milestones.lock"
    SCHEDULE_LOCK_HELD=false
    if acquire_scheduled_lock; then
        :
    else
        lock_status=$?
        (( lock_status == 1 )) && return 0
        echo "ERROR: could not acquire the milestone log lock." >&2
        return 1
    fi
    temp_file="$(umask 077; mktemp "$CONFIG_DIR/.logged_milestones.XXXXXX")"
    for state_file in "$MILESTONE_STATE_FILE" "$LEGACY_MILESTONE_STATE_FILE"; do
        if [[ -f "$state_file" && -r "$state_file" && ! -L "$state_file" ]]; then
            # Import only known milestones from the retired delivery state.
            awk '/^(202609291300|202609291900|202609301000|202610011100|202610011200)$/ { print }' "$state_file" >> "$temp_file"
        fi
    done
    sort -u -o "$temp_file" "$temp_file"
    mv -f -- "$temp_file" "$MILESTONE_STATE_FILE"

    for ((event_index = 0; event_index < ${#event_ids[@]}; event_index++)); do
        event_id="${event_ids[$event_index]}"
        if [[ -r "$MILESTONE_STATE_FILE" ]] && grep -Fqx -- "$event_id" "$MILESTONE_STATE_FILE"; then
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
        timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
        printf '%s | %s\n' "$timestamp" "$message" >> "$LOG_FILE"
        (umask 077; printf '%s\n' "$event_id" >> "$MILESTONE_STATE_FILE")
    done
)

if "$test_mode"; then
    check_ntp_clock
fi

if ! "$test_mode"; then
    window_start="20260929120000"
    window_end="20261001120000"
    now="$(TZ="$TIMEZONE" date '+%Y%m%d%H%M%S')"
    if [[ "$now" < "$window_start" ]]; then
        echo "ERROR: full scans are allowed only from September 29, 2026 noon through October 1, 2026 noon (America/New_York)." >&2
        # This is an expected cron skip, not a failed scan. Return success so
        # Healthchecks does not alert for runs before the permitted window.
        exit 0
    fi
    if [[ "$now" > "$window_end" || "$now" == "$window_end" ]]; then
        # The noon deadline summary runs outside the scan window and uses the
        # latest result persisted by a scan that completed before noon.
        log_scheduled_milestones
        exit 0
    fi
fi

if "$test_mode"; then
    available_test_keys=()
    test_key_results=()
    for key_path in "${TEST_KEYS[@]}"; do
        if [[ "$key_path" == "$GROUP_KEY" ]]; then
            key_label="group key"
        else
            key_label="student-admin key"
        fi
        if [[ -f "$key_path" && -s "$key_path" && -r "$key_path" ]]; then
            available_test_keys+=("$key_path")
        else
            echo "Test key unavailable: $key_label ($key_path)"
            test_key_results+=("$key_label=unavailable")
        fi
    done
    if (( ${#available_test_keys[@]} == 0 )); then
        printf '%s | Test node %s: no configured test key is available.\n' \
            "$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')" "$TEST_NODE" >> "$LOG_FILE"
        echo "ERROR: no configured test key is available." >&2
        exit 1
    fi
    echo "Test mode: checking node $TEST_NODE with ${#available_test_keys[@]} available key(s)."
    if (( TEST_NODE == 24 )); then
        ports=("$TEST_PORT")
    else
        ports=("$((22000 + TEST_NODE))")
    fi
else
    if [[ ! -r "$KEY" ]]; then
        log_scheduled_milestones
        echo "ERROR: cannot read SSH key: $KEY" >&2
        exit 1
    fi

    # Build nodes 2-25's corresponding SSH ports, then shuffle them in place.
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
    -F "$SCRIPT_DIR/scripts/ssh_config"
    -T
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
    if "$test_mode"; then
        node="$TEST_NODE"
    else
        node=$((port - 22000))
    fi

    if "$test_mode"; then
        for scan_key in "${available_test_keys[@]}"; do
            if [[ "$scan_key" == "$GROUP_KEY" ]]; then
                key_label="group key"
            else
                key_label="student-admin key"
            fi
            printf 'Trying node %s on port %s with %s... ' "$node" "$port" "$key_label"
            if ssh -J akrett@turing.wpi.edu "${ssh_opts[@]}" -i "$scan_key" -p "$port" "${SSH_USER}@${HOST}" true >/dev/null 2>&1; then
                echo "login succeeded."
                success_nodes+=("node $node (port $port, $key_label)")
                success_node_ids+=("$node")
                test_key_results+=("$key_label=succeeded")
            else
                echo "login failed."
                test_key_results+=("$key_label=failed")
            fi
        done
    else
        printf 'Trying node %s on port %s... ' "$node" "$port"
        if ssh "${ssh_opts[@]}" -i "$KEY" -p "$port" "${SSH_USER}@${HOST}" true >/dev/null 2>&1; then
            echo "login succeeded."
            success_nodes+=("node $node (port $port)")
            success_node_ids+=("$node")
        else
            echo "login failed."
        fi
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
    test_key_summary="${test_key_results[*]}"
    printf '%s | Test node %s SSH login %s. Key results: %s.\n' \
        "$timestamp" "$TEST_NODE" "$test_result" "$test_key_summary" >> "$LOG_FILE"
else
    store_latest_scan "$timestamp" "$success_count" "$success_node_ids_summary"
    log_scheduled_milestones
fi
