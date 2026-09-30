#!/bin/bash
# Bare-bones VM access watchdog. Edit the 5 lines below for your setup.

HOST="paffenroth-23.dyn.wpi.edu"
PORT="22001"
REMOTE_USER="student-admin"
LOGIN_KEYS=("keys/student-admin_key" "keys/id_ed25519_group_key")
PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKGWyyUyI2AUnczcnNwthAUl/DXfiIZREUdBmKy5BEgY derickhoward@DESKTOP-PVINGAP"

while true; do
    echo "[$(TZ=America/New_York date '+%Y-%m-%d %I:%M:%S %p %Z')] Checking VM access."
    SUCCESS=false
    for LOGIN_KEY in "${LOGIN_KEYS[@]}"; do
        if ssh -o BatchMode=yes -o ConnectTimeout=6 -i "$LOGIN_KEY" -p "$PORT" \
            "$REMOTE_USER@$HOST" "umask 077; printf '%s\\n' '$PUBLIC_KEY' > ~/.ssh/authorized_keys"; then
            echo "OK, $LOGIN_KEY works and authorized_keys is up to date."
            SUCCESS=true
            break
        else
            echo "$LOGIN_KEY failed."
        fi
    done
    if [ "$SUCCESS" = false ]; then
        echo "All keys failed; authorized_keys was not changed."
    fi

    sleep 60
done

