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

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

# The access helper performs bounded SSH checks through the jump host.
# A direct TCP probe can fail even when that route is healthy.
if ! "$REPO_ROOT/scripts/ssh_key_access.sh"; then
    log "Key recovery failed on port $PORT; the next minute's cron run will retry."
    exit 1
fi

exit 0
