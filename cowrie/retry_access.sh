#!/usr/bin/env bash
# Retry management SSH recovery while node 24 comes back after a rebuild.
# Called by the once-a-minute, locked reconciler; no separate cron job needed.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
HOST=paffenroth-23.dyn.wpi.edu
PORT=22024
RETRY_INTERVAL=3
RETRY_WINDOW=55
REPAIR_INTERVAL=15

if (( $# != 0 )); then
    echo "Usage: $0" >&2
    exit 2
fi

for command_name in timeout python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

probe_management_port() {
    # Bound DNS and TCP connection time. This is the real management port,
    # not the deliberately delayed Cowrie listener on public port 22001.
    timeout --kill-after=1s 3s python3 - "$HOST" "$PORT" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2):
        pass
except OSError:
    sys.exit(1)
PY
}

deadline=$((SECONDS + RETRY_WINDOW))
port_was_unavailable=true
next_repair_at=0
while (( SECONDS < deadline )); do
    probe_started=$SECONDS
    repair_attempted=false
    if probe_management_port; then
        if "$port_was_unavailable" || (( SECONDS >= next_repair_at )); then
            port_was_unavailable=false
            repair_attempted=true
            if "$REPO_ROOT/ssh_key_access.sh"; then
                exit 0
            fi
            next_repair_at=$((SECONDS + REPAIR_INTERVAL))
            log "Key recovery failed on port $PORT; continuing three-second port checks."
        fi
    else
        port_was_unavailable=true
    fi

    # TCP probes start roughly three seconds apart. A key repair can take
    # longer; resume probing three seconds after it finishes.
    if "$repair_attempted"; then
        delay=$RETRY_INTERVAL
    else
        delay=$((RETRY_INTERVAL - (SECONDS - probe_started)))
    fi
    (( SECONDS < deadline )) || break
    if (( delay > 0 )); then
        sleep "$delay"
    fi
done

log "Management SSH access is still unavailable; the next cron run will retry."
exit 1
