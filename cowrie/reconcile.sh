#!/usr/bin/env bash
# Run from the scheduler user's cron. Never needs sudo on the scheduler.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-recovery"
mkdir -p "$STATE_DIR"

if ! command -v flock >/dev/null 2>&1; then
    echo "ERROR: flock is required on the Ubuntu scheduler." >&2
    exit 1
fi

exec 9>"$STATE_DIR/reconcile.lock"
if ! flock -n 9; then
    # A previous install can take longer than a one-minute cron interval.
    exit 0
fi

log() {
    printf '%s | %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

if ! "$REPO_ROOT/ssh_key_access.sh"; then
    log "Access recovery failed; Cowrie deployment is deferred."
    exit 1
fi

if "$SCRIPT_DIR/deploy.sh" --check; then
    exit 0
fi

log "Cowrie state is missing or unhealthy; reconciling node 24."
if ! "$SCRIPT_DIR/deploy.sh"; then
    log "Cowrie deployment failed."
    exit 1
fi
if ! "$SCRIPT_DIR/deploy.sh" --check; then
    log "Cowrie deployment finished but the post-check failed."
    exit 1
fi
log "Cowrie is healthy."
