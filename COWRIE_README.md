# Node 24 Cowrie recovery

This setup runs a deny-all Cowrie decoy on node 24 and restores it after the target VM is rebuilt. The scheduler VM runs the jobs as its ordinary user; it needs no local sudo. The target's `student-admin` account must have passwordless `sudo -n` for installation.

## Routing and behavior

| Public port | Destination on node 24 | Purpose |
| --- | --- | --- |
| `23001` | real SSH server on port `22` | Management with `~/.ssh/mlops/id_ed25519_group_key` |
| `22001` | delay front end on port `22001` | Decoy, which later relays to Cowrie on `127.0.0.1:2222` |
| `8001` | Gradio application on port `7860` | ResumeLens web application, when running |

The gateway mappings are node port `22` to public `23001`, node port `22001` to public `22001`, and node port `7860` to public `8001`. The `22001` rule must actually forward to **node 24's port 22001**. Do not move the management SSH service to the decoy port. The front end accepts TCP, waits 14 minutes 45 seconds before sending an SSH banner, briefly relays to Cowrie, and closes the connection at 15 minutes. It limits concurrent clients and logs arrivals. Cowrie 3.0.15 runs as the dedicated `cowrie` user under systemd with public-key, password, no-auth, and keyboard-interactive logins denied; `hostname` must never succeed.

The legacy [`red_team_v1.sh`](rcpaffenroth/red_team_v1.sh) scans `22001` through `22025` **in order**. A held attempt on `22001` prevents that script from reaching later ports until it exits. The separate [`redteam_scan_flag.sh`](redteam_scan_flag.sh) checks ports `22002` through `22025` in shuffled order and can be installed to run every 45 minutes during its permitted scan window.

The previous VM helper scripts remain under [`old_scripts/`](old_scripts/) and are not part of this Cowrie scheduler. Several still use port `22001` for SSH and would now connect to the decoy, so do not use them for VM access. The active scan's NTP helper is [`ntp_clock_check.py`](ntp_clock_check.py) beside `redteam_scan_flag.sh`.

## Unified scheduler and ResumeLens setup

Run `./init.sh` on **linux.wpi.edu**, using the account that owns the scheduler crontab. It securely prompts for the Cowrie, redteam, and ResumeLens VM Healthchecks URLs; the Discord webhook; the Hugging Face token; the ResumeLens Git checkout; and the VM SSH user, port, private keys, and optional jump host. Secret and URL inputs are hidden. Press Enter to keep saved values; type `-` for the optional jump host to select direct SSH.

Configuration is saved atomically with mode 0600 in the ignored root `.env` and `.hf.env`. Values are parsed as data, never sourced by shell. Do not commit either file. The managed application checkout defaults to `~/cs553-case-study-1`; it is cloned through the scheduler's GitHub SSH access on **local-deploy-main**. Initial setup checks integrations, installs the cron jobs, and starts deployment. The deployment worker fetches and fast-forwards that branch before a full redeploy. Dirty or diverged checkouts are left alone and reported.

```bash
./init.sh --dry-run # Preview cron jobs; no prompts, network calls, or writes
./init.sh --test    # Prompt/save settings and test integrations; no cron or deployment
./init.sh           # Configure, test, install cron, and start deployment
crontab -l
```

The initializer tests all three Healthchecks ping URLs, posts a labeled test embed to Discord, and validates the Hugging Face token. A successful setup sends the test embed to the configured team channel.

The crontab keeps Cowrie's ordinary minute monitor and the existing redteam schedule, and adds a one-minute ResumeLens recovery check. Persistent Cowrie monitoring can still be managed with its existing lower-level tools, but the unified initializer selects ordinary minute mode and stops an old persistent SSH master. Unrelated cron jobs are preserved. `./clean.sh --dry-run` previews removal of jobs owned by this checkout; `./clean.sh` removes them and stops Cowrie's persistent SSH master.

### VM recovery and resource alerts

