#!/bin/bash

set -e

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

BOOTSTRAP_KEY="$HOME/.private/cs553/student-admin_key"
MY_KEY="$HOME/.ssh/cs553_vm_key"

# Create our SSH key if it does not already exist.
if [ ! -f "$MY_KEY" ]; then
    echo "Creating team SSH key..."

    ssh-keygen \
        -t ed25519 \
        -f "$MY_KEY" \
        -N ""
fi

chmod 600 "$MY_KEY"
chmod 644 "$MY_KEY.pub"

PUBKEY=$(cat "$MY_KEY.pub")

echo "Installing our public key on the VM..."

ssh \
    -i "$BOOTSTRAP_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE" \
    "mkdir -p ~/.ssh &&
     chmod 700 ~/.ssh &&
     printf '%s\n' '$PUBKEY' > ~/.ssh/authorized_keys &&
     chmod 600 ~/.ssh/authorized_keys"

echo "Testing new key..."

ssh \
    -i "$MY_KEY" \
    -p "$PORT" \
    -o StrictHostKeyChecking=no \
    "$REMOTE_USER@$MACHINE" \
    "echo 'New SSH key works.'"
