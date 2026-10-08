#!/usr/bin/env bash
# Install retention on the Ubuntu development VM (requires root).
set -Eeuo pipefail
REPO_ROOT="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
OWNER="${2:-${SUDO_USER:-$(id -un)}}"
if (( EUID != 0 )); then
    echo "Usage: sudo bash $0 [absolute-repo-path] [owner]" >&2
    exit 1
fi
[[ "$REPO_ROOT" == /* && -d "$REPO_ROOT/logs" ]]
[[ "$REPO_ROOT" != *'"'* && "$REPO_ROOT" != *$'\n'* ]]
GROUP="$(id -gn "$OWNER")"
USER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
command -v logrotate >/dev/null
BACKUP="$(mktemp -d /var/backups/log-retention.XXXXXX)"
for path in /etc/logrotate.d/rsyslog /etc/logrotate.d/cs553-repo /etc/logrotate.d/cs553-ops-agent /etc/systemd/journald.conf.d/cs553-retention.conf /etc/systemd/system/logrotate.timer.d/cs553-retention.conf; do
    if [[ -f "$path" ]]; then cp --parents "$path" "$BACKUP/"; fi
done
cat > /etc/logrotate.d/cs553-repo <<EOF
"$REPO_ROOT/logs/*.log" "$REPO_ROOT/*.log" "$USER_HOME/.local/state/cowrie-monitor/*.log" "$USER_HOME/.local/state/cowrie-persistent/*.log" {
    su $OWNER $GROUP
    daily
    maxsize 10M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
cat > /etc/logrotate.d/cs553-ops-agent <<'EOF'
/var/log/google-cloud-ops-agent/subagents/*.log {
    daily
    maxsize 25M
    rotate 5
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
# Keep Ubuntu's reopen hook and log paths; tighten its existing policy.
sed -i -E 's/^[[:space:]]*weekly[[:space:]]*$/\tdaily\n\tmaxsize 25M/; s/^[[:space:]]*rotate [0-9]+/\trotate 7/' /etc/logrotate.d/rsyslog
install -d /etc/systemd/journald.conf.d /etc/systemd/system/logrotate.timer.d
cat > /etc/systemd/journald.conf.d/cs553-retention.conf <<'EOF'
[Journal]
SystemMaxUse=200M
SystemKeepFree=1G
SystemMaxFileSize=25M
MaxRetentionSec=7day
EOF
cat > /etc/systemd/system/logrotate.timer.d/cs553-retention.conf <<'EOF'
[Timer]
OnCalendar=
OnCalendar=*-*-* *:00/15:00
RandomizedDelaySec=0
AccuracySec=1min
EOF
logrotate --debug /etc/logrotate.conf
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now logrotate.timer
systemctl restart logrotate.timer
systemctl start logrotate.service
echo "Installed retention; previous configurations saved in $BACKUP"
