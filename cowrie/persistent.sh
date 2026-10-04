#!/usr/bin/env bash
# Keep one group-key SSH transport to node 24 and use it for Cowrie checks.
# Only a lost transport can start fresh group/student-key authentication.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TARGET=student-admin@paffenroth-23.dyn.wpi.edu
PORT=23001
SSH_JUMP="${RESUMELENS_SSH_JUMP:-${SSH_JUMP:-turing.wpi.edu}}"
GROUP_KEY="$HOME/.ssh/mlops/id_ed25519_group_key"
GROUP_PUBLIC_KEY="$HOME/.ssh/mlops/id_ed25519_group_key.pub"
STUDENT_KEY="$HOME/.ssh/mlops/student-admin_key"
STATE_DIR="${COWRIE_PERSISTENT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-persistent}"
CONTROL_PATH="$STATE_DIR/master.sock"
RETRY_FILE="$STATE_DIR/retry-state"

case "${1:-}" in
    --check|--recover|--initialize|--stop|--stop-keep-state) mode="$1" ;;
    *) echo "Usage: $0 [--check|--recover|--initialize|--stop|--stop-keep-state]" >&2; exit 2 ;;
esac
if (( $# != 1 )); then
    echo "Usage: $0 [--check|--recover|--initialize|--stop|--stop-keep-state]" >&2
    exit 2
fi

for command_name in ssh ssh-keygen timeout flock date; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done
if [[ -L "$STATE_DIR" ]]; then
    echo "ERROR: persistent SSH state directory must not be a symlink." >&2
    exit 1
fi
mkdir -p -- "$STATE_DIR"
chmod 700 "$STATE_DIR"

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

SSH_COMMON=(
    -T -F "$REPO_ROOT/scripts/ssh_config" -p "$PORT"
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o CertificateFile=none
    -o PreferredAuthentications=publickey
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o GSSAPIAuthentication=no
    -o HostbasedAuthentication=no
    -o ConnectTimeout=8
    -o ConnectionAttempts=1
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
    -o ForwardAgent=no
    -o ClearAllForwardings=yes
    -o LogLevel=ERROR
)
SSH_GROUP=("${SSH_COMMON[@]}" -J "$SSH_JUMP" -i "$GROUP_KEY")
SSH_STUDENT=("${SSH_COMMON[@]}" -J "$SSH_JUMP" -i "$STUDENT_KEY" -o ControlPath=none)
SSH_MUX=("${SSH_COMMON[@]}" -i "$GROUP_KEY" -S "$CONTROL_PATH" -o ControlMaster=no -o ProxyCommand=/bin/false)

key_material_available() {
    [[ -s "$GROUP_KEY" && -r "$GROUP_KEY" && -s "$GROUP_PUBLIC_KEY" && -r "$GROUP_PUBLIC_KEY" ]]
}

master_alive() {
    # The control command is local to the Unix socket. Its failing proxy
    # fallback guarantees that this liveness check cannot open target TCP.
    timeout --kill-after=1s 4s ssh -F "$REPO_ROOT/scripts/ssh_config" -S "$CONTROL_PATH" \
        -o ProxyCommand=/bin/false -O check -p "$PORT" "$TARGET" \
        >/dev/null 2>&1
}

remote_alive() {
    timeout --kill-after=1s 8s ssh "${SSH_MUX[@]}" "$TARGET" true \
        >/dev/null 2>&1
}

check_health() {
    if ! key_material_available; then
        echo "ERROR: group SSH key material is missing or unreadable." >&2
        return 4
    fi
    if ! validate_group_key_pair; then
        echo "ERROR: local group public and private keys do not match." >&2
        return 4
    fi
    if ! master_alive; then
        echo "ERROR: persistent management SSH connection is unavailable." >&2
        return 2
    fi

    local status=0
    if COWRIE_SSH_CONTROL_PATH="$CONTROL_PATH" \
        timeout --kill-after=1s 22s "$SCRIPT_DIR/deploy.sh" --check; then
        return 0
    else
        status=$?
    fi
    case "$status" in
        1|3|4|5|6|7) return "$status" ;;
        2|124|137)
            # A slow remote health script is different from a dead transport.
            if master_alive && remote_alive; then
                echo "Cowrie health check failed while the SSH transport remains available." >&2
                return 7
            fi
            return 2
            ;;
        *) return 7 ;;
    esac
}

