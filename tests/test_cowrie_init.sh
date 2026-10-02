#!/usr/bin/env bash
set -Eeuo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/bin"
cat > "$FIXTURE_DIR/bin/crontab" <<'MOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == -l ]]; then cat "$MOCK_CRONTAB"; else cp "$1" "$MOCK_CRONTAB"; fi
MOCK
chmod +x "$FIXTURE_DIR/bin/crontab"
cat > "$FIXTURE_DIR/crontab" <<'CRONTAB'
# preserved comment
0 * * * * /home/user/unrelated.sh
CRONTAB
export PATH="$FIXTURE_DIR/bin:$PATH"
export MOCK_CRONTAB="$FIXTURE_DIR/crontab"

# Dry run has no prompts, network calls, config writes, or crontab mutation.
bash "$REPO_ROOT/init.sh" --dry-run > "$FIXTURE_DIR/preview"
grep -q '# cowrie-monitor' "$FIXTURE_DIR/preview"
grep -q '# redteam-scan-cron' "$FIXTURE_DIR/preview"
grep -q '# resumelens-recovery' "$FIXTURE_DIR/preview"
grep -q 'unrelated.sh' "$FIXTURE_DIR/preview"
! grep -q '# resumelens-recovery' "$MOCK_CRONTAB"

# Installation is idempotent, preserves unrelated lines, and includes one job
# for each workflow. No redteam scan is executed by cron installation.
bash "$REPO_ROOT/cowrie/install_cron.sh" --mode minute
bash "$REPO_ROOT/cowrie/install_cron.sh" --mode minute
[[ "$(grep -c '# cowrie-monitor' "$MOCK_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# redteam-scan-cron' "$MOCK_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# resumelens-recovery' "$MOCK_CRONTAB")" -eq 1 ]]
grep -q 'unrelated.sh' "$MOCK_CRONTAB"
echo 'Unified initializer cron preview and installation passed.'
