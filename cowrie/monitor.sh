#!/usr/bin/env bash
# Run the shared Cowrie check once a minute. Repairs run in a separate,
# locked process so a deployment cannot block the monitor heartbeat.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${COWRIE_MONITOR_CONFIG:-$SCRIPT_DIR/../.env}"
STATE_DIR="${COWRIE_MONITOR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-monitor}"
STATE_FILE="$STATE_DIR/status"
ROUTE_STATE_FILE="$STATE_DIR/route-status"
LOG_FILE="$STATE_DIR/monitor.log"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPAIR_LOG="${COWRIE_MONITOR_RECONCILE_LOG:-$REPO_ROOT/logs/cowrie_reconcile.log}"
HOST=paffenroth-23.dyn.wpi.edu
SSH_PORT=23001
PUBLIC_PORT=22001
TIMEZONE=America/New_York
ROUTE_WINDOW_START=20260929120000
ROUTE_WINDOW_END=20261001120000
ROUTE_INTERVAL_SECONDS=2700
DEPLOY_CHECK_TIMEOUT=25s
MONITOR_JITTER_MAX_SECONDS=20

if (( $# != 0 )); then
    echo "Usage: $0" >&2
    exit 2
fi

for command_name in flock timeout ssh python3 curl date; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done

mkdir -p -- "$STATE_DIR"
chmod 700 "$STATE_DIR"
exec 9>"$STATE_DIR/monitor.lock"
if ! flock -n 9; then
    # The running monitor owns this minute. Do not send a duplicate heartbeat.
    exit 0
fi

log() {
    printf '%s | %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"
}

# Spread the once-a-minute SSH check over a 20-second window. The heartbeat
# still runs each minute, and this remains well inside its five-minute grace.
jitter_seconds=$((RANDOM % (MONITOR_JITTER_MAX_SECONDS + 1)))
if (( jitter_seconds > 0 )); then
    log "Applying ${jitter_seconds}s startup jitter."
    sleep "$jitter_seconds"
fi

discord_url=''
healthchecks_url=''
read_config() {
    local line
    if [[ -L "$ENV_FILE" || ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
        log "Configuration unavailable: $ENV_FILE must be a readable regular file."
        return 1
    fi

    # A .env file is input data here, never shell code.
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            DISCORD_WEBHOOK_URL=*) discord_url="${line#DISCORD_WEBHOOK_URL=}" ;;
            HEALTHCHECKS_PING_URL=*) healthchecks_url="${line#HEALTHCHECKS_PING_URL=}" ;;
        esac
    done < "$ENV_FILE"

    if [[ ! "$discord_url" =~ ^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$ ]]; then
        log "Discord webhook is missing or invalid in $ENV_FILE."
        discord_url=''
    fi
    if [[ ! "$healthchecks_url" =~ ^https://hc-ping\.com/[0-9A-Fa-f-]{36}$ ]]; then
        log "Healthchecks ping URL is missing or invalid in $ENV_FILE."
        healthchecks_url=''
    fi
}

send_discord() {
    local message="$1" payload http_status
    [[ -n "$discord_url" ]] || return 1
    payload="$(python3 -c 'import json, sys; print(json.dumps({"content": sys.argv[1]}))' "$message")" || return 1
    # The validated secret URL is read by curl from stdin; it is never an argv value.
    http_status="$(curl --silent --show-error --connect-timeout 3 --max-time 7 \
        --output /dev/null --write-out '%{http_code}' --request POST \
        --header 'Content-Type: application/json' --data "$payload" \
        --config - 2>/dev/null <<EOF
url = "$discord_url"
EOF
)" || return 1
    [[ "$http_status" == 2?? ]]
}

ping_healthchecks() {
    local http_status
    [[ -n "$healthchecks_url" ]] || return 1
    # This is a scheduler heartbeat, even when the target is unhealthy.
    http_status="$(curl --silent --show-error --connect-timeout 3 --max-time 7 \
        --output /dev/null --write-out '%{http_code}' --config - 2>/dev/null <<EOF
url = "$healthchecks_url"
EOF
)" || return 1
    [[ "$http_status" == 2?? ]]
}

previous_failures=0
announced_state=healthy
pending_outage=0
if [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]]; then
    IFS=' ' read -r stored_failures stored_state stored_pending < "$STATE_FILE" || true
    # Migrate the older two-field state. A second failed sample with no DOWN
    # announcement meant its webhook had failed and the outage was pending.
    if [[ -z "${stored_pending:-}" && "${stored_failures:-}" == 2 && "${stored_state:-}" == healthy ]]; then
        stored_pending=1
    fi
    [[ -n "${stored_pending:-}" ]] || stored_pending=0
    if [[ "${stored_failures:-}" =~ ^[0-2]$ &&
          ( "${stored_state:-}" == healthy || "${stored_state:-}" == failing ) &&
          "$stored_pending" =~ ^[01]$ ]]; then
        previous_failures="$stored_failures"
        announced_state="$stored_state"
        pending_outage="$stored_pending"
        [[ "$announced_state" == failing ]] && pending_outage=0
    else
        log "Invalid monitor state; resetting counters."
    fi
fi

read_config || true

route_last_epoch=0
route_failures=0
route_announced_state=healthy
route_pending=none
if [[ -f "$ROUTE_STATE_FILE" && ! -L "$ROUTE_STATE_FILE" ]]; then
    IFS=' ' read -r stored_route_epoch stored_route_failures stored_route_state stored_route_pending \
        < "$ROUTE_STATE_FILE" || true
    if [[ "${stored_route_epoch:-}" =~ ^[0-9]+$ &&
          "${stored_route_failures:-}" =~ ^[0-2]$ &&
          ( "${stored_route_state:-}" == healthy || "${stored_route_state:-}" == failing ) &&
          ( "${stored_route_pending:-}" == none || "${stored_route_pending:-}" == down ||
            "${stored_route_pending:-}" == recovery || "${stored_route_pending:-}" == recovered ) ]]; then
        route_last_epoch="$stored_route_epoch"
        route_failures="$stored_route_failures"
        route_announced_state="$stored_route_state"
        route_pending="$stored_route_pending"
    else
        log "Invalid 22001 route state; resetting counters."
    fi
fi

reasons=()
repair_mode=''
check_output=''
if check_output="$(timeout --kill-after=1s "$DEPLOY_CHECK_TIMEOUT" \
    "$SCRIPT_DIR/deploy.sh" --check 2>&1)"; then
    check_status=0
else
    check_status=$?
fi

case "$check_status" in
    0)
        ;;
    2|3|4|124)
        reason="group-key management SSH on $SSH_PORT failed"
        [[ "$check_status" == 3 ]] && reason="authorized_keys does not match the group public key"
        [[ "$check_status" == 4 ]] && reason="group key material is missing or unreadable"
        reasons+=("$reason")
        repair_mode=--repair-access
        ;;
    *)
        summary_detail="$(printf '%s' "$check_output" | tr '\r\n' '  ' | cut -c1-240)"
        [[ -n "$summary_detail" ]] || summary_detail="Cowrie deployment or service health check failed"
        reasons+=("$summary_detail")
        repair_mode=--repair-deploy
        ;;
