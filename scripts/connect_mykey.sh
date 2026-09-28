#!/bin/bash

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

MY_KEY="$HOME/.ssh/cs553_vm_key"

ssh \
    -i "$MY_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE"
