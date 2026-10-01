#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf -- "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/repo/cowrie" "$FIXTURE_DIR/bin"
cp "$REPO_ROOT/"{init_minute.sh,init_persistent.sh} "$FIXTURE_DIR/repo/"
cp "$REPO_ROOT/cowrie/"{init_common.sh,install_cron.sh} "$FIXTURE_DIR/repo/cowrie/"

cat > "$FIXTURE_DIR/repo/redteam_scan_flag.sh" <<'REDTEAM'
#!/usr/bin/env bash
echo 'Redteam scan must not run during initialization.' >&2
exit 99
REDTEAM
cat > "$FIXTURE_DIR/repo/cowrie/persistent.sh" <<'PERSISTENT'
#!/usr/bin/env bash
case "$*" in
    --initialize)
        printf 'persistent-initialize\n' >> "$MOCK_EVENTS"
        [[ "${MOCK_PERSISTENT_FAIL:-0}" == 0 ]]
        ;;
    --stop)
        printf 'persistent-stop\n' >> "$MOCK_EVENTS"
        [[ "${MOCK_STOP_FAIL:-0}" == 0 ]]
        ;;
    *) exit 99 ;;
esac
PERSISTENT

cat > "$FIXTURE_DIR/bin/crontab" <<'MOCK_CRONTAB'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == -l ]]; then
    cat "$MOCK_CRONTAB"
else
    if [[ "${MOCK_CRONTAB_INSTALL_FAIL:-0}" == 1 ]]; then
        echo 'simulated crontab install failure' >&2
        exit 70
    fi
    cp "$1" "$MOCK_CRONTAB"
    printf 'crontab-install\n' >> "$MOCK_EVENTS"
fi
MOCK_CRONTAB
cat > "$FIXTURE_DIR/bin/curl" <<'MOCK_CURL'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$*" != *'hc-ping.com'* && "$*" != *'discord.com/api/webhooks'* ]]
config="$(cat)"
case "$config" in
    *'12345678-1234-1234-1234-123456789abc'*) kind=cowrie-healthchecks; status=200 ;;
    *'87654321-4321-4321-4321-cba987654321'*) kind=redteam-healthchecks; status=200 ;;
    *'discord'*) echo 'unexpected Discord request' >&2; exit 99 ;;
    *) echo 'unexpected curl config' >&2; exit 99 ;;
esac
printf '%s\n' "$kind" >> "$MOCK_EVENTS"
if [[ "${MOCK_CURL_FAIL:-}" == "$kind" ]]; then status=503; fi
printf '%s' "$status"
MOCK_CURL
chmod +x "$FIXTURE_DIR/bin/"{crontab,curl}

cat > "$FIXTURE_DIR/crontab" <<'CRONTAB'
# unrelated job
0 * * * * /home/user/unrelated.sh
CRONTAB

export PATH="$FIXTURE_DIR/bin:$PATH"
export MOCK_EVENTS="$FIXTURE_DIR/events"
export MOCK_CRONTAB="$FIXTURE_DIR/crontab"
minute_init="$FIXTURE_DIR/repo/init_minute.sh"
persistent_init="$FIXTURE_DIR/repo/init_persistent.sh"

# Preview makes no network calls, needs no secrets, and leaves cron untouched.
bash "$minute_init" --dry-run > "$FIXTURE_DIR/preview"
grep -q '# redteam-scan-cron' "$FIXTURE_DIR/preview"
grep -q '# cowrie-monitor' "$FIXTURE_DIR/preview"
[[ ! -e "$MOCK_EVENTS" ]]

# Missing configuration fails before any cron or network mutation.
if bash "$minute_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Missing .env unexpectedly succeeded.' >&2
    exit 1
fi
grep -q 'must be a readable regular file' "$FIXTURE_DIR/output"
[[ ! -e "$MOCK_EVENTS" ]]

