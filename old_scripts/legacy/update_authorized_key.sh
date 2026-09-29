#!/usr/bin/env bash
#
# Replaces the old rcpaffenroth SSH key in student-admin's ~/.ssh/authorized_keys
# on paffenroth-23 with the new key, after checking the old one is present.

set -euo pipefail

HOST="student-admin@paffenroth-23.dyn.wpi.edu"
PORT="22001"

OLD_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGO/pm63LYvO7aDxjzCQ3enaxK0gsaH/HTEY1YSotS8a rcpaffenroth@paffenroth-23"
NEW_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEh/lxIUb+20DUJx5/nQrCB9lAc/lHq3t8vZfA0GhwC4"

echo "Connecting to $HOST (port $PORT)..."

# Run the whole check-and-replace in one remote session. The keys are passed as
# quoted positional args to the remote bash, so their '/', '+', etc. are never
# interpreted as regex by sed/grep.
ssh -p "$PORT" "$HOST" "bash -s -- '$OLD_KEY' '$NEW_KEY'" <<'REMOTE'
set -euo pipefail

OLD_KEY="$1"
NEW_KEY="$2"
AUTH="$HOME/.ssh/authorized_keys"

# 1. Check that the old key exists before changing anything.
if ! grep -qxF -- "$OLD_KEY" "$AUTH"; then
    echo "ERROR: old key not found in $AUTH:" >&2
    echo "  $OLD_KEY" >&2
    exit 1
fi
echo "Old key found in $AUTH."

# 2. Back it up, then swap old key for new one.
cp "$AUTH" "$AUTH.bak"

# Keep every line except the old key (allow grep's "no lines left" exit of 1).
grep -vxF -- "$OLD_KEY" "$AUTH" > "$AUTH.tmp" || [ $? -eq 1 ]

if grep -qxF -- "$NEW_KEY" "$AUTH.tmp"; then
    echo "New key already present; not adding a duplicate."
else
    echo "$NEW_KEY" >> "$AUTH.tmp"
fi

mv "$AUTH.tmp" "$AUTH"
chmod 600 "$AUTH"

# 3. Verify: new key present, old key gone.
if grep -qxF -- "$NEW_KEY" "$AUTH" && ! grep -qxF -- "$OLD_KEY" "$AUTH"; then
    echo "Success. authorized_keys now contains the new key:"
    grep -F -- "$NEW_KEY" "$AUTH"
else
    echo "ERROR: verification failed; restoring backup." >&2
    cp "$AUTH.bak" "$AUTH"
    exit 1
fi
REMOTE

echo "Done."
