#!/usr/bin/env bash
set -Eeuo pipefail
SECONDS=0
export COPYFILE_DISABLE=1
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="${APP_SOURCE:-$SCRIPT_DIR/../../cs553-case-study-1}"
TOKEN_FILE="${TOKEN_FILE:-$SCRIPT_DIR/../.hf.env}"
SSH_HOST="${SSH_HOST:-student-admin@paffenroth-23.dyn.wpi.edu}"
SSH_PORT="${SSH_PORT:-23001}"
SSH_JUMP="${SSH_JUMP-akrett@turing.wpi.edu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/mlops/id_ed25519_group_key}"
[[ "$(git -C "$APP_SOURCE" branch --show-current)" == local-deploy-main ]] || { echo 'Source must be on local-deploy-main' >&2; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { echo 'Missing/empty credential file' >&2; exit 1; }
# Parse data, never source credential files. Accept a bare token or HF_TOKEN=... .
credential="$(mktemp)"
trap 'rm -f "$credential"' EXIT
python3 - "$TOKEN_FILE" "$credential" <<'PY'
import pathlib,sys,re
text=pathlib.Path(sys.argv[1]).read_text().strip()
if text.startswith('HF_TOKEN='):
 text=text.split('=',1)[1].strip().strip('\"\'')
if not re.fullmatch(r'hf_[A-Za-z0-9]+',text):
 raise SystemExit('Credential must be a bare HF token or a single HF_TOKEN assignment')
p=pathlib.Path(sys.argv[2]);p.write_text(text+'\n');p.chmod(0o600)
PY
ssh_args=(-i "$SSH_KEY" -p "$SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes)
[[ -z "$SSH_JUMP" ]] || ssh_args+=(-J "$SSH_JUMP")
remote_dir="$(ssh "${ssh_args[@]}" "$SSH_HOST" 'printf "%s/resumelens" "$HOME"')"
[[ "$remote_dir" =~ ^/[a-zA-Z0-9_/-]+$ ]] || { echo 'Unsupported remote home path' >&2; exit 1; }
ssh "${ssh_args[@]}" "$SSH_HOST" "mkdir -p '$remote_dir' '$remote_dir/.deploy'; chmod 700 '$remote_dir/.deploy'"
# Preserve environment/model caches on repeat deployment; no deletion of unrelated files.
tar --no-xattrs --no-acls -C "$APP_SOURCE" --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.cache --exclude=models --exclude=.runtime --exclude='.env*' --exclude=hf-token --exclude=.DS_Store -czf - app.py src pyproject.toml uv.lock .python-version README.md | ssh "${ssh_args[@]}" "$SSH_HOST" "tar -xzf - -C '$remote_dir'"
for file in provision_resumelens.sh configure_resumelens.py; do
 ssh "${ssh_args[@]}" "$SSH_HOST" "cat > '$remote_dir/.deploy/$file'" < "$SCRIPT_DIR/$file"
done
ssh "${ssh_args[@]}" "$SSH_HOST" "umask 077; cat > '$remote_dir/.deploy/token'" < "$credential"
ssh "${ssh_args[@]}" "$SSH_HOST" "bash '$remote_dir/.deploy/provision_resumelens.sh' '$remote_dir'"

printf "Deployment completed in %s seconds\n" "$SECONDS"
