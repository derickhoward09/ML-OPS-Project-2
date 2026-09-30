#!/bin/bash
# Reverses everything deploy_case_study1.sh does.
#
# Removes: the systemd service + unit file, the env file, the firewall rule
# it added (if any), the recorded deploy-state file, and (unless --keep-repo
# is passed) the cloned app directory including its venv.
#
# Does NOT touch the NVIDIA driver by default, since it's shared system
# state other work on the VM may depend on. Pass --purge-nvidia to also
# remove it, but only if this script's earlier run is what installed it.
#
# Usage:
#   ./deploy_case_study1_cleanup.sh                # interactive, asks before deleting the repo
#   ./deploy_case_study1_cleanup.sh --yes           # non-interactive, deletes everything below
#   ./deploy_case_study1_cleanup.sh --keep-repo     # leaves the cloned repo/venv in place
#   ./deploy_case_study1_cleanup.sh --purge-nvidia  # also remove the NVIDIA driver, if this
#                                                    # deploy script is what installed it

set -euo pipefail

SERVICE_NAME="resumelens"
ENV_FILE="/etc/resumelens.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
STATE_FILE="/etc/resumelens.deploy_state"
DEFAULT_APP_DIR="$HOME/cs553-case-study-1"

ASSUME_YES="no"
KEEP_REPO="no"
PURGE_NVIDIA="no"

for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES="yes" ;;
        --keep-repo) KEEP_REPO="yes" ;;
        --purge-nvidia) PURGE_NVIDIA="yes" ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

log() { printf '\n==> %s\n' "$1"; }

confirm() {
    # confirm "prompt" — returns success (0) if the user agrees, or if -y/--yes was passed.
    [ "$ASSUME_YES" = "yes" ] && return 0
    read -r -p "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
log "Checking sudo access"
if sudo -n true 2>/dev/null; then
    echo "Passwordless sudo confirmed."
else
    echo "WARNING: passwordless sudo not available for $(whoami); you may be prompted for your password." >&2
fi

# ---------------------------------------------------------------------------
APP_DIR="$DEFAULT_APP_DIR"
UFW_RULE_ADDED="no"
NVIDIA_DRIVER_INSTALLED_BY_SCRIPT="no"
PORT=""
if [ -f "$STATE_FILE" ]; then
    log "Reading deploy state ($STATE_FILE)"
    # shellcheck disable=SC1090
    source "$STATE_FILE"
else
    echo "No deploy-state file found; using defaults (APP_DIR=$APP_DIR)."
fi

# ---------------------------------------------------------------------------
log "Stopping and removing the systemd service"
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    sudo systemctl stop "$SERVICE_NAME" || true
    sudo systemctl disable "$SERVICE_NAME" || true
else
    echo "Service $SERVICE_NAME not installed, skipping."
fi
if [ -f "$SERVICE_FILE" ]; then
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload
fi

# ---------------------------------------------------------------------------
log "Removing environment file"
[ -f "$ENV_FILE" ] && sudo rm -f "$ENV_FILE"

# ---------------------------------------------------------------------------
log "Removing firewall rule"
if [ "$UFW_RULE_ADDED" = "yes" ] && [ -n "$PORT" ] && command -v ufw >/dev/null 2>&1; then
    sudo ufw delete allow "${PORT}/tcp" || true
else
    echo "No firewall rule recorded, skipping."
fi

# ---------------------------------------------------------------------------
log "Removing cloned application directory"
if [ "$KEEP_REPO" = "yes" ]; then
    echo "Skipping (--keep-repo passed): $APP_DIR"
elif [ -d "$APP_DIR" ]; then
    if confirm "Delete $APP_DIR (includes the git clone and its venv)?"; then
        rm -rf "$APP_DIR"
        echo "Removed $APP_DIR"
    else
        echo "Left in place: $APP_DIR"
    fi
else
    echo "Not found, skipping: $APP_DIR"
fi

# ---------------------------------------------------------------------------
log "Removing deploy-state file"
[ -f "$STATE_FILE" ] && sudo rm -f "$STATE_FILE"

# ---------------------------------------------------------------------------
if [ "$PURGE_NVIDIA" = "yes" ]; then
    log "Purging NVIDIA driver"
    if [ "$NVIDIA_DRIVER_INSTALLED_BY_SCRIPT" != "yes" ]; then
        echo "Deploy state says this script did not install the NVIDIA driver; leaving it alone."
        echo "Pass --purge-nvidia together with a deploy-state showing it was installed here, or remove it manually."
    elif confirm "This will remove NVIDIA driver packages system-wide. Continue?"; then
        sudo apt-get purge -y 'nvidia-driver-*' 'nvidia-utils-*' 'nvidia-dkms-*' ubuntu-drivers-common
        sudo apt-get autoremove -y
        echo "NVIDIA driver packages removed. A reboot is recommended."
    else
        echo "Skipped NVIDIA driver removal."
    fi
else
    log "NVIDIA driver left untouched (pass --purge-nvidia to remove it)"
fi

# ---------------------------------------------------------------------------
log "Cleanup complete"
echo "Re-run deploy_case_study1.sh at any time to redeploy from scratch."
