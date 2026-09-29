#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/repo/cowrie" "$FIXTURE_DIR/bin"
cp "$REPO_ROOT/cowrie/install_cron.sh" "$FIXTURE_DIR/repo/cowrie/install_cron.sh"

cat > "$FIXTURE_DIR/current-crontab" <<'CRONTAB'
# keep unrelated entries
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
    exit 0
fi
echo "Unexpected crontab mutation in dry-run test." >&2
exit 1
MOCK_CRONTAB
chmod +x "$FIXTURE_DIR/bin/crontab"

output="$(PATH="$FIXTURE_DIR/bin:$PATH" MOCK_CURRENT_CRONTAB="$FIXTURE_DIR/current-crontab" \
    bash "$FIXTURE_DIR/repo/cowrie/install_cron.sh" --dry-run)"
[[ "$output" == *"# keep unrelated entries"* ]]
[[ "$output" == *"*/10 * * * * /home/user/redteam_scan_flag.sh --run-scheduled"* ]]
[[ "$output" == *"# cowrie-monitor"* ]]
[[ "$(printf '%s\n' "$output" | grep -c '# cowrie-monitor')" -eq 1 ]]
[[ "$output" != *"cowrie-reconcile"* ]]
[[ "$output" != *"ssh_key_access.sh"* ]]

echo 'Cowrie cron installer: installs one coordinator entry and preserves unrelated jobs.'