validate_group_key_pair() {
    local public_line public_part private_part
    key_material_available || return 1
    public_line="$(cat -- "$GROUP_PUBLIC_KEY")" || return 1
    [[ -n "$public_line" && "$public_line" != *$'\n'* ]] || return 1
    public_part="$(awk 'NF >= 2 { print $1 " " $2; exit }' "$GROUP_PUBLIC_KEY")" || return 1
    private_part="$(ssh-keygen -y -P '' -f "$GROUP_KEY" 2>/dev/null | awk 'NF >= 2 { print $1 " " $2; exit }')" || return 1
    [[ -n "$public_part" && "$public_part" == "$private_part" ]]
}

restore_keys_over_master() {
    if ! validate_group_key_pair; then
        log "Cannot restore authorized_keys: local group key pair is invalid."
        return 1
    fi
    if ! master_alive || ! remote_alive; then
        log "Cannot restore authorized_keys: persistent SSH connection is lost."
        return 1
    fi
    # The current authenticated channel stays usable after authorized_keys
    # changes. Restore the exact expected key before that channel is lost.
    if ! timeout --kill-after=1s 15s ssh "${SSH_MUX[@]}" "$TARGET" \
        'set -eu; umask 077; mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; auth="$HOME/.ssh/authorized_keys"; tmp=$(mktemp "$auth.XXXXXX"); trap "rm -f \"$tmp\"" EXIT; cat > "$tmp"; chmod 600 "$tmp"; mv -f "$tmp" "$auth"; trap - EXIT' \
        < "$GROUP_PUBLIC_KEY"; then
        log "Failed to restore authorized_keys over the persistent connection."
        return 1
    fi
    log "Restored authorized_keys over the persistent connection."
}

clear_retry_state() {
    rm -f -- "$RETRY_FILE"
}

# One attempt per wall-clock minute until recovery succeeds.
# The monitor's 0-20 second jitter spreads
# each permitted attempt within its minute; its heartbeat continues throughout.
allow_reconnect_attempt() {
    local now last_attempt=0 last_slot=-1 current_slot retry_tmp
    now="$(date '+%s')"
    [[ "$now" =~ ^[0-9]+$ ]] || return 1
    current_slot=$((now / 60))
    if [[ -f "$RETRY_FILE" && ! -L "$RETRY_FILE" ]]; then
        # Preserve the two-field format so existing retry state remains usable.
        read -r last_attempt last_slot < "$RETRY_FILE" || true
    fi
    if [[ ! "$last_slot" =~ ^-?[0-9]+$ ]]; then
        last_slot=-1
    fi
    if (( last_slot == current_slot )); then
        log "Fresh SSH was already retried this minute."
        return 1
    fi
    retry_tmp="$(mktemp "$STATE_DIR/.retry-state.XXXXXX")" || return 1
    printf '%s %s\n' "$now" "$current_slot" > "$retry_tmp"
    mv -f -- "$retry_tmp" "$RETRY_FILE"
}

start_group_master() {
    local start_output='' start_log
    if master_alive; then
        if remote_alive; then
            return 0
        fi
        # A live control process with an unresponsive remote channel must be
        # shut down before its socket is replaced, or it could keep an orphan
        # TCP connection open and defeat the single-connection design.
        log "Persistent SSH master is unresponsive; requesting a clean exit."
        timeout --kill-after=1s 5s ssh -F "$REPO_ROOT/scripts/ssh_config" -S "$CONTROL_PATH" \
            -o ProxyCommand=/bin/false -O exit -p "$PORT" "$TARGET" \
            >/dev/null 2>&1 || true
        if master_alive; then
            log "Existing SSH master has not exited; fresh authentication is deferred."
            return 1
        fi
    fi
    if ! validate_group_key_pair; then
        log "Cannot start persistent SSH: local group key pair is invalid."
        return 4
    fi
    # Only the recovery lock holder removes an abandoned socket.
    if [[ -e "$CONTROL_PATH" || -L "$CONTROL_PATH" ]]; then
        rm -f -- "$CONTROL_PATH"
    fi
    start_log="$(mktemp "$STATE_DIR/.master-start.XXXXXX")" || return 1
    # The background master must not inherit the recovery flock descriptor.
    if timeout --kill-after=1s 20s ssh "${SSH_GROUP[@]}" \
        -N -f -M -S "$CONTROL_PATH" -o ControlMaster=yes \
        -o ControlPersist=yes "$TARGET" > "$start_log" 2>&1 9>&-; then
        if master_alive && remote_alive; then
            rm -f -- "$start_log"
            log "Persistent group-key SSH connection established."
            return 0
        fi
        start_output='master started but its control channel is unavailable'
    else
        start_output="$(cat -- "$start_log")"
    fi
    rm -f -- "$start_log"
    START_ERROR="$start_output"
    log "Persistent group-key SSH connection failed: ${start_output:-unknown SSH error}."
    return 1
}

