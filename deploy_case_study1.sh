#!/bin/bash
# Deploys CS553 Case Study 1 ("ResumeLens AI") on a fresh VM.
#
# The upstream app (github.com/alexander-krett/cs553-case-study-1) bundles
# both Case Study 1 deliverables into one Gradio app.py, selected at runtime:
#   - "Local"  mode = the locally-executed product (transformers on cuda)
#   - "Remote" mode = the API-based product (HF Inference API)
#
# Two changes are needed to run it standalone on a VM instead of as a real
# Hugging Face Space (both are applied idempotently below, see PATCH 1/2):
#   1. Remote mode normally authenticates via HF OAuth login, which only
#      works when hosted on huggingface.co. We patch it to also accept an
#      HF_TOKEN environment variable so the API-based product works here.
#   2. Gradio binds to 127.0.0.1 by default. We patch it to bind 0.0.0.0 on
#      a configurable port so the app is reachable from outside the VM.
#
# Usage:
#   HF_TOKEN=hf_xxx ./deploy_case_study1.sh
#   PORT=7860 HF_TOKEN=hf_xxx ./deploy_case_study1.sh
#
# Safe to re-run: pulls latest instead of re-cloning, skips patches already
# applied, and just restarts the systemd service.
#
# To reverse everything this script does, run deploy_case_study1_cleanup.sh.

set -euo pipefail

REPO_URL="https://github.com/alexander-krett/cs553-case-study-1.git"
APP_DIR="${APP_DIR:-$HOME/cs553-case-study-1}"
SERVICE_NAME="resumelens"
PORT="${PORT:-7860}"
HF_TOKEN="${HF_TOKEN:-}"
ENV_FILE="/etc/resumelens.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/resumelens.deploy_state"

log() { printf '\n==> %s\n' "$1"; }

NVIDIA_DRIVER_INSTALLED_BY_SCRIPT="no"
NVIDIA_REBOOT_REQUIRED="no"
UFW_RULE_ADDED="no"

# Preserve the "did we install the driver" flag across re-runs (e.g. this run
# might find the driver already working, but a prior run is what installed it).
if [ -f "$STATE_FILE" ]; then
    PREV_NVIDIA_FLAG="$(grep -m1 '^NVIDIA_DRIVER_INSTALLED_BY_SCRIPT=' "$STATE_FILE" | cut -d= -f2 || true)"
    [ "$PREV_NVIDIA_FLAG" = "yes" ] && NVIDIA_DRIVER_INSTALLED_BY_SCRIPT="yes"
fi

# ---------------------------------------------------------------------------
log "Checking sudo access"
if sudo -n true 2>/dev/null; then
    echo "Passwordless sudo confirmed."
else
    echo "WARNING: passwordless sudo not available for $(whoami)." >&2
    echo "You may be prompted for your password during installation steps below." >&2
fi

# ---------------------------------------------------------------------------
log "Resolving Hugging Face token for API-based (Remote) mode"
# Note: this token is required even to START the app, not just to use Remote
# mode. Gradio's gr.LoginButton() mocks a local login by calling HF's
# whoami() with this token at startup (since we're not on huggingface.co);
# an invalid token crashes the whole app, not just Remote inference.
if [ -z "$HF_TOKEN" ] && [ -f "$HOME/.cache/huggingface/token" ]; then
    HF_TOKEN="$(tr -d '[:space:]' < "$HOME/.cache/huggingface/token")"
    echo "Using token found at ~/.cache/huggingface/token"
fi
while true; do
    if [ -z "$HF_TOKEN" ]; then
        read -r -s -p "Enter a Hugging Face token (required to start the app; input hidden): " HF_TOKEN
        echo
        HF_TOKEN="$(printf '%s' "$HF_TOKEN" | tr -d '[:space:]')"
    fi
    if [ -z "$HF_TOKEN" ]; then
        echo "WARNING: no HF_TOKEN provided. The app will crash on startup without one (see comment above)." >&2
        break
    fi
    HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${HF_TOKEN}" \
        https://huggingface.co/api/whoami-v2 || true)"
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Token validated against huggingface.co."
        break
    fi
    echo "WARNING: Hugging Face rejected this token (HTTP ${HTTP_CODE})." >&2
    echo "Generate a valid one at https://huggingface.co/settings/tokens" >&2
    if [ -t 0 ]; then
        HF_TOKEN=""  # re-prompt
    else
        echo "Non-interactive session: proceeding anyway, but the app will crash on startup." >&2
        break
    fi
done

# ---------------------------------------------------------------------------
log "Installing system dependencies"
sudo apt-get update -y
sudo apt-get install -y git python3 python3-venv python3-pip build-essential

log "GPU driver check"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    nvidia-smi
else
    echo "nvidia-smi not working; attempting to install the NVIDIA driver."
    sudo apt-get install -y ubuntu-drivers-common
    if sudo ubuntu-drivers autoinstall; then
        NVIDIA_DRIVER_INSTALLED_BY_SCRIPT="yes"
    else
        echo "WARNING: 'ubuntu-drivers autoinstall' failed. Local (GPU) inference mode will not work." >&2
    fi

    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        nvidia-smi
    else
        NVIDIA_REBOOT_REQUIRED="yes"
        echo "WARNING: driver package installed but the kernel module isn't loaded yet." >&2
        echo "         A reboot (sudo reboot) is required before Local (GPU) mode will work." >&2
        echo "         Re-run this script after rebooting to verify." >&2
    fi
