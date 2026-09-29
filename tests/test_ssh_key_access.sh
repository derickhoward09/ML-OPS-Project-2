#!/usr/bin/env bash
# Verify bootstrap-only recovery and the no-write healthy path without a VM.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/bin" "$FIXTURE_DIR/home/.ssh/mlops"

ssh-keygen -q -t ed25519 -N '' -f "$FIXTURE_DIR/home/.ssh/mlops/id_ed25519_group_key"
printf 'bootstrap-key\n' > "$FIXTURE_DIR/home/.ssh/mlops/student-admin_key"
printf 'previous-key\n' > "$FIXTURE_DIR/remote_authorized_keys"

cat > "$FIXTURE_DIR/bin/ssh" <<'MOCK_SSH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ " $* " == *" -F /dev/null "* ]]
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
    grep -Fqx -- "$(cat "$HOME/.ssh/mlops/id_ed25519_group_key.pub")" "$FAKE_REMOTE_AUTH" || exit 255
elif [[ "$key" != */student-admin_key ]]; then
    exit 255
fi

case "$command_arg" in
    true) ;;
    'cat "$HOME/.ssh/authorized_keys"') cat "$FAKE_REMOTE_AUTH" ;;
    *'mktemp'*'authorized_keys'*)
        cat > "$FAKE_REMOTE_AUTH"
        printf 'write\n' >> "$FAKE_WRITES"
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

"$REPO_ROOT/ssh_key_access.sh" >/dev/null
cmp -s "$FAKE_REMOTE_AUTH" "$HOME/.ssh/mlops/id_ed25519_group_key.pub"
[[ "$(wc -l < "$FAKE_WRITES")" -eq 1 ]]
bootstrap_calls_before="$(grep -c '/student-admin_key$' "$FAKE_CALLS")"

"$REPO_ROOT/ssh_key_access.sh" >/dev/null
[[ "$(wc -l < "$FAKE_WRITES")" -eq 1 ]]
bootstrap_calls_after="$(grep -c '/student-admin_key$' "$FAKE_CALLS")"
[[ "$bootstrap_calls_before" == "$bootstrap_calls_after" ]]

printf 'unexpected-extra-key\n' >> "$FAKE_REMOTE_AUTH"
"$REPO_ROOT/ssh_key_access.sh" >/dev/null
cmp -s "$FAKE_REMOTE_AUTH" "$HOME/.ssh/mlops/id_ed25519_group_key.pub"
[[ "$(wc -l < "$FAKE_WRITES")" -eq 2 ]]
[[ "$(grep -c '/student-admin_key$' "$FAKE_CALLS")" == "$bootstrap_calls_before" ]]

echo 'SSH key recovery: bootstrap used only when necessary; healthy state was unchanged; extra key was removed.'
