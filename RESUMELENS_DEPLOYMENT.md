# ResumeLens CPU deployment

The application lives in sibling `cs553-case-study-1` on `local-deploy-main`.
These scripts are maintained on this repository's `alexander-dev` branch.

## Deploy

Create `.hf.env` at this repository root, containing a bare Hugging Face token
or a single `HF_TOKEN=...` assignment. Keep it private (`chmod 600 .hf.env`).
Use a token with inference-provider access. It is ignored by Git and is never
printed. The remote app receives a private `.env`; existing settings survive
updates. No OAuth, public share link, or UI login is configured.

Run `./scripts/deploy_resumelens.sh`. Defaults:

- SSH: `student-admin@paffenroth-23.dyn.wpi.edu`, port `23001`.
- Key: `~/.ssh/mlops/id_ed25519_group_key`; known host verification is required.
- Jump host: `akrett@turing.wpi.edu`, authenticated using your existing SSH configuration.
- Source: sibling case study 1 checkout; remote app: `~/resumelens`.
- UI: `0.0.0.0:7860`; CPU router: `127.0.0.1:8080`.

Override `APP_SOURCE`, `TOKEN_FILE`, `SSH_HOST`, `SSH_PORT`, `SSH_JUMP`, and `SSH_KEY` as
needed. Set `SSH_JUMP=` to explicitly disable the jump host for another target. Set `TOKEN_FILE` to case study 1's root `hf-token` if preferred. Source
files are transferred directly, so a GitHub push is unnecessary. Existing app
files are overlaid; virtual environments, secrets, model caches, and unrelated
services are preserved. The script refuses occupied ports owned by other
processes. It does not change firewall, proxy, Cowrie, or SSH configuration.

uv 0.11.16 provisions Python 3.11 and installs exactly the committed lockfile
with `uv sync --locked --no-dev`. Restarts execute `.venv/bin/python` directly.
The CPU runtime and model revisions are fixed in the provisioning scripts;
repeat deployments reuse the runtime, model downloads, and uv cache. Cold deployment uses the checksum-verified official Ubuntu x64 CPU binary
(build b11324). If that binary fails its compatibility check, the fallback
build uses one compilation job with GPU backends disabled.

## Operation (on the VM)

```bash
sudo systemctl status resumelens-app resumelens-inference
sudo journalctl -u resumelens-app -u resumelens-inference -n 100
sudo systemctl restart resumelens-inference resumelens-app
```

Rerun deployment to update code or rotate the token. Configure timeout and
context in `~/resumelens/.env`; context must stay between 512 and 4096 and the
local model IDs must match the generated runtime presets. Rerun deployment
after context changes. Default context is 4096, response budget 2048, two
threads, one loaded model, one request at a time, and eight queued requests.

To uninstall services (app files are kept):

```bash
sudo systemctl disable --now resumelens-app resumelens-inference
sudo rm /etc/systemd/system/resumelens-app.service /etc/systemd/system/resumelens-inference.service
sudo systemctl daemon-reload
```

## Verification

Run app tests using `uv run --locked python -m pytest -q` in case study 1. Run deployment
unit tests using that environment's Python against
`tests/test_resumelens_deployment.py` (requires pytest and app dependencies).

Copy `scripts/verify_resumelens.py` to the VM and run it with
`~/resumelens/.venv/bin/python verify_resumelens.py ~/resumelens`. It exercises
all bundled local examples, each remote model, and records combined service
RSS, minimum available memory, swap counters, timings, and responses for manual
quality inspection. The report contains synthetic resumes' feedback, never
tokens. Acceptance requires no OOM or sustained swapping and 500 MiB OS
headroom. For live UI queue, model-unloading, remote-failover, and restart tests, copy
`scripts/verify_live_resumelens.py` to the VM and run it with the app environment
in the same way. This test briefly stops the CPU backend and restarts the app
services; run it when other users are not submitting reviews. Repeat deployment
should reuse cached dependencies, models, and the runtime.

## Observed model behavior

On this VM, Qwen3 0.6B Q8_0 completed all four synthetic review types in
8–19 seconds when run alone. Its feedback can misread supplied facts and
invent accomplishments despite the shared prompt; review suggestions before
using them. In one run it fabricated a 15% improvement, and another suggested
changing an assistant role to a leadership role without supporting evidence.

Ternary Bonsai's group-64 file loads and generates text on this x86 CPU, but
measured only about 3.4 prompt tokens/sec and 2.7 generated tokens/sec on a
short request. A full resume request exceeded the configured 300-second
read timeout. It is no longer part of the local preset. The current local-first
order is Qwen3 0.6B Q8_0, then LiquidAI LFM2.5 1.2B Instruct QAD Q4_0. Qwen
was faster end-to-end and used less memory in the collected runs; both models
still produced factual errors and need human review. The router keeps one model
resident at a time.

See `RESUMELENS_VERIFICATION.md` for deployment and acceptance measurements.

## Comparing alternative CPU models

Copy `scripts/compare_resumelens.py` to the VM and run it with the app virtual
environment Python. It compares pinned Qwen3 0.6B Q8_0, LiquidAI LFM2.5 1.2B
Instruct QAD-Q4_0, and SmolLM2 1.7B Instruct Q4_K_M on all four review types
with a 2,048-token output ceiling, context 4096, and two CPU threads. Models
may finish before the output ceiling. Downloads are cached. The script warms OS file caches with sequential reads
and records that time separately; model loading allows up to 15 minutes to
accommodate host storage pressure. Port 8081 must be
free; the existing inference service must be active. After downloads, the
script briefly stops that service, loads each candidate separately on
loopback port 8081, and restores the original service in a finally block.
Run when no one is submitting reviews. Results and synthetic feedback are
written to `~/resumelens/.deploy/comparison-2048.jsonl`. Inspect quality
manually; throughput and file size alone do not determine model selection.
