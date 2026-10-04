#!/usr/bin/env bash
# Verify bootstrap-only recovery and the no-write healthy path without a VM.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/bin" "$FIXTURE_DIR/home/.ssh/mlops"

ssh-keygen -q -t ed25519 -N '' -f "$FIXTURE_DIR/home/.ssh/mlops/id_ed25519_group_key"
printf 'bootstrap-key\n' > "$FIXTURE_DIR/home/.ssh/mlops/student-admin_key"
printf 'bootstrap-key\n' > "$FIXTURE_DIR/remote_authorized_keys"

cat > "$FIXTURE_DIR/bin/ssh" <<'MOCK_SSH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ " $* " == *" -F /dev/null "* ]]
[[ " $* " == *" -J ${FAKE_EXPECTED_JUMP:-turing.wpi.edu} "* ]]
[[ " $* " == *" -p 23001 "* ]]
[[ " $* " == *" -o CertificateFile=none "* ]]
[[ " $* " == *" -o PreferredAuthentications=publickey "* ]]
key=""
while (( $# )); do
    if [[ "$1" == -i ]]; then
        key="$2"
        shift 2
    else
        command_arg="$1"
        shift
    fi
done
printf '%s\n' "$key" >> "$FAKE_CALLS"

if [[ "$key" == */id_ed25519_group_key ]]; then
    [[ "${FAKE_REJECT_GROUP:-0}" != 1 ]] || exit 255
    grep -Fqx -- "$(cat "$HOME/.ssh/mlops/id_ed25519_group_key.pub")" "$FAKE_REMOTE_AUTH" || exit 255
elif [[ "$key" == */student-admin_key ]]; then
    grep -Fqx 'bootstrap-key' "$FAKE_REMOTE_AUTH" || exit 255
else
    exit 255
fi

case "$command_arg" in
    true) ;;
    'cat "$HOME/.ssh/authorized_keys"') cat "$FAKE_REMOTE_AUTH" ;;
    *'incoming=$(cat)'*)
        incoming="$(cat)"
        if ! grep -Fqx -- "$incoming" "$FAKE_REMOTE_AUTH"; then
            printf '\n%s\n' "$incoming" >> "$FAKE_REMOTE_AUTH"
            printf 'append\n' >> "$FAKE_WRITES"
        fi
        ;;
    *'mktemp'*'authorized_keys'*)
        cat > "$FAKE_REMOTE_AUTH"
        printf 'replace\n' >> "$FAKE_WRITES"
        ;;
    *) echo "Unexpected remote command: $command_arg" >&2; exit 2 ;;
esac
MOCK_SSH
chmod +x "$FIXTURE_DIR/bin/ssh"

export HOME="$FIXTURE_DIR/home"
export PATH="$FIXTURE_DIR/bin:$PATH"
export FAKE_REMOTE_AUTH="$FIXTURE_DIR/remote_authorized_keys"
export FAKE_CALLS="$FIXTURE_DIR/calls"
export FAKE_WRITES="$FIXTURE_DIR/writes"

"$REPO_ROOT/scripts/ssh_key_access.sh" >/dev/null
cmp -s "$FAKE_REMOTE_AUTH" "$HOME/.ssh/mlops/id_ed25519_group_key.pub"
[[ "$(wc -l < "$FAKE_WRITES")" -eq 2 ]]
[[ "$(head -n 1 "$FAKE_WRITES")" == append ]]
[[ "$(tail -n 1 "$FAKE_WRITES")" == replace ]]
bootstrap_calls_before="$(grep -c '/student-admin_key$' "$FAKE_CALLS")"

"$REPO_ROOT/scripts/ssh_key_access.sh" >/dev/null
[[ "$(wc -l < "$FAKE_WRITES")" -eq 2 ]]
bootstrap_calls_after="$(grep -c '/student-admin_key$' "$FAKE_CALLS")"
[[ "$bootstrap_calls_before" == "$bootstrap_calls_after" ]]

RESUMELENS_SSH_JUMP=custom.example.edu FAKE_EXPECTED_JUMP=custom.example.edu \
    "$REPO_ROOT/scripts/ssh_key_access.sh" >/dev/null

printf 'unexpected-extra-key\n' >> "$FAKE_REMOTE_AUTH"
"$REPO_ROOT/scripts/ssh_key_access.sh" >/dev/null
cmp -s "$FAKE_REMOTE_AUTH" "$HOME/.ssh/mlops/id_ed25519_group_key.pub"
[[ "$(wc -l < "$FAKE_WRITES")" -eq 3 ]]
[[ "$(grep -c '/student-admin_key$' "$FAKE_CALLS")" == "$bootstrap_calls_before" ]]

printf 'bootstrap-key\n' > "$FAKE_REMOTE_AUTH"
export FAKE_REJECT_GROUP=1
if "$REPO_ROOT/scripts/ssh_key_access.sh" >/dev/null; then
    echo 'Expected group-key verification to fail.' >&2
    exit 1
fi
grep -Fqx 'bootstrap-key' "$FAKE_REMOTE_AUTH"
[[ "$(wc -l < "$FAKE_WRITES")" -eq 4 ]]
[[ "$(tail -n 1 "$FAKE_WRITES")" == append ]]

echo 'SSH key recovery: bootstrap preserved until group access works; healthy state unchanged; extra key removed.'
