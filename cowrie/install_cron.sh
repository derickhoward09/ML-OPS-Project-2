#!/usr/bin/env bash
# Install one Cowrie monitor mode and the existing redteam scan schedule.
set -Eeuo pipefail

mode=minute
dry_run=false
while (( $# > 0 )); do
    case "$1" in
        --mode)
            if (( $# < 2 )); then
                echo "ERROR: --mode requires minute or persistent." >&2
                exit 2
            fi
            mode="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        --help)
            echo "Usage: $0 [--mode minute|persistent] [--dry-run]"
            exit 0
            ;;
        *)
            echo "Usage: $0 [--mode minute|persistent] [--dry-run]" >&2
            exit 2
            ;;
    esac
done
if [[ "$mode" != minute && "$mode" != persistent ]]; then
    echo "ERROR: mode must be minute or persistent." >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REDTEAM_SCRIPT="$REPO_ROOT/redteam_scan_flag.sh"

for command_name in crontab awk mktemp; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $command_name" >&2
        exit 1
    fi
done
if [[ ! -f "$REDTEAM_SCRIPT" || ! -r "$REDTEAM_SCRIPT" ]]; then
    echo "ERROR: redteam scanner is missing: $REDTEAM_SCRIPT" >&2
    exit 1
fi

temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
current_file="$temp_dir/current"
error_file="$temp_dir/error"
new_file="$temp_dir/new"

if ! crontab -l > "$current_file" 2> "$error_file"; then
    if ! grep -qi 'no crontab' "$error_file"; then
        cat "$error_file" >&2
        exit 1
    fi
    : > "$current_file"
fi

# Replace both Cowrie modes, legacy direct repair jobs, and old redteam scan
# entries. Preserve every unrelated active or commented crontab line.
awk '
    /# redteam-scan-cron([[:space:]]|$)/ { next }
    /^[[:space:]]*#/ { print; next }
    /ssh_key_access[.]sh|reconcile_cowrie[.]sh|monitor_cowrie[.]sh|cowrie\/reconcile[.]sh|cowrie\/monitor[.]sh|cowrie\/persistent[.]sh/ { next }
    /redteam_scan_flag[.]sh/ { next }
    { print }
' "$current_file" > "$new_file"

printf '*/15 * * * * /bin/bash "%s" --run-scheduled >> "%s/redteam_scan_cron.log" 2>&1 # redteam-scan-cron\n' \
    "$REDTEAM_SCRIPT" "$REPO_ROOT" >> "$new_file"
if [[ "$mode" == persistent ]]; then
    printf '* * * * * /bin/bash "%s/monitor.sh" --persistent >> "%s/logs/cowrie_persistent_monitor.log" 2>&1 # cowrie-persistent-monitor\n' \
        "$SCRIPT_DIR" "$REPO_ROOT" >> "$new_file"
else
    printf '* * * * * /bin/bash "%s/monitor.sh" >> "%s/logs/cowrie_monitor.log" 2>&1 # cowrie-monitor\n' \
        "$SCRIPT_DIR" "$REPO_ROOT" >> "$new_file"
fi

if "$dry_run"; then
    cat "$new_file"
    exit 0
fi

mkdir -p -- "$REPO_ROOT/logs"
crontab "$new_file"

# Read back the installed crontab so init callers know the selected mode and
# redteam schedule actually reached cron.
if ! crontab -l > "$temp_dir/installed"; then
    echo "ERROR: crontab was submitted but could not be verified." >&2
    exit 1
fi
if ! cmp -s -- "$new_file" "$temp_dir/installed"; then
    echo "ERROR: installed crontab differs from the submitted schedule." >&2
    exit 1
fi
echo "Installed $mode Cowrie monitoring and the redteam 45-minute scan schedule for $(id -un)."
