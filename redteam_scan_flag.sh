#!/usr/bin/env bash
#
# CS553 case study 2 — read-only SSH access check.
# Tries the configured key against ports 22002-22025 on the class VM.
# Each node number maps to port 22000 + its number. The script issues only a
# remote `true` command; it does not install files or change remote settings.
# Results are appended to redteam_scan.log beside this script.

set -euo pipefail

HOST="paffenroth-23.dyn.wpi.edu"
SSH_USER="student-admin"
KEY="$HOME/.ssh/mlops/student-admin_key"
TIMEZONE="America/New_York"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/redteam_scan.log"

if [ ! -r "$KEY" ]; then
    echo "ERROR: cannot read SSH key: $KEY" >&2
    exit 1
fi

# Build node 2-25's corresponding SSH ports, then shuffle them in place.
ports=()
for node in {2..25}; do
    ports+=("$((22000 + node))")
done

for ((i = ${#ports[@]} - 1; i > 0; i--)); do
    j=$((RANDOM % (i + 1)))
    temp="${ports[$i]}"
    ports[$i]="${ports[$j]}"
    ports[$j]="$temp"
done

ssh_opts=(
    -T
    -i "$KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o ConnectTimeout=5
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
)

success_nodes=()
for ((index = 0; index < ${#ports[@]}; index++)); do
    port="${ports[$index]}"
    node=$((port - 22000))

    printf 'Trying node %s on port %s... ' "$node" "$port"
    if ssh "${ssh_opts[@]}" -p "$port" "${SSH_USER}@${HOST}" true >/dev/null 2>&1; then
        echo "login succeeded."
        success_nodes+=("node $node (port $port)")
    else
        echo "login failed."
    fi

    if ((index < ${#ports[@]} - 1)); then
        delay=$((RANDOM % 3 + 1))
        sleep "$delay"
    fi
done

if [ "${#success_nodes[@]}" -eq 0 ]; then
    success_summary="none"
else
    success_summary="${success_nodes[0]}"
    for ((index = 1; index < ${#success_nodes[@]}; index++)); do
        success_summary+=", ${success_nodes[$index]}"
    done
fi

timestamp="$(TZ="$TIMEZONE" date '+%Y-%m-%d %H:%M:%S %Z')"
printf '%s | authenticated: %s\n' "$timestamp" "$success_summary" >> "$LOG_FILE"

echo
echo "Authenticated: $success_summary"
echo "Results appended to $LOG_FILE"
