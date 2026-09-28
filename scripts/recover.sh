#!/bin/bash

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

MY_KEY="$HOME/.ssh/cs553_vm_key"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "$(date): checking VM"

if ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=6 \
    -i "$MY_KEY" \
    -p "$PORT" \
    "$REMOTE_USER@$MACHINE" \
    exit
then
    echo "$(date): SSH connection works."
else
    echo "$(date): SSH connection failed."
    exit 1
fi

echo "$(date): checking ResumeLens"

if "$SCRIPT_DIR/health_check.sh"; then
    echo "$(date): nothing to do."
else
    echo "$(date): application failed. Redeploying..."

    "$SCRIPT_DIR/deploy_app.sh"

    sleep 10

    "$SCRIPT_DIR/health_check.sh"
fi
