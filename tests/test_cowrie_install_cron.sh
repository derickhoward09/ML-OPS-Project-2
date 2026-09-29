#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/repo/cowrie" "$FIXTURE_DIR/bin"
cp "$REPO_ROOT/cowrie/install_cron.sh" "$FIXTURE_DIR/repo/cowrie/install_cron.sh"
: > "$FIXTURE_DIR/repo/redteam_scan_flag.sh"

cat > "$FIXTURE_DIR/crontab" <<'CRONTAB'
# keep unrelated entries
0 * * * * /home/user/unrelated.sh
*/10 * * * * /home/user/redteam_scan_flag.sh --run-scheduled
* * * * * /bin/bash /repo/cowrie/reconcile.sh # cowrie-reconcile
* * * * * /bin/bash /repo/cowrie/monitor.sh # cowrie-monitor
* * * * * /bin/bash /repo/ssh_key_access.sh
CRONTAB

cat > "$FIXTURE_DIR/bin/crontab" <<'MOCK_CRONTAB'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == -l ]]; then
    cat "$MOCK_CURRENT_CRONTAB"
else
    cp "$1" "$MOCK_CURRENT_CRONTAB"
    printf 'install\n' >> "$MOCK_EVENTS"
fi
MOCK_CRONTAB
chmod +x "$FIXTURE_DIR/bin/crontab"

export PATH="$FIXTURE_DIR/bin:$PATH"
export MOCK_CURRENT_CRONTAB="$FIXTURE_DIR/crontab"
export MOCK_EVENTS="$FIXTURE_DIR/events"
installer="$FIXTURE_DIR/repo/cowrie/install_cron.sh"

output="$(bash "$installer" --dry-run)"
[[ "$output" == *"# keep unrelated entries"* ]]
[[ "$output" == *"0 * * * * /home/user/unrelated.sh"* ]]
[[ "$output" == *"*/15 * * * * "*"# redteam-scan-cron"* ]]
[[ "$output" == *"# cowrie-monitor"* ]]
[[ "$output" != *"cowrie-reconcile"* ]]
[[ "$output" != *"ssh_key_access.sh"* ]]
[[ ! -e "$MOCK_EVENTS" ]]

bash "$installer" --mode persistent > /dev/null
[[ "$(grep -c '# redteam-scan-cron' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# cowrie-persistent-monitor' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
[[ "$(grep -c -- '--persistent' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
[[ "$(grep -c 'unrelated.sh' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
cp "$MOCK_CURRENT_CRONTAB" "$FIXTURE_DIR/first-install"
bash "$installer" --mode persistent > /dev/null
cmp -s "$MOCK_CURRENT_CRONTAB" "$FIXTURE_DIR/first-install"

bash "$installer" --mode minute > /dev/null
[[ "$(grep -c '# redteam-scan-cron' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# cowrie-monitor' "$MOCK_CURRENT_CRONTAB")" -eq 1 ]]
! grep -q -- '--persistent' "$MOCK_CURRENT_CRONTAB"
! grep -q '# cowrie-persistent-monitor' "$MOCK_CURRENT_CRONTAB"

echo 'Cowrie cron installer: switches modes, preserves unrelated jobs, and keeps one redteam schedule.'
