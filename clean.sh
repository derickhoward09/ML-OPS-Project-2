#!/usr/bin/env bash
# Remove this checkout's scheduled jobs and stop its persistent SSH master.
set -Eeuo pipefail
umask 077

dry_run=false
case "${1:-}" in
    "") ;;
    --dry-run) dry_run=true ;;
    --help|-h)
        echo "Usage: $0 [--dry-run]"
        exit 0
        ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    echo "Usage: $0 [--dry-run]" >&2
    exit 2
fi

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PERSISTENT_SCRIPT="$REPO_ROOT/cowrie/persistent.sh"
MONITOR_STATE_DIR="${COWRIE_MONITOR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-monitor}"
for command_name in crontab awk mktemp cmp; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
current_file="$temp_dir/current"
cleaned_file="$temp_dir/cleaned"
error_file="$temp_dir/error"
had_crontab=true
if ! crontab -l > "$current_file" 2> "$error_file"; then
    if ! grep -qi 'no crontab' "$error_file"; then
        cat "$error_file" >&2
        exit 1
    fi
    had_crontab=false
    : > "$current_file"
fi
# A cron command belongs to this checkout when it references its absolute
# path. Keep comments, environment assignments, and other projects' jobs,
# even when those projects use the same script basenames or cron tags.
awk -v root="$REPO_ROOT" '
    {
        trimmed = $0
        sub(/^[[:space:]]*/, "", trimmed)
        if (trimmed ~ /^([*@]|[0-9])/ && index($0, root "/") > 0) {
            next
        }
        print
    }
' "$current_file" > "$cleaned_file"

if "$dry_run"; then
    cat "$cleaned_file"
    exit 0
fi

if [[ ! -f "$PERSISTENT_SCRIPT" || ! -r "$PERSISTENT_SCRIPT" ]]; then
    echo "ERROR: cannot stop persistent SSH; missing $PERSISTENT_SCRIPT" >&2
    exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
    echo "ERROR: flock is required to wait for any running monitor." >&2
    exit 1
fi
if [[ -L "$MONITOR_STATE_DIR" ]]; then
    echo "ERROR: monitor state directory must not be a symlink." >&2
    exit 1
fi
mkdir -p -- "$MONITOR_STATE_DIR"
chmod 700 "$MONITOR_STATE_DIR"
exec 8>"$MONITOR_STATE_DIR/monitor.lock"
if ! flock -w 70 8; then
    echo "ERROR: a monitor is still running; cron and SSH were not changed." >&2
    exit 1
fi

if "$had_crontab" && ! cmp -s -- "$current_file" "$cleaned_file"; then
    crontab "$cleaned_file"
    if ! crontab -l > "$temp_dir/installed"; then
        echo "ERROR: crontab was changed but could not be verified; SSH master was not stopped." >&2
        exit 1
    fi
    if ! cmp -s -- "$cleaned_file" "$temp_dir/installed"; then
        echo "ERROR: installed crontab differs from the cleaned version; SSH master was not stopped." >&2
        exit 1
    fi
fi

if ! /bin/bash "$PERSISTENT_SCRIPT" --stop-keep-state; then
    echo "ERROR: cron cleanup finished, but the persistent SSH master could not be stopped." >&2
    exit 1
fi
echo "Removed this repository's cron jobs and stopped its persistent SSH master."
