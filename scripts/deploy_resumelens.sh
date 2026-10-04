#!/usr/bin/env bash
set -Eeuo pipefail
SECONDS=0
export COPYFILE_DISABLE=1
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="${APP_SOURCE:-$SCRIPT_DIR/../../cs553-case-study-1}"
TOKEN_FILE="${TOKEN_FILE:-$SCRIPT_DIR/../.hf.env}"
SSH_HOST="${SSH_HOST:-student-admin@paffenroth-23.dyn.wpi.edu}"
SSH_PORT="${SSH_PORT:-23001}"
SSH_JUMP="${SSH_JUMP:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/mlops/id_ed25519_group_key}"
STATE_DIR="${RESUMELENS_STATE_DIR:-$HOME/.local/state/resumelens-recovery}"
[[ "$(git -C "$APP_SOURCE" branch --show-current)" == local-deploy-main ]] || { echo 'Source must be on local-deploy-main' >&2; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { echo 'Missing/empty credential file' >&2; exit 1; }
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
if [[ "${RESUMELENS_DEPLOY_LOCK_HELD:-0}" != 1 ]]; then
 exec 8>"$STATE_DIR/deploy.lock"
 flock -w 2700 8 || { echo 'Another ResumeLens deployment is still running' >&2; exit 1; }
fi
# Parse data, never source credential files. Accept a bare token or HF_TOKEN=... .
credential="$(mktemp)"
archive=""
monitor_config=""
trap 'rm -f "$credential" "${archive:-}" "${monitor_config:-}"' EXIT
python3 - "$TOKEN_FILE" "$credential" <<'PY'
import pathlib,sys,re
text=pathlib.Path(sys.argv[1]).read_text().strip()
if text.startswith('HF_TOKEN='):
 text=text.split('=',1)[1].strip().strip('\"\'')
if not re.fullmatch(r'hf_[A-Za-z0-9]+',text):
 raise SystemExit('Credential must be a bare HF token or a single HF_TOKEN assignment')
p=pathlib.Path(sys.argv[2]);p.write_text(text+'\n');p.chmod(0o600)
PY
known_hosts="$STATE_DIR/known_hosts"
touch "$known_hosts"
chmod 600 "$known_hosts"
ssh_args=(-F "$SCRIPT_DIR/ssh_config" -i "$SSH_KEY" -p "$SSH_PORT" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$known_hosts" -o GlobalKnownHostsFile=/dev/null)
[[ -z "$SSH_JUMP" ]] || ssh_args+=(-J "$SSH_JUMP")
ssh_checked() {
 local error_file code host
 error_file="$(mktemp "$STATE_DIR/ssh-error.XXXXXX")"
 if ssh "${ssh_args[@]}" "$SSH_HOST" "$@" 2>"$error_file"; then
  cat "$error_file" >&2
  rm -f "$error_file"
  return 0
 else
  code=$?
 fi
 if grep -q 'REMOTE HOST IDENTIFICATION HAS CHANGED' "$error_file"; then
  host="${SSH_HOST##*@}"
  echo "Configured VM host key changed; refreshing its isolated host-key record." >&2
  ssh-keygen -R "[$host]:$SSH_PORT" -f "$known_hosts" >/dev/null 2>&1 || true
  rm -f "$error_file"
  ssh "${ssh_args[@]}" "$SSH_HOST" "$@"
  return $?
 fi
 cat "$error_file" >&2
 rm -f "$error_file"
 return "$code"
}
remote_dir="$(ssh_checked 'printf "%s/resumelens" "$HOME"')"
[[ "$remote_dir" =~ ^/[a-zA-Z0-9_/-]+$ ]] || { echo 'Unsupported remote home path' >&2; exit 1; }
ssh_checked "mkdir -p '$remote_dir' '$remote_dir/.deploy'; chmod 700 '$remote_dir/.deploy'"
# Preserve environment/model caches on repeat deployment; no deletion of unrelated files.
archive="$(mktemp "$STATE_DIR/app-source.XXXXXX.tar.gz")"
tar --no-xattrs --no-acls -C "$APP_SOURCE" --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.cache --exclude=models --exclude=.runtime --exclude='.env*' --exclude=hf-token --exclude=.DS_Store -czf "$archive" app.py src pyproject.toml uv.lock .python-version README.md
ssh_checked "tar -xzf - -C '$remote_dir'" < "$archive"
for file in provision_resumelens.sh configure_resumelens.py resumelens_vm_monitor.py resumelens_vm_heartbeat.py; do
 ssh_checked "cat > '$remote_dir/.deploy/$file'" < "$SCRIPT_DIR/$file"
done
ssh_checked "umask 077; cat > '$remote_dir/.deploy/token'" < "$credential"
monitor_config="$(mktemp "$STATE_DIR/monitor-config.XXXXXX")"
python3 - "$monitor_config" <<'PY'
import os,sys
with open(sys.argv[1],'w') as stream:
 for key in ('DISCORD_WEBHOOK_URL','VM_HEALTHCHECKS_PING_URL'):
  value=os.environ.get(key,'')
  if not value or any(char in value for char in '\r\n"'):
   raise SystemExit(f'Missing or invalid {key}; run init.sh first')
  stream.write(f'{key}={value}\n')
PY
chmod 600 "$monitor_config"
ssh_checked "umask 077; cat > '$remote_dir/.deploy/monitor.env'" < "$monitor_config"
ssh_checked "bash '$remote_dir/.deploy/provision_resumelens.sh' '$remote_dir'"

printf "Deployment completed in %s seconds\n" "$SECONDS"
