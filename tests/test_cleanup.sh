#!/usr/bin/env bash
# Exercise cleanup without touching the caller's crontab or SSH connection.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/repo/cowrie" "$FIXTURE_DIR/bin"
cp "$REPO_ROOT/clean.sh" "$FIXTURE_DIR/repo/clean.sh"

cat > "$FIXTURE_DIR/repo/cowrie/persistent.sh" <<'PERSISTENT'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${1:-}" == --stop-keep-state && "$#" -eq 1 ]] || exit 99
printf 'persistent-stop\n' >> "$MOCK_EVENTS"
[[ "${MOCK_STOP_FAIL:-0}" == 0 ]]
PERSISTENT
chmod +x "$FIXTURE_DIR/repo/cowrie/persistent.sh"

cat > "$FIXTURE_DIR/bin/crontab" <<'CRONTAB'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == -l ]]; then
    if [[ "${MOCK_READ_FAIL:-0}" == 1 ]]; then
        echo 'simulated crontab read failure' >&2
        exit 70
    fi
    if [[ ! -e "$MOCK_CRONTAB" ]]; then
        echo 'no crontab for test-user' >&2
        exit 1
    fi
    cat "$MOCK_CRONTAB"
    if [[ "${MOCK_VERIFY_MISMATCH:-0}" == 1 && -e "$MOCK_INSTALLED_MARKER" ]]; then
        printf '# unexpected installed line\n'
    fi
elif [[ "$#" -eq 1 ]]; then
    if [[ "${MOCK_INSTALL_FAIL:-0}" == 1 ]]; then
        echo 'simulated crontab install failure' >&2
        exit 71
    fi
    cp "$1" "$MOCK_CRONTAB"
    touch "$MOCK_INSTALLED_MARKER"
    printf 'crontab-install\n' >> "$MOCK_EVENTS"
else
    echo 'unexpected crontab arguments' >&2
    exit 99
fi
CRONTAB
cat > "$FIXTURE_DIR/bin/flock" <<'FLOCK'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${1:-}" == -w && "${2:-}" == 70 && "${3:-}" == 8 ]]
[[ "${MOCK_FLOCK_FAIL:-0}" == 0 ]]
FLOCK
chmod +x "$FIXTURE_DIR/bin/"{crontab,flock}

export PATH="$FIXTURE_DIR/bin:$PATH"
export MOCK_CRONTAB="$FIXTURE_DIR/crontab"
export MOCK_EVENTS="$FIXTURE_DIR/events"
export MOCK_INSTALLED_MARKER="$FIXTURE_DIR/installed-marker"
export COWRIE_MONITOR_STATE_DIR="$FIXTURE_DIR/monitor-state"
clean="$FIXTURE_DIR/repo/clean.sh"
fixture_repo="$FIXTURE_DIR/repo"

write_initial_crontab() {
    rm -f -- "$MOCK_EVENTS" "$MOCK_INSTALLED_MARKER"
    cat > "$MOCK_CRONTAB" <<CRONTAB
# keep unrelated entries
0 * * * * /home/user/unrelated.sh
*/5 * * * * /bin/bash /opt/other/repo/redteam_scan_flag.sh --run-scheduled
15 3 * * * /bin/bash /opt/other/repo/cowrie/monitor.sh
# disabled example: /bin/bash "$fixture_repo/cowrie/monitor.sh"
* * * * * /bin/bash "$fixture_repo/cowrie/monitor.sh" --persistent # cowrie-persistent-monitor
* * * * * /bin/bash "$fixture_repo/cowrie/monitor.sh" # cowrie-monitor
*/15 * * * * /bin/bash "$fixture_repo/redteam_scan_flag.sh" --run-scheduled # redteam-scan-cron
* * * * * /bin/bash "$fixture_repo/cowrie/reconcile.sh" # cowrie-reconcile
* * * * * /bin/bash "$fixture_repo/cowrie/persistent.sh" --check
* * * * * /bin/bash "$fixture_repo/reconcile_cowrie.sh"
* * * * * /bin/bash "$fixture_repo/monitor_cowrie.sh"
* * * * * /bin/bash "$fixture_repo/ssh_key_access.sh"
* * * * * /bin/bash "$fixture_repo/scripts/ssh_key_access.sh"
0 * * * * /bin/bash "$fixture_repo/redteam_scan_flag.sh" --run-scheduled
CRONTAB
}

cat > "$FIXTURE_DIR/expected" <<CRONTAB
# keep unrelated entries
0 * * * * /home/user/unrelated.sh
*/5 * * * * /bin/bash /opt/other/repo/redteam_scan_flag.sh --run-scheduled
15 3 * * * /bin/bash /opt/other/repo/cowrie/monitor.sh
# disabled example: /bin/bash "$fixture_repo/cowrie/monitor.sh"
CRONTAB

write_initial_crontab
cp "$MOCK_CRONTAB" "$FIXTURE_DIR/original"

# Preview shows the resulting crontab but has no side effects.
bash "$clean" --dry-run > "$FIXTURE_DIR/preview"
cmp -s "$FIXTURE_DIR/preview" "$FIXTURE_DIR/expected"
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/original"
[[ ! -e "$MOCK_EVENTS" ]]

# Live cleanup removes all current and legacy jobs from this repo, then stops
# the SSH master. Running it again does not change the result.
bash "$clean" > "$FIXTURE_DIR/output"
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/expected"
[[ "$(cat "$MOCK_EVENTS")" == $'crontab-install\npersistent-stop' ]]
: > "$MOCK_EVENTS"
bash "$clean" > "$FIXTURE_DIR/output"
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/expected"

# A crontab read or install error must not stop the master while old cron
# entries could still restart it.
write_initial_crontab
if MOCK_READ_FAIL=1 bash "$clean" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Cleanup unexpectedly succeeded after a crontab read failure.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/original"
[[ ! -e "$MOCK_EVENTS" ]]

if MOCK_INSTALL_FAIL=1 bash "$clean" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Cleanup unexpectedly succeeded after a crontab install failure.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/original"
[[ ! -e "$MOCK_EVENTS" ]]

if MOCK_FLOCK_FAIL=1 bash "$clean" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Cleanup unexpectedly changed cron while a monitor lock was busy.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/original"
[[ ! -e "$MOCK_EVENTS" ]]

# A mismatched readback also prevents stopping the master.
if MOCK_VERIFY_MISMATCH=1 bash "$clean" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Cleanup unexpectedly accepted a mismatched installed crontab.' >&2
    exit 1
fi
[[ "$(cat "$MOCK_EVENTS")" == 'crontab-install' ]]

# Once cron is removed, a failed master stop is reported, leaving the cron
# cleanup in place so another cleanup attempt can finish the stop.
write_initial_crontab
if MOCK_STOP_FAIL=1 bash "$clean" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Cleanup unexpectedly succeeded after the SSH master failed to stop.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/expected"
[[ "$(cat "$MOCK_EVENTS")" == $'crontab-install\npersistent-stop' ]]

# No installed crontab is already a clean cron state; stop still runs.
rm -f -- "$MOCK_CRONTAB" "$MOCK_EVENTS" "$MOCK_INSTALLED_MARKER"
bash "$clean" > "$FIXTURE_DIR/output"
[[ "$(tail -n 1 "$MOCK_EVENTS")" == 'persistent-stop' ]]

echo 'Cleanup removes this repo cron jobs, preserves other jobs, and stops persistent SSH safely.'