esac

route_due=false
route_in_window=false
route_failed_this_run=false
route_result='not due'
route_now_local="$(TZ="$TIMEZONE" date '+%Y%m%d%H%M%S')"
route_now_epoch="$(date '+%s')"
if [[ "$route_now_local" > "$ROUTE_WINDOW_START" || "$route_now_local" == "$ROUTE_WINDOW_START" ]] &&
   [[ "$route_now_local" < "$ROUTE_WINDOW_END" ]]; then
    route_in_window=true
    if [[ "$route_last_epoch" == 0 ]] || (( route_now_epoch - route_last_epoch >= ROUTE_INTERVAL_SECONDS )); then
        route_due=true
        route_last_epoch="$route_now_epoch"
        # Silence after a successful TCP connect is expected: the public route
        # must delay its SSH banner until just before its 15-minute deadline.
        route_result="$(timeout --kill-after=1s 6s python3 - "$HOST" "$PUBLIC_PORT" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3) as sock:
        sock.settimeout(1.25)
        try:
            data = sock.recv(1)
        except socket.timeout:
            print("ok")
        else:
            print("early banner" if data else "early close")
except OSError:
    print("connect failed")
PY
)" || route_result="probe failed"
        if [[ "$route_result" == ok ]]; then
            route_failures=0
            if [[ "$route_announced_state" == failing ]]; then
                route_pending=recovery
            elif [[ "$route_pending" == down ]]; then
                route_pending=recovered
            fi
        else
            route_failed_this_run=true
            if [[ "$route_announced_state" == failing && "$route_pending" == recovery ]]; then
                route_pending=none
            fi
            if (( route_failures < 2 )); then
                ((route_failures += 1))
            fi
            if (( route_failures >= 2 )) && [[ "$route_announced_state" == healthy && "$route_pending" == none ]]; then
                route_pending=down
            fi
        fi
    fi
