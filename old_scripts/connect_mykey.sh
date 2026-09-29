#!/bin/bash
# We created this as the normal connection method after setting up our own SSH key. 
# The idea is that once the VM is secured, we should no longer rely on the shared class key and should authenticate using our own key instead.

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

MY_KEY="$HOME/.ssh/cs553_vm_key"

ssh \
    -i "$MY_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE"
