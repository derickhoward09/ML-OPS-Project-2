#!/bin/bash
# Created this script so we could quickly connect to the VM using the original shared student-admin SSH key. 
# This is mainly for the initial setup phase before replacing the shared key with my own more secure key.

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

BOOTSTRAP_KEY="$HOME/.private/cs553/student-admin_key"

ssh \
    -i "$BOOTSTRAP_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE"