cat > "$FIXTURE_DIR/repo/.env" <<'ENV'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123/test-token
HEALTHCHECKS_PING_URL=https://hc-ping.com/12345678-1234-1234-1234-123456789abc
REDTEAM_HEALTHCHECKS_PING_URL=https://hc-ping.com/87654321-4321-4321-4321-cba987654321
ENV
chmod 600 "$FIXTURE_DIR/repo/.env"

bash "$minute_init" > "$FIXTURE_DIR/output"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks\npersistent-stop\ncrontab-install' ]]
[[ "$(grep -c '# cowrie-monitor' "$MOCK_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# redteam-scan-cron' "$MOCK_CRONTAB")" -eq 1 ]]
grep -q 'unrelated.sh' "$MOCK_CRONTAB"

: > "$MOCK_EVENTS"
sed '/DISCORD_WEBHOOK_URL=/d' "$FIXTURE_DIR/repo/.env" > "$FIXTURE_DIR/no-webhook"
mv "$FIXTURE_DIR/no-webhook" "$FIXTURE_DIR/repo/.env"
bash "$persistent_init" > "$FIXTURE_DIR/output"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks\npersistent-initialize\ncrontab-install' ]]
[[ "$(grep -c '# cowrie-persistent-monitor' "$MOCK_CRONTAB")" -eq 1 ]]
[[ "$(grep -c '# redteam-scan-cron' "$MOCK_CRONTAB")" -eq 1 ]]
! grep -q '# cowrie-monitor' "$MOCK_CRONTAB"
cp "$MOCK_CRONTAB" "$FIXTURE_DIR/before-failure"

: > "$MOCK_EVENTS"
if MOCK_CURL_FAIL=redteam-healthchecks bash "$minute_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed Healthchecks test unexpectedly installed cron.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/before-failure"
! grep -q 'crontab-install' "$MOCK_EVENTS"

: > "$MOCK_EVENTS"
if MOCK_CURL_FAIL=redteam-healthchecks bash "$persistent_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed Healthchecks test unexpectedly started persistent SSH.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/before-failure"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks' ]]

: > "$MOCK_EVENTS"
if MOCK_STOP_FAIL=1 bash "$minute_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed persistent stop unexpectedly installed minute cron.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/before-failure"
[[ "$(tail -n 1 "$MOCK_EVENTS")" == 'persistent-stop' ]]
! grep -q 'crontab-install' "$MOCK_EVENTS"

: > "$MOCK_EVENTS"
if MOCK_PERSISTENT_FAIL=1 bash "$persistent_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed persistent initialization unexpectedly installed cron.' >&2
    exit 1
fi
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/before-failure"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks\npersistent-initialize' ]]

# A failed switch from minute mode stops the newly started master and keeps
# the old minute cron, whether initialization or crontab installation failed.
: > "$MOCK_EVENTS"
bash "$minute_init" > "$FIXTURE_DIR/output"
cp "$MOCK_CRONTAB" "$FIXTURE_DIR/minute-before-failure"

: > "$MOCK_EVENTS"
if MOCK_PERSISTENT_FAIL=1 bash "$persistent_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed persistent initialization unexpectedly switched cron.' >&2
    exit 1
fi
grep -q 'persistent SSH initialization failed' "$FIXTURE_DIR/output"
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/minute-before-failure"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks\npersistent-initialize\npersistent-stop' ]]

: > "$MOCK_EVENTS"
if MOCK_CRONTAB_INSTALL_FAIL=1 bash "$persistent_init" > "$FIXTURE_DIR/output" 2>&1; then
    echo 'Failed crontab installation unexpectedly switched cron.' >&2
    exit 1
fi
grep -q 'persistent cron installation failed' "$FIXTURE_DIR/output"
cmp -s "$MOCK_CRONTAB" "$FIXTURE_DIR/minute-before-failure"
[[ "$(cat "$MOCK_EVENTS")" == $'cowrie-healthchecks\nredteam-healthchecks\npersistent-initialize\npersistent-stop' ]]

echo 'Cowrie init scripts: validate and test Healthchecks, start persistence first, and switch cron safely.'
