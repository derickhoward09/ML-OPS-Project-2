#!/bin/bash

set -e

PORT=22001
MACHINE="paffenroth-23.dyn.wpi.edu"
REMOTE_USER="student-admin"

MY_KEY="$HOME/.ssh/cs553_vm_key"

APP_DIR="ML-OPS-Project-2-"
REPO_URL="https://github.com/derickhoward09/ML-OPS-Project-2-.git"

SSH="ssh -i $MY_KEY -p $PORT -o StrictHostKeyChecking=no"

echo "Connecting to VM..."

$SSH "$REMOTE_USER@$MACHINE" "
    sudo apt-get update -qq &&
    sudo apt-get install -y python3-venv git curl
"

echo "Getting application source..."

$SSH "$REMOTE_USER@$MACHINE" "
    if [ -d ~/$APP_DIR/.git ]; then
        git -C ~/$APP_DIR pull --ff-only
    else
        git clone $REPO_URL ~/$APP_DIR
    fi
"

echo "Creating Python environment..."

$SSH "$REMOTE_USER@$MACHINE" "
    cd ~/$APP_DIR &&
    python3 -m venv .venv &&
    ./.venv/bin/pip install --upgrade pip &&
    ./.venv/bin/pip install -r requirements.txt
"

echo "Starting application..."

$SSH "$REMOTE_USER@$MACHINE" "
    pkill -f 'app.py' || true

    cd ~/$APP_DIR

    nohup ./.venv/bin/python app.py \
        > app.log 2>&1 &
"

echo "Deployment complete."