repair_over_master() {
    local status=0
    if check_health; then
        clear_retry_state
        return 0
    else
        status=$?
    fi
    case "$status" in
        2|4|5|7) return "$status" ;;
        3)
            restore_keys_over_master || return 1
            ;;
    esac
    status=0
    if COWRIE_SSH_CONTROL_PATH="$CONTROL_PATH" "$SCRIPT_DIR/deploy.sh"; then
        clear_retry_state
        return 0
    else
        status=$?
    fi
    return "$status"
}

recover() {
    local status=0 start_status=0 student_output=''
    START_ERROR=''
    if check_health; then
        clear_retry_state
        return 0
    else
        status=$?
    fi
    if (( status == 4 )); then
        log "Persistent recovery cannot run without the local group key material."
        return 1
    fi
    if (( status == 5 || status == 7 )); then
        return "$status"
    fi
    if (( status != 2 )) && master_alive && remote_alive; then
        repair_over_master
        return $?
    fi

    if ! allow_reconnect_attempt; then
        return 1
    fi
    if start_group_master; then
        repair_over_master
        return $?
    else
        start_status=$?
    fi
    if (( start_status == 4 )); then
        return 1
    fi
    # A transport failure or firewall block is not evidence that the group key
    # was rejected. Only an explicit authentication denial justifies testing
    # the student key and running bootstrap recovery.
    if [[ "$START_ERROR" != *"Permission denied"* ]]; then
        return 1
    fi
    if [[ ! -s "$STUDENT_KEY" || ! -r "$STUDENT_KEY" ]]; then
        log "Student key is unavailable; bootstrap recovery was not attempted."
        return 1
    fi
    if ! student_output="$(timeout --kill-after=1s 15s ssh "${SSH_STUDENT[@]}" \
        "$TARGET" true 2>&1)"; then
        log "Student-key authentication failed; bootstrap recovery was not attempted."
        return 1
    fi
    log "Only the student key authenticated; restoring group-key access."
    if ! "$REPO_ROOT/scripts/ssh_key_access.sh"; then
        log "Student-key bootstrap recovery failed."
        return 1
    fi
    if ! start_group_master; then
        log "Group-key access was repaired, but the persistent SSH connection did not start."
        return 1
    fi
    repair_over_master
}

stop_master() {
    local attempt
    if master_alive; then
        if ! timeout --kill-after=1s 5s ssh -F "$REPO_ROOT/scripts/ssh_config" -S "$CONTROL_PATH" \
            -o ProxyCommand=/bin/false -O exit -p "$PORT" "$TARGET" \
            >/dev/null 2>&1; then
            log "Could not stop the persistent SSH master."
            return 1
        fi
        for attempt in 1 2 3 4 5; do
            if ! master_alive; then
                break
            fi
            sleep 1
        done
        if master_alive; then
            log "Persistent SSH master did not exit."
            return 1
        fi
    fi
    if [[ -e "$CONTROL_PATH" || -L "$CONTROL_PATH" ]]; then
        rm -f -- "$CONTROL_PATH"
    fi
    if [[ "$mode" != --stop-keep-state ]]; then
        clear_retry_state
    fi
    log "Persistent SSH master stopped."
}

if [[ "$mode" == --check ]]; then
    check_health
    exit $?
fi

exec 9>"$STATE_DIR/recovery.lock"
if [[ "$mode" == --stop || "$mode" == --stop-keep-state ]]; then
    if ! flock -w 30 9; then
        log "Persistent recovery is busy; the SSH master was not stopped."
        exit 1
    fi
    stop_master
    exit $?
fi
if ! flock -n 9; then
    log "Another persistent recovery is running."
    if [[ "$mode" == --initialize ]]; then
        exit 1
    fi
    exit 0
fi
# App restoration takes priority over Cowrie access and deployment repairs.
if ! python3 "$REPO_ROOT/scripts/resumelens_scheduler.py" prepare-honeypot; then
    log "App recovery is pending; deferring honeypot recovery to the next check."
    exit 1
fi

recover
