#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="$1"
cd "$APP_DIR"
trap 'rm -f "$APP_DIR/.deploy/token"' EXIT
UV_VERSION=0.11.16
LLAMA_REV=32dd62ee6dfa80ada846551fefec215cefc5ae1c
LLAMA_RELEASE=b11324
LLAMA_SHA256=124e8551bbbd9cead1173fa572f09ef67f9a1e4a2b208906eb25fcc60e038849
export PATH="$HOME/.local/bin:$PATH"
# Reject occupied ports unless owned by our existing units.
for entry in '7860 resumelens-app' '8080 resumelens-inference'; do
 read -r port unit <<< "$entry"
 if [[ -n "$(ss -ltnH "sport = :$port")" ]]; then
  service_pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null || true)"
  [[ "$service_pid" =~ ^[1-9][0-9]*$ ]] || { echo "Port $port is occupied by an unrelated process" >&2; exit 1; }
  sudo ss -ltnpH "sport = :$port" | grep -q "pid=$service_pid," || { echo "Port $port is not owned by $unit" >&2; exit 1; }
 fi
done
if ! command -v uv >/dev/null || [[ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]]; then
 curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
fi
uv python install 3.11
uv sync --locked --no-dev
mkdir -p .runtime models
if [[ ! -x .runtime/bin/llama-server ]] || [[ "$(cat .runtime/revision 2>/dev/null || true)" != "$LLAMA_REV" ]]; then
 mkdir -p .runtime/download
 curl -fL --silent --show-error "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_RELEASE/llama-$LLAMA_RELEASE-bin-ubuntu-x64.tar.gz" -o .runtime/download/runtime.tar.gz
 printf '%s  %s\n' "$LLAMA_SHA256" .runtime/download/runtime.tar.gz | sha256sum -c -
 tar -xzf .runtime/download/runtime.tar.gz -C .runtime/download
 candidate="$(find "$APP_DIR/.runtime/download" -type f -name llama-server -print -quit)"
 if [[ -n "$candidate" ]] && "$candidate" --version; then
  ln -sfn "$(dirname "$candidate")" .runtime/bin
 else
 sudo apt-get update -qq
 sudo apt-get install -y --no-install-recommends build-essential cmake git libssl-dev
 if [[ ! -d .runtime/llama/.git ]]; then
  git init .runtime/llama
  git -C .runtime/llama remote add origin https://github.com/ggml-org/llama.cpp.git
 fi
 git -C .runtime/llama fetch --depth 1 origin "$LLAMA_REV"
 git -C .runtime/llama checkout --detach "$LLAMA_REV"
 cmake -S .runtime/llama -B .runtime/llama/build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF -DGGML_METAL=OFF -DGGML_VULKAN=OFF -DGGML_NATIVE=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
 cmake --build .runtime/llama/build --target llama-server -j 1
 ln -sfn "$APP_DIR/.runtime/llama/build/bin" .runtime/bin
 fi
 printf '%s\n' "$LLAMA_REV" > .runtime/revision
fi
.runtime/bin/llama-server --version
.venv/bin/python .deploy/configure_resumelens.py "$APP_DIR"
sudo systemd-analyze verify .deploy/resumelens-app.service .deploy/resumelens-inference.service .deploy/resumelens-monitor.service .deploy/resumelens-vm-heartbeat.service .deploy/resumelens-vm-heartbeat.timer
sudo install -m 644 .deploy/resumelens-app.service .deploy/resumelens-inference.service .deploy/resumelens-monitor.service .deploy/resumelens-vm-heartbeat.service .deploy/resumelens-vm-heartbeat.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable resumelens-inference resumelens-app resumelens-monitor resumelens-vm-heartbeat.timer
sudo systemctl restart resumelens-inference resumelens-app resumelens-monitor
sudo systemctl restart resumelens-vm-heartbeat.timer
.venv/bin/python - <<'PY'
import json,subprocess,time,httpx
for _ in range(60):
 try:
  for url in ['http://127.0.0.1:8080/health','http://127.0.0.1:7860/']:
   httpx.get(url,timeout=2).raise_for_status()
  status=json.load(open('/run/resumelens-monitor/status.json'))
  if time.time()-float(status['timestamp']) > 5: raise ValueError('resource monitor status is stale')
  for unit in ['resumelens-app','resumelens-inference','resumelens-monitor','resumelens-vm-heartbeat.timer']:
   subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
  print('Deployment healthy: UI and CPU router respond; resource monitor and VM heartbeat timer are active')
  break
 except (httpx.HTTPError,OSError,ValueError,subprocess.CalledProcessError):time.sleep(1)
else:raise SystemExit('Health check failed; inspect journalctl -u resumelens-app -u resumelens-inference')
PY
