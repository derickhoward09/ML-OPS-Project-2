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

Without --test, installs the group public key in the remote authorized_keys
file after checking access with both available keys.
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

timestamp() {
    date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
    printf '%s | %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"
}

SSH_OPTIONS=(
    -T
    -p "$PORT"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o ConnectTimeout=8
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
)

try_login() {
    local label="$1"
    local key_file="$2"

    if [[ ! -r "$key_file" || ! -s "$key_file" ]]; then
        log "$label login unavailable: key is missing, empty, or unreadable ($key_file)."
        return 1
    fi

    if ssh "${SSH_OPTIONS[@]}" -i "$key_file" "$REMOTE_USER@$HOST" true \
        >/dev/null 2>&1; then
        log "$label login succeeded."
        return 0
    fi

    log "$label login failed."
    return 1
}

log "Starting SSH key access run for node $NODE on port $PORT."

STUDENT_ADMIN_OK=false
GROUP_KEY_OK=false
if try_login "student-admin key" "$STUDENT_ADMIN_KEY"; then
    STUDENT_ADMIN_OK=true
fi
if try_login "group key" "$GROUP_KEY"; then
    GROUP_KEY_OK=true
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

if "$STUDENT_ADMIN_OK"; then
    UPDATE_KEY="$STUDENT_ADMIN_KEY"
    UPDATE_KEY_LABEL="student-admin key"
else
    UPDATE_KEY="$GROUP_KEY"
    UPDATE_KEY_LABEL="group key"
fi

log "Replacing remote authorized_keys using the $UPDATE_KEY_LABEL."
if ! ssh "${SSH_OPTIONS[@]}" -i "$UPDATE_KEY" "$REMOTE_USER@$HOST" \
    'umask 077; mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && cat > "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"' \
    < "$GROUP_PUBLIC_KEY" 2>> "$LOG_FILE"; then
    log "Run failed: could not replace remote authorized_keys."
    exit 1
fi

if ! ssh "${SSH_OPTIONS[@]}" -i "$GROUP_KEY" "$REMOTE_USER@$HOST" \
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