fi

# ---------------------------------------------------------------------------
log "Fetching application code"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------------------
log "Setting up Python virtual environment"
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
# shellcheck disable=SC1091
source "$APP_DIR/venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$APP_DIR/requirements.txt"
deactivate

# ---------------------------------------------------------------------------
log "Patching app.py for standalone VM deployment"
python3 - "$APP_DIR/app.py" <<'PYEOF'
import re
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    src = f.read()

marker = "# VM-DEPLOY PATCH"
if marker in src:
    print("Patches already applied, skipping.")
else:
    # Patch 1: allow Remote/API mode to authenticate via HF_TOKEN env var
    # when no HF OAuth token is available (OAuth only works on huggingface.co).
    old_check = (
        '    if hf_token is None:\n'
        '        raise gr.Error("Please sign in with Hugging Face before using a remote model.")\n'
    )
    new_check = (
        f'    {marker}: fall back to HF_TOKEN env var when OAuth is unavailable off HF Spaces.\n'
        '    token = hf_token.token if hf_token is not None else os.getenv("HF_TOKEN")\n'
        '    if not token:\n'
        '        raise gr.Error("Please sign in with Hugging Face before using a remote model.")\n'
    )
    if old_check not in src:
        sys.exit("ERROR: could not find expected token-check block in app.py; app.py may have changed upstream.")
    src = src.replace(old_check, new_check, 1)

    old_call = (
        '            response = _run_remote_model(\n'
        '                model_id, hf_token.token, prompt, max_tokens, temperature\n'
        '            )\n'
    )
    new_call = (
        '            response = _run_remote_model(\n'
        '                model_id, token, prompt, max_tokens, temperature\n'
        '            )\n'
    )
    if old_call not in src:
        sys.exit("ERROR: could not find expected _run_remote_model call in app.py; app.py may have changed upstream.")
    src = src.replace(old_call, new_call, 1)

    # Patch 2: bind externally on a configurable port instead of 127.0.0.1.
    old_launch = "    demo.launch()\n"
    new_launch = (
        f'    {marker}: bind externally so the VM can serve this app.\n'
        '    demo.launch(\n'
        '        server_name="0.0.0.0",\n'
        '        server_port=int(os.getenv("PORT", "7860")),\n'
        '    )\n'
    )
    if old_launch not in src:
        sys.exit("ERROR: could not find expected demo.launch() call in app.py; app.py may have changed upstream.")
    src = src.replace(old_launch, new_launch, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("Applied VM-deploy patches to app.py.")
PYEOF

# ---------------------------------------------------------------------------
log "Configuring firewall (if ufw is active)"
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow "${PORT}/tcp"
    UFW_RULE_ADDED="yes"
fi

# ---------------------------------------------------------------------------
log "Writing environment file ($ENV_FILE)"
sudo tee "$ENV_FILE" >/dev/null <<EOF
HF_TOKEN=${HF_TOKEN}
PORT=${PORT}
EOF
sudo chmod 600 "$ENV_FILE"
sudo chown root:root "$ENV_FILE"

# ---------------------------------------------------------------------------
log "Recording deploy state (for the cleanup script)"
sudo tee "$STATE_FILE" >/dev/null <<EOF
APP_DIR=${APP_DIR}
PORT=${PORT}
UFW_RULE_ADDED=${UFW_RULE_ADDED}
NVIDIA_DRIVER_INSTALLED_BY_SCRIPT=${NVIDIA_DRIVER_INSTALLED_BY_SCRIPT}
EOF
sudo chmod 644 "$STATE_FILE"

# ---------------------------------------------------------------------------
log "Installing systemd service ($SERVICE_FILE)"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=CS553 Case Study 1 - ResumeLens AI (Local + Remote inference)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# ---------------------------------------------------------------------------
log "Deployment summary"
sleep 3
sudo systemctl status "$SERVICE_NAME" --no-pager || true

VM_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

App directory:   ${APP_DIR}
Service:         ${SERVICE_NAME} (systemctl status/restart/stop ${SERVICE_NAME})
Logs:            journalctl -u ${SERVICE_NAME} -f
Listening on:    0.0.0.0:${PORT}
Try:             http://${VM_IP:-<vm-host>}:${PORT}

Modifications made to app.py for this VM environment:
  1. Remote (API-based) mode now also accepts an HF_TOKEN environment
     variable (set in ${ENV_FILE}) since HF OAuth login only works when
     the app is hosted on huggingface.co.
  2. demo.launch() now binds 0.0.0.0:\${PORT} (default 7860) instead of
     127.0.0.1, so the app is reachable from outside the VM.
No other files in this app were changed. Local (GPU) mode is unmodified
and requires a working NVIDIA driver (see GPU driver check above).
EOF

if [ "$NVIDIA_REBOOT_REQUIRED" = "yes" ]; then
    cat <<EOF

REBOOT REQUIRED: the NVIDIA driver was just installed but its kernel module
is not loaded yet. Local (GPU) mode will fail until you run 'sudo reboot'
and then re-run this script once to confirm 'nvidia-smi' works.
EOF
fi

echo
echo "To reverse this deployment, run: ./deploy_case_study1_cleanup.sh"
