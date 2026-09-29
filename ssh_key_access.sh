#!/usr/bin/env bash

set -Eeuo pipefail

# Node 24 maps to SSH port 22024 (22000 + node number).
NODE=24
PORT=$((22000 + NODE))
HOST="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

KEY_DIR="$HOME/.ssh/mlops"
STUDENT_ADMIN_KEY="$KEY_DIR/student-admin_key"
GROUP_KEY="$KEY_DIR/id_ed25519_group_key"
GROUP_PUBLIC_KEY="$KEY_DIR/id_ed25519_group_key.pub"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/ssh_key_access.log"

usage() {
    cat <<EOF
Usage: $0 [--test|-t]

Without --test, ensures the remote authorized_keys contains only the group
public key. The bootstrap key is used only when the group key cannot log in.
With --test, tries both available keys without changing remote files.
EOF
}

TEST_MODE=false
case "${1:-}" in
    "") ;;
    --test|-t) TEST_MODE=true ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    usage >&2
    exit 2
fi

if ! mkdir -p "$LOG_DIR"; then
    echo "ERROR: cannot create log directory: $LOG_DIR" >&2
    exit 1
fi

if ! "$TEST_MODE" && command -v flock >/dev/null 2>&1; then
    LOCK_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-recovery"
    mkdir -p "$LOCK_DIR"
    exec 8>"$LOCK_DIR/access.lock"
    if ! flock -n 8; then
        printf '%s\n' "Another SSH access repair is running; skipping this invocation."
        exit 75
    fi
fi

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
    printf '%s | %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"
}

SSH_OPTIONS=(
    -T
    -F /dev/null
    -p "$PORT"
    -o IdentitiesOnly=yes
    -o CertificateFile=none
    -o BatchMode=yes
    -o PreferredAuthentications=publickey
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o GSSAPIAuthentication=no
    -o HostbasedAuthentication=no
    -o ConnectTimeout=8
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
)

run_ssh() {
    # GNU timeout is available on the Ubuntu scheduler. Keep local use working
    # on systems where it is not installed.
    if command -v timeout >/dev/null 2>&1; then
        timeout 25s ssh "${SSH_OPTIONS[@]}" "$@"
    else
        ssh "${SSH_OPTIONS[@]}" "$@"
    fi
}

try_login() {
    local label="$1"
    local key_file="$2"

    if [[ ! -r "$key_file" || ! -s "$key_file" ]]; then
        log "$label login unavailable: key is missing, empty, or unreadable ($key_file)."
        return 1
    fi

    if run_ssh -i "$key_file" "$REMOTE_USER@$HOST" true \
        >/dev/null 2>&1; then
        log "$label login succeeded."
        return 0
    fi

    log "$label login failed."
    return 1
}

log "Starting SSH key access run for node $NODE on port $PORT."

GROUP_KEY_OK=false
if try_login "group key" "$GROUP_KEY"; then
    GROUP_KEY_OK=true
fi

STUDENT_ADMIN_OK=false
if ! "$GROUP_KEY_OK" || "$TEST_MODE"; then
    if try_login "student-admin key" "$STUDENT_ADMIN_KEY"; then
        STUDENT_ADMIN_OK=true
    fi
fi

if "$TEST_MODE"; then
    if "$STUDENT_ADMIN_OK" || "$GROUP_KEY_OK"; then
        log "Test mode succeeded: at least one key authenticated; no remote files changed."
        exit 0
    fi
    log "Test mode failed: both keys failed to authenticate."
    exit 1
fi

if ! "$STUDENT_ADMIN_OK" && ! "$GROUP_KEY_OK"; then
    log "Run failed: both keys failed to authenticate."
    exit 1
fi

if [[ ! -r "$GROUP_PUBLIC_KEY" || ! -s "$GROUP_PUBLIC_KEY" ]]; then
    log "Run failed: group public key is missing, empty, or unreadable ($GROUP_PUBLIC_KEY)."
    exit 1
fi
if [[ ! -r "$GROUP_KEY" || ! -s "$GROUP_KEY" ]]; then
    log "Run failed: group private key is missing, empty, or unreadable ($GROUP_KEY)."
    exit 1
fi

if ! GROUP_PUBLIC_PART="$(awk 'NF >= 2 { print $1 " " $2; exit }' "$GROUP_PUBLIC_KEY")" ||
   [[ -z "$GROUP_PUBLIC_PART" ]]; then
    log "Run failed: could not read a public key from $GROUP_PUBLIC_KEY."
    exit 1
fi
if ! GROUP_PRIVATE_PART="$(ssh-keygen -y -P '' -f "$GROUP_KEY" 2>/dev/null | awk 'NF >= 2 { print $1 " " $2; exit }')" ||
   [[ -z "$GROUP_PRIVATE_PART" || "$GROUP_PUBLIC_PART" != "$GROUP_PRIVATE_PART" ]]; then
    log "Run failed: group public and private keys do not match or the private key cannot be read without a passphrase."
    exit 1
fi

if "$GROUP_KEY_OK"; then
    if run_ssh -i "$GROUP_KEY" "$REMOTE_USER@$HOST" \
        'cat "$HOME/.ssh/authorized_keys"' 2>> "$LOG_FILE" | cmp -s - "$GROUP_PUBLIC_KEY"; then
        log "Run succeeded: authorized_keys already matches the group public key."
        exit 0
    fi
    UPDATE_KEY="$GROUP_KEY"
    UPDATE_KEY_LABEL="group key"
else
    UPDATE_KEY="$STUDENT_ADMIN_KEY"
    UPDATE_KEY_LABEL="student-admin bootstrap key"
fi

log "Replacing remote authorized_keys using the $UPDATE_KEY_LABEL."
if ! run_ssh -i "$UPDATE_KEY" "$REMOTE_USER@$HOST" \
    'set -eu; umask 077; mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; tmp=$(mktemp "$HOME/.ssh/authorized_keys.XXXXXX"); trap "rm -f \"$tmp\"" EXIT; cat > "$tmp"; chmod 600 "$tmp"; mv -f "$tmp" "$HOME/.ssh/authorized_keys"; trap - EXIT' \
    < "$GROUP_PUBLIC_KEY" 2>> "$LOG_FILE"; then
    log "Run failed: could not replace remote authorized_keys."
    exit 1
fi

if ! run_ssh -i "$GROUP_KEY" "$REMOTE_USER@$HOST" \
    'cat "$HOME/.ssh/authorized_keys"' 2>> "$LOG_FILE" | cmp -s - "$GROUP_PUBLIC_KEY"; then
    log "Run failed: remote authorized_keys does not match the group public key."
    exit 1
fi
log "Remote authorized_keys matches the group public key."

if ! try_login "group key after update" "$GROUP_KEY"; then
    log "Run failed: group key did not authenticate after the update."
    exit 1
fi

log "Run succeeded: authorized_keys was updated and group-key login was verified."
