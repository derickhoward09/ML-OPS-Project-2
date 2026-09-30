#!/usr/bin/env bash
# Same as init_minute.sh, but holds an exclusive lock so concurrent
# invocations on this machine cannot overlap.
set -Eeuo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$REPO_ROOT/.init_minute_v2.lock"

if ! command -v flock >/dev/null 2>&1; then
    echo "ERROR: missing required command: flock" >&2
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: another init_minute_v2.sh is already running on this host (lock: $LOCK_FILE); exiting without changes." >&2
    exit 1
fi

exec /bin/bash "$REPO_ROOT/cowrie/init_common.sh" minute "$@"
