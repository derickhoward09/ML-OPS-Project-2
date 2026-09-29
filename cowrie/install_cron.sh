#!/usr/bin/env bash
# Install the combined Cowrie monitor/reconciler in the current user's crontab.
set -Eeuo pipefail

if (( $# > 1 )) || { (( $# == 1 )) && [[ "$1" != --dry-run && "$1" != --help ]]; }; then
    echo "Usage: $0 [--dry-run|--help]" >&2
    exit 2
fi
if [[ "${1:-}" == --help ]]; then
    echo "Usage: $0 [--dry-run]"
    exit 0
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
mkdir -p "$REPO_ROOT/logs"

current_file="$(mktemp)"
error_file="$(mktemp)"
new_file="$(mktemp)"
trap 'rm -f "$current_file" "$error_file" "$new_file"' EXIT

if ! crontab -l > "$current_file" 2> "$error_file"; then
    if ! grep -qi 'no crontab' "$error_file"; then
        cat "$error_file" >&2
        exit 1
    fi
    : > "$current_file"
fi

# Replace old direct key-repair jobs and the two previous Cowrie jobs. Other
# crontab entries, including unrelated class jobs, are preserved verbatim.
awk '
    /^[[:space:]]*#/ { print; next }
    /ssh_key_access[.]sh|reconcile_cowrie[.]sh|monitor_cowrie[.]sh|cowrie\/reconcile[.]sh|cowrie\/monitor[.]sh/ { next }
    { print }
' "$current_file" > "$new_file"

cat >> "$new_file" <<EOF
* * * * * /bin/bash "$SCRIPT_DIR/monitor.sh" >> "$REPO_ROOT/logs/cowrie_monitor.log" 2>&1 # cowrie-monitor
EOF

if [[ "${1:-}" == --dry-run ]]; then
    cat "$new_file"
else
    crontab "$new_file"
    echo "Installed the Cowrie monitor and reconciler for $(id -un)."
fi
