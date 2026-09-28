#!/bin/bash

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

BOOTSTRAP_KEY="$HOME/.private/cs553/student-admin_key"

ssh \
    -i "$BOOTSTRAP_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE"

