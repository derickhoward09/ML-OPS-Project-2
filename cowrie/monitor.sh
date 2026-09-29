#!/usr/bin/env bash
# Run once a minute on the scheduler VM, independently of reconcile.sh.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${COWRIE_MONITOR_CONFIG:-$SCRIPT_DIR/../.env}"
STATE_DIR="${COWRIE_MONITOR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-monitor}"
STATE_FILE="$STATE_DIR/status"
LOG_FILE="$STATE_DIR/monitor.log"
HOST=paffenroth-23.dyn.wpi.edu
SSH_PORT=23001
PUBLIC_PORT=22001
REMOTE_USER=student-admin
GROUP_KEY="$HOME/.ssh/mlops/id_ed25519_group_key"

if (( $# != 0 )); then
    echo "Usage: $0" >&2
    exit 2
fi

for command_name in flock timeout ssh python3 curl; do
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

reasons=()
if [[ ! -f "$GROUP_KEY" || ! -s "$GROUP_KEY" || ! -r "$GROUP_KEY" ]]; then
    reasons+=("group key missing or unreadable")
else
    ssh_output=''
    if ! ssh_output="$(timeout --kill-after=1s 12s ssh -T -F /dev/null -i "$GROUP_KEY" -p "$SSH_PORT" \
        -o IdentitiesOnly=yes -o CertificateFile=none -o BatchMode=yes -o ConnectionAttempts=1 \
        -o PreferredAuthentications=publickey -o PasswordAuthentication=no \
        -o KbdInteractiveAuthentication=no -o GSSAPIAuthentication=no \
        -o HostbasedAuthentication=no \
        -o ConnectTimeout=4 -o ServerAliveInterval=4 -o ServerAliveCountMax=1 \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o GlobalKnownHostsFile=/dev/null \
        "$REMOTE_USER@$HOST" \
        'for service in cowrie.service cowrie-delay.service; do if systemctl is-active --quiet "$service"; then printf "active\n"; else printf "inactive\n"; fi; done' \
        2>/dev/null)"; then
        reasons+=("group-key management SSH on $SSH_PORT failed")
    else
        expected_output=$'active\nactive'
        if [[ "$ssh_output" != "$expected_output" ]]; then
            first_status="${ssh_output%%$'\n'*}"
            second_status=''
            [[ "$ssh_output" == *$'\n'* ]] && second_status="${ssh_output#*$'\n'}"
            [[ "$first_status" == active ]] || reasons+=("cowrie.service is not active")
            [[ "$second_status" == active ]] || reasons+=("cowrie-delay.service is not active")
        fi
    fi
fi

# A successful connect followed by a short silent read verifies the public
# route reaches a banner-delaying listener, rather than management sshd.
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
[[ "$route_result" == ok ]] || reasons+=("public $PUBLIC_PORT route: $route_result")

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
    if send_discord "Cowrie node 24 RECOVERED: group-key SSH on $SSH_PORT, both services, and public $PUBLIC_PORT are healthy."; then
        announced_state=healthy
        pending_outage=0
        log "Discord recovery alert sent."
    else
        alert_failed=true
        log "Discord recovery alert failed; will retry while healthy."
    fi
elif [[ "$pending_outage" == 1 ]]; then
    if send_discord "Cowrie node 24 OUTAGE RECOVERED: a sustained outage ended before its DOWN alert could be delivered. Group-key SSH on $SSH_PORT, both services, and public $PUBLIC_PORT are now healthy."; then
        pending_outage=0
        log "Discord delayed outage recovery alert sent."
    else
        alert_failed=true
        log "Discord delayed outage recovery alert failed; will retry while healthy."
    fi
fi

# State is committed after each completed check, even if a webhook failed.
state_tmp="$(mktemp "$STATE_DIR/.status.XXXXXX")"
printf '%s %s %s\n' "$failures" "$announced_state" "$pending_outage" > "$state_tmp"
mv -f "$state_tmp" "$STATE_FILE"

heartbeat_failed=false
if ! ping_healthchecks; then
    heartbeat_failed=true
    log "Healthchecks heartbeat failed or is unconfigured."
fi

if (( ${#reasons[@]} == 0 )); then
    log "HEALTHY: $summary."
else
    log "UNHEALTHY ($failures consecutive): $summary."
fi

if "$alert_failed" || "$heartbeat_failed" || (( ${#reasons[@]} > 0 )); then
    exit 1
fi