Cowrie and the delay proxy get 180 seconds to finish starting before recovery
intervenes. Checks use systemd monotonic start times and continue scheduler
heartbeats while reporting `STARTING`, without increasing failure counters.
With correct managed files, recovery first restarts the failed service: a
Cowrie restart includes its dependent proxy, while a proxy-only restart leaves
Cowrie running. If readiness still fails after another 180 seconds, recovery
reconciles the deployment. Missing or changed managed files and unexpected
Cowrie bindings bypass startup grace. Target-side mutations share a root-owned
lock and timestamps under `/run/cowrie-recovery`; each targeted restart and full
reconciliation is limited to once per 300 seconds. This state clears on reboot.
SSH retries and monitoring continue every minute throughout these intervals.

`cowrie/deploy.sh --check` is read-only. Its results are healthy (0), deployment
drift or unexpected binding (1), unavailable management SSH (2), mismatched
authorized keys (3), invalid local key material (4), starting (5), runtime failure
with correct configuration (6), or inconclusive (7). An inconclusive or timed-out
check cannot trigger deployment repair. A missing port 2222 listener is reported
separately from an unexpected binding. The proxy's delayed-banner behavior is
unchanged.

The scheduler checks the configured application VM every minute. When the VM cannot be reached, the next check retries. When files or units are missing, recovery first restores group-key SSH access with the configured bootstrap key, refreshes the app branch if GitHub is reachable, then runs `scripts/deploy_resumelens.sh`. If services alone are stopped, it restarts them and allows two minutes for health checks before a full redeploy. A failed full deploy enters a five-minute retry cooldown; the monitor and recovery worker run separately so deployment cannot delay Cowrie heartbeats.

The VM's isolated SSH known-hosts file is under `~/.local/state/resumelens-recovery/known_hosts`. If the configured VM's host key changes after a rebuild, the recovery tools replace that VM's isolated pin and reconnect. They do not change global SSH settings. Keep the scheduler's group and bootstrap keys private. The scheduler must retain GitHub SSH access to pull the app repository.

The deployment enables these VM services:

- `resumelens-app` and `resumelens-inference` for the UI and CPU inference.
- `resumelens-monitor` for CPU and RAM sampling and Discord notifications.
- `resumelens-vm-heartbeat.timer` for a separate Healthchecks ping every 60 seconds, starting 30 seconds after boot.

The VM monitor reads aggregate CPU counters from `/proc/stat` and calculates CPU use over one-second counter differences across the VM's allocated CPUs. It calculates RAM use as `(MemTotal - MemAvailable) / MemTotal`; swap is excluded. **Either CPU or RAM above 80% for more than five seconds** triggers one amber Discord embed and a near-capacity banner in ResumeLens. The app continues accepting reviews. The banner refreshes every two seconds and reports current CPU and RAM.

The monitor clears the banner and sends one green recovery embed after **both CPU and RAM remain below 70% for ten seconds**. It records each alert episode and pending delivery so monitor restarts do not silently drop an alert or repeat an already delivered alert. Network retries and rate limits do not stop resource sampling.

Discord alerts include hostname, CPU and RAM percentages, triggering resource, threshold and duration, UTC timestamp, and recovery duration. They disable mentions and do not include webhook credentials or resume content. The VM Healthchecks URL is separate from the Cowrie and redteam checks; set its period to one minute with a two-minute grace period. Those pings originate on the application VM, so a dead VM stops heartbeats even while the scheduler is up.

Useful VM commands:

```bash
sudo systemctl status resumelens-app resumelens-inference resumelens-monitor resumelens-vm-heartbeat.timer
sudo journalctl -u resumelens-monitor -u resumelens-vm-heartbeat.service -n 100
cat /run/resumelens-monitor/status.json
```

To demonstrate the alert path, create a temporary systemd override with `sudo systemctl edit resumelens-monitor`:

```ini
[Service]
Environment=RESUMELENS_HIGH_PERCENT=1
Environment=RESUMELENS_HIGH_SECONDS=5
```

