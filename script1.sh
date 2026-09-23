#!/bin/bash
# Bare-bones VM access watchdog. Edit the 5 lines below for your setup.

HOST="paffenroth-23.dyn.wpi.edu"
PORT="22001"
REMOTE_USER="student-admin"
MY_KEY="Newkeys/pwd"
SHARED_KEY="Keys/student-admin_key"

while true; do
    if ssh -o BatchMode=yes -o ConnectTimeout=6 -i "$MY_KEY" -p "$PORT" "$REMOTE_USER@$HOST" exit; then
        echo "OK, my key still works."
    else
        echo "My key failed, trying to restore access with the shared key..."
        PUBKEY=$(cat "$MY_KEY.pub")
        ssh -i "$SHARED_KEY" -p "$PORT" "$REMOTE_USER@$HOST" "echo '$PUBKEY' >> ~/.ssh/authorized_keys"
    fi

    sleep 60
done