fi

if (( ${#reasons[@]} == 0 )); then
    failures=0
    summary="all checks passed"
else
    failures=$((previous_failures + 1))
    (( failures > 2 )) && failures=2
    summary="$(IFS='; '; echo "${reasons[*]}")"
fi

alert_failed=false
if (( ${#reasons[@]} > 0 )); then
    if (( failures >= 2 )) && [[ "$announced_state" == healthy ]]; then
        pending_outage=1
        if send_discord "Cowrie node 24 DOWN: $summary. Reconciler will retry."; then
            announced_state=failing
            pending_outage=0
            log "Discord DOWN alert sent."
        else
            alert_failed=true
            log "Discord DOWN alert failed; will retry while down."
        fi
    fi
elif [[ "$announced_state" == failing ]]; then
    if send_discord "Cowrie node 24 RECOVERED: group-key SSH, authorized_keys, Cowrie deployment, and both services are healthy."; then
        announced_state=healthy
        pending_outage=0
        log "Discord recovery alert sent."
    else
        alert_failed=true
        log "Discord recovery alert failed; will retry while healthy."
    fi
elif [[ "$pending_outage" == 1 ]]; then
    if send_discord "Cowrie node 24 OUTAGE RECOVERED: a sustained management or service outage ended before its DOWN alert could be delivered. Group-key SSH, authorized_keys, Cowrie deployment, and both services are now healthy."; then
        pending_outage=0
        log "Discord delayed outage recovery alert sent."
    else
        alert_failed=true
        log "Discord delayed outage recovery alert failed; will retry while healthy."
    fi
fi

route_alert_failed=false
if "$route_in_window" || [[ "$route_pending" != none ]]; then
    case "$route_pending" in
        down)
            if send_discord "Cowrie node 24 public port 22001 DOWN: two consecutive scheduled checks failed the delayed-banner test."; then
                route_announced_state=failing
                route_pending=none
                log "Discord 22001 DOWN alert sent."
            else
                route_alert_failed=true
                log "Discord 22001 DOWN alert failed; will retry."
            fi
            ;;
        recovery)
            if send_discord "Cowrie node 24 public port 22001 RECOVERED: the delayed-banner check passed."; then
                route_announced_state=healthy
                route_pending=none
                log "Discord 22001 recovery alert sent."
            else
                route_alert_failed=true
                log "Discord 22001 recovery alert failed; will retry."
            fi
            ;;
        recovered)
            if send_discord "Cowrie node 24 public port 22001 OUTAGE RECOVERED: the delayed-banner check passed after an undelivered DOWN alert."; then
                route_announced_state=healthy
                route_pending=none
                log "Discord delayed 22001 recovery notice sent."
            else
                route_alert_failed=true
                log "Discord delayed 22001 recovery notice failed; will retry."
            fi
            ;;
    esac
fi

# State is committed after each completed check, even if a webhook failed.
state_tmp="$(mktemp "$STATE_DIR/.status.XXXXXX")"
printf '%s %s %s\n' "$failures" "$announced_state" "$pending_outage" > "$state_tmp"
mv -f "$state_tmp" "$STATE_FILE"
route_state_tmp="$(mktemp "$STATE_DIR/.route-status.XXXXXX")"
printf '%s %s %s %s\n' "$route_last_epoch" "$route_failures" \
    "$route_announced_state" "$route_pending" > "$route_state_tmp"
mv -f "$route_state_tmp" "$ROUTE_STATE_FILE"

heartbeat_failed=false
if ! ping_healthchecks; then
    heartbeat_failed=true
    log "Healthchecks heartbeat failed or is unconfigured."
fi

if (( ${#reasons[@]} == 0 )) && ! "$route_failed_this_run"; then
    log "HEALTHY: $summary."
elif (( ${#reasons[@]} == 0 )); then
    log "HEALTHY on management SSH and deployment; public $PUBLIC_PORT check failed: $route_result."
else
    log "UNHEALTHY ($failures consecutive): $summary."
fi

if [[ -n "$repair_mode" ]]; then
    mkdir -p -- "$(dirname -- "$REPAIR_LOG")"
    nohup /bin/bash "$SCRIPT_DIR/reconcile.sh" "$repair_mode" \
        >> "$REPAIR_LOG" 2>&1 </dev/null &
    log "Started background Cowrie recovery ($repair_mode)."
fi

if "$alert_failed" || "$route_alert_failed" || "$heartbeat_failed" || \
   (( ${#reasons[@]} > 0 )) || "$route_failed_this_run"; then
    exit 1
fi
