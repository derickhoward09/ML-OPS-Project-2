#!/usr/bin/env bash
# Check management SSH recovery once per reconciler run.
# The reconciler runs once a minute; no separate cron job is needed.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
HOST=paffenroth-23.dyn.wpi.edu
PORT=23001

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

if ! probe_management_port; then
    log "Management SSH port $PORT is unavailable; the next minute's cron run will retry."
    exit 1
fi

if ! "$REPO_ROOT/scripts/ssh_key_access.sh"; then
    log "Key recovery failed on port $PORT; the next minute's cron run will retry."
    exit 1
fi

exit 0
