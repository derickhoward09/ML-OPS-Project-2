#!/usr/bin/env bash
# Repair Cowrie after monitor.sh records an unhealthy check, or run a full
# reconciliation manually. Never needs sudo on the scheduler.
set -Eeuo pipefail

mode="${1:-full}"
case "$mode" in
    full|--repair-access|--repair-deploy) ;;
    *)
        echo "Usage: $0 [--repair-access|--repair-deploy]" >&2
        exit 2
        ;;
esac
if (( $# > 1 )); then
    echo "Usage: $0 [--repair-access|--repair-deploy]" >&2
    exit 2
fi

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

# App restoration takes priority over Cowrie access and deployment repairs.
if ! python3 "$REPO_ROOT/scripts/resumelens_scheduler.py" prepare-honeypot; then
    log "App recovery is pending; deferring honeypot recovery to the next check."
    exit 1
fi

if [[ "$mode" == --repair-deploy ]]; then
    log "Deployment health check failed; starting repair."
    exec "$SCRIPT_DIR/deploy.sh"
fi

if [[ "$mode" == --repair-access ]]; then
    if ! "$SCRIPT_DIR/retry_access.sh"; then
        log "Access recovery failed; the next monitor run will retry."
        exit 1
    fi
    log "Management access recovered; checking Cowrie deployment."
    exec "$SCRIPT_DIR/deploy.sh"
fi

if ! "$SCRIPT_DIR/retry_access.sh"; then
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
