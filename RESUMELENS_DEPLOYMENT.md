# ResumeLens CPU deployment

The application lives in sibling `cs553-case-study-1` on `local-deploy-main`.
These scripts are maintained on this repository's `alexander-dev` branch.

## Scheduler setup and automatic recovery

Run `./init.sh` on **linux.wpi.edu**, where the scheduler crontab lives. It prompts for the Cowrie, redteam, and ResumeLens VM Healthchecks URLs; Discord webhook; Hugging Face token; Git checkout path; VM SSH user/port; group and bootstrap private-key paths; group public-key path; and optional jump host. Inputs for secrets and check URLs are hidden. Enter retains an existing value, and `-` clears the optional jump host.

The initializer saves private settings in the case study 2 root `.env` and `.hf.env`, tests the three checks, Discord delivery, and token, installs ordinary minute-mode Cowrie plus the existing redteam schedule and ResumeLens recovery cron, then starts initial deployment. Use `./init.sh --test` to prompt, save, and test without touching cron or the VM. Use `./init.sh --dry-run` to preview scheduler jobs without asking for credentials or accessing the network. It preserves unrelated cron entries.

The app source defaults to `~/cs553-case-study-1` on the scheduler. Setup clones [the app repository](https://github.com/alexander-krett/cs553-case-study-1) using the scheduler user's GitHub SSH access, on **local-deploy-main** only. Before a full redeploy, the recovery worker fetches and fast-forwards that branch. It refuses dirty or diverged checkouts. If GitHub is temporarily unavailable, it can redeploy from the clean, validated local checkout and logs that it may be behind.

A one-minute scheduler check connects through `ssh -J turing.wpi.edu` by default (a nonempty `SSH_JUMP` overrides the jump host) and verifies SSH access, required files, enabled and active services, UI/router HTTP health, fresh monitor status, and the VM heartbeat timer. If services alone stop, recovery restarts them and checks for up to two minutes. If installation files or units are missing, or a restart fails, it restores group-key access with the bootstrap key and redeploys. Recovery uses a lock shared with manual deploys; failed full deployments wait five minutes before retrying. The monitor returns promptly while installations run in a separate worker.

Honeypot recovery (minute reconciliation and persistent recovery) first runs ResumeLens recovery and confirms that the app is healthy. If app recovery fails or another app deployment is still running and the app remains unhealthy, Cowrie restoration is deferred until the next check. Honeypot health checks and scheduler heartbeats continue independently.

For this configured VM, deployment and recovery keep an isolated known-hosts file and automatically replace its entry when the VM's SSH host key changes. This is scoped to the target connection and does not alter global SSH settings. Keep GitHub SSH access and both SSH keys available on the scheduler for unattended rebuilds.

## Deploy

`init.sh` runs `scripts/deploy_resumelens.sh` automatically. It can also be invoked directly by the recovery worker. The script expects the app checkout on `local-deploy-main`, a token file, Discord webhook, and the separate VM Healthchecks URL.

Defaults:

- SSH: `student-admin@paffenroth-23.dyn.wpi.edu`, port `23001`, using direct SSH.
- Key: `~/.ssh/mlops/id_ed25519_group_key`.
- Jump host: none. Set `SSH_JUMP` to use a jump host.
- Source: scheduler-managed app checkout; remote app: `~/resumelens`.
- UI: `0.0.0.0:7860`; CPU router: `127.0.0.1:8080`.

uv 0.11.16 provisions Python 3.11 and installs exactly the committed lockfile
with `uv sync --locked --no-dev`. The CPU runtime and model revisions are fixed
in the provisioning scripts. Repeat deployments reuse the runtime, model
downloads, and uv cache. Cold deployment uses the checksum-verified official
Ubuntu x64 CPU binary (build b11324). If that binary fails its compatibility
check, the fallback build uses one compilation job with GPU backends disabled.

## Operation (on the VM)

```bash
sudo systemctl status resumelens-app resumelens-inference resumelens-monitor resumelens-vm-heartbeat.timer
sudo journalctl -u resumelens-app -u resumelens-inference -u resumelens-monitor -u resumelens-vm-heartbeat.service -n 100
sudo systemctl restart resumelens-inference resumelens-app resumelens-monitor
```

After publishing a change to `local-deploy-main`, run
`python3 scripts/resumelens_scheduler.py deploy` on linux.wpi.edu to fetch the
branch and force a locked deployment, or run `./init.sh` to rotate credentials
and retest integrations. Configure timeout and context in
`~/resumelens/.env`; context must stay between 512 and 4096 and the local
model IDs must match the generated runtime presets. Redeploy after context
changes. Default context is 4096, response budget 2048, two
threads, one loaded model, one request at a time, and eight queued requests.

To uninstall services (app files are kept):

```bash
sudo systemctl disable --now resumelens-vm-heartbeat.timer resumelens-vm-heartbeat.service resumelens-monitor resumelens-app resumelens-inference
sudo rm /etc/systemd/system/resumelens-app.service /etc/systemd/system/resumelens-inference.service /etc/systemd/system/resumelens-monitor.service /etc/systemd/system/resumelens-vm-heartbeat.service /etc/systemd/system/resumelens-vm-heartbeat.timer
sudo systemctl daemon-reload
```

The VM monitor measures CPU from `/proc/stat` aggregate counter deltas and RAM as `(MemTotal - MemAvailable) / MemTotal`, excluding swap. Either resource above 80% for more than five seconds triggers one amber Discord embed and a ResumeLens near-capacity warning; reviews remain available. The monitor clears the warning and sends one green recovery embed when both CPU and RAM remain below 70% for ten seconds. It publishes status at `/run/resumelens-monitor/status.json`; current alerts and retry state persist under `/var/lib/resumelens-monitor`.

The VM's own `resumelens-vm-heartbeat.timer` pings the dedicated Healthchecks check every 60 seconds, beginning 30 seconds after boot. Set that check's period to one minute and its grace to two minutes. The Cowrie and redteam check URLs remain separate.

## Verification

Run app tests in case study 1 with `uv run --locked python -m pytest -q`. Run
case study 2 tests from its repository root with
`uv run --project ../cs553-case-study-1 --locked python -m pytest -q tests`;
the Cowrie delay-proxy tests need permission to bind loopback TCP sockets.
Run the shell checks with `bash tests/test_cowrie_init.sh`,
`bash tests/test_cowrie_install_cron.sh`, and `bash tests/test_cleanup.sh`.

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