Reload and restart the monitor, then verify the amber alert and banner. Remove only this temporary override with `sudo rm /etc/systemd/system/resumelens-monitor.service.d/override.conf`, reload systemd, restart the monitor, and let CPU and RAM remain below 70% for ten seconds to verify the green recovery alert and cleared banner. Restore production thresholds (**80% / more than five seconds**, recovery **below 70% for ten seconds**) after the demonstration.

## Cleanup

On the scheduler VM, run these commands from the repository root as the user who installed the cron jobs:

```bash
bash clean.sh --dry-run
bash clean.sh
```

The dry run previews the updated crontab without changing cron or the SSH connection. Cleanup removes active cron entries referencing this checkout, including Cowrie and redteam jobs, and stops its persistent SSH master. It preserves unrelated cron jobs, Healthchecks configuration, logs, and state files. Cowrie and redteam scheduler pings stop after cleanup. The ResumeLens VM heartbeat continues until its VM systemd timer is disabled.

## Verification

1. Run `bash cowrie/reconcile.sh` twice. The first run installs or repairs Cowrie; the second should pass its deployment check without reinstalling. Confirm group-key access on `23001` before and after, using `ssh -i ~/.ssh/mlops/id_ed25519_group_key -p 23001 -o IdentitiesOnly=yes -o BatchMode=yes student-admin@paffenroth-23.dyn.wpi.edu true`.
2. From **outside node 24**, connect to public `22001`; confirm the front end logs the arrival with `journalctl -u cowrie-delay.service` on the target. An immediate SSH banner is a failure. On the machine with the legacy scanner's key path, run its port-`22001` command with only a test-only outer timeout added:

   ```bash
   PORT=22000
   MACHINE=paffenroth-23.dyn.wpi.edu
   KEY=$HOME/projects/1_classes/DS553_private/scripts/CS2/student-admin_key
   i=1
   timeout 16m ssh -i $KEY -p $((${i} + ${PORT})) -o StrictHostKeyChecking=no student-admin@${MACHINE} hostname
   ```

   The command should wait and then exit unsuccessfully; `hostname` must never run. Check every Cowrie authentication method, including public key, password, no-auth, and keyboard interactive, for rejection.
3. Stop either target service temporarily and run the monitor twice, one minute apart. Confirm a local DOWN transition, then restore/reconcile and confirm a RECOVERED transition. Check that each completed monitor run still reaches Healthchecks while the target is down.
4. Suspend the scheduler monitor long enough to miss the five-minute grace and confirm Healthchecks sends its Discord alert, then restore the cron job.
5. If the Gradio application is expected to respond, confirm it is listening on node port `7860`, then request `http://paffenroth-23.dyn.wpi.edu:8001/` from outside node 24. The gateway rule alone does not start the application.

The 15-minute stall depends on the scanner and gateway keeping the TCP connection open that long. On September 29, 2026, live checks confirmed public `23001` returned the OpenSSH banner, old management port `22024` refused connections, and public `22001` reached the delay service without an immediate SSH banner. A full-duration connection stayed open for 900 seconds, returned the SSH banner at 885 seconds, then closed. Public `8001` reset because no application was listening on node port `7860`; ResumeLens is currently stopped. Cowrie logs on the target can be lost when the VM is rebuilt.

## References

- [Cowrie recommended installation](https://docs.cowrie.org/en/stable/INSTALL.html)
- [Cowrie authentication checker](https://github.com/cowrie/cowrie/blob/v3.0.15/src/cowrie/core/checkers.py)
- [Healthchecks cron monitoring](https://healthchecks.io/docs/monitoring_cron_jobs/)

## Development VM log retention

On the Ubuntu development VM, install the size-based retention policy with
`sudo bash scripts/install_log_rotation.sh "$PWD" "$USER"`. See
[LOG_RETENTION.md](LOG_RETENTION.md) for limits, verification, and backup paths.
