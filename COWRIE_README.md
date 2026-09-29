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

## Scheduler configuration

1. Put the group private/public key at `~/.ssh/mlops/id_ed25519_group_key{,.pub}` on the scheduler. Keep `student-admin_key` there only for [`ssh_key_access.sh`](scripts/ssh_key_access.sh) to restore the authorized group public key after a rebuild. Healthy deployment and monitoring use the group private key on public `23001`; recovery can use the student key when group-key authentication is rejected.
2. Make sure the scheduler user has `bash`, OpenSSH, `curl`, Python 3, GNU `timeout`, and `flock`. Ubuntu 22.04 provides these through its usual packages; install missing packages as an administrator if needed. The target needs Python 3, `venv`, and passwordless `sudo -n` for `student-admin`.
3. Keep the repo-root `.env` private (mode `600`). The red-team script can create the Discord line with `./redteam_scan_flag.sh --init-discord`. Cowrie uses `HEALTHCHECKS_PING_URL`; redteam has a separate Healthchecks check and stores its URL as `REDTEAM_HEALTHCHECKS_PING_URL` when its cron is initialized.

   ```dotenv
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_WITH_YOURS
   HEALTHCHECKS_PING_URL=https://hc-ping.com/REPLACE_WITH_COWRIE_CHECK_UUID
   REDTEAM_HEALTHCHECKS_PING_URL=https://hc-ping.com/REPLACE_WITH_REDTEAM_CHECK_UUID
   ```

   Run `chmod 600 .env` after editing it. Keep the real URLs out of Git and command arguments. The monitor reads the file as data and never executes it.

4. In Healthchecks.io, set **Cowrie Deploy & SSH** to a **1-minute** period with a **5-minute** grace period, and connect its Discord integration. This heartbeat reports whether the scheduler monitor ran, even when node 24 is down. A missed heartbeat covers a dead scheduler, disabled cron, or a monitor that cannot complete.
5. Configure redteam's independent Healthchecks URL if `REDTEAM_HEALTHCHECKS_PING_URL` is missing. The command securely prompts for the URL and installs the existing redteam schedule. Set **RedTeam Cron** to a **45-minute** period with a **2-hour** grace period. The cron entry runs every 15 minutes, while the script gates full scans to at least 45 minutes apart and to the September 29–October 1 scan window.

   ```bash
   ./redteam_scan_flag.sh --init-cron
   crontab -l
   ```

6. Choose one Cowrie mode. Run its dry run to preview the resulting crontab, then run the init script. Each script validates the three `.env` notification URLs, sends test pings to both Healthchecks checks and one Discord test message, installs exactly one Cowrie cron entry, and keeps redteam's current 45-minute schedule. Neither init script starts a redteam scan. After the notification tests, the persistent init runs `cowrie/persistent.sh --initialize` to establish and test the SSH master before changing the crontab. If that step or cron installation fails while switching from minute mode, it closes the new master. The minute init closes an existing persistent master before switching cron. Failed preflight checks leave the existing crontab in place.

   ```bash
   bash init_minute.sh --dry-run
   bash init_minute.sh
   # Or select persistent mode instead:
   bash init_persistent.sh --dry-run
   bash init_persistent.sh
   crontab -l
   ```

   Switch modes by running the other init script. Both preserve unrelated cron entries and replace old Cowrie jobs. The persistent checker makes jittered reconnection attempts once a minute for 20 minutes after a lost connection, then observes a 30-minute cooldown before another retry window. Heartbeats and alerts continue during the cooldown. Redteam's full scan still makes 24 fresh SSH attempts, which can independently trigger the firewall.

`cowrie/monitor.sh` is the once-a-minute coordinator in regular mode; `cowrie/monitor.sh --persistent` selects the maintained SSH connection. Each run waits a random 0–20 seconds before checking, so successive runs are usually 40–80 seconds apart while remaining within the Healthchecks grace period. Monitor log entries use Boston time (`America/New_York`) in `YYYY-MM-DD HH:MM:SS EDT/EST` format. In regular mode, each healthy check opens one authenticated SSH session on `23001` to validate the group key in `authorized_keys`, the Cowrie deployment, both systemd services, and their listeners. It then updates the monitor state and pings Healthchecks. When access or deployment is unhealthy, it starts the corresponding locked repair in the background so a slow repair cannot hold up the next heartbeat. Access recovery makes one bounded TCP probe before trying the group and bootstrap keys; subsequent repair work opens additional SSH sessions only as needed.

The public `22001` delayed-banner probe has a separate persisted timer. It runs immediately on the first monitor minute inside the half-open window **September 29, 2026 noon to October 1, 2026 noon, America/New_York**, then when at least **45 minutes** have passed since its previous probe. It does not probe outside that window. Two consecutive failed route probes trigger one Discord alert; a passing probe sends recovery. Failed Discord deliveries remain pending and are retried. If the route recovers before an undelivered failure alert can be sent, the monitor sends one delayed outage-recovered notice. Route state, monitor state, and logs are under `${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-monitor/`; cron output is also written to `logs/cowrie_monitor.log` and repair output to `logs/cowrie_reconcile.log`.

The monitor pings the Healthchecks URL after **every completed minute run**, including runs that find node 24 down or start a repair. Thus a target outage produces a Discord target alert while a missing heartbeat means the scheduler monitor stopped running. If the Healthchecks URL is missing or unreachable, the monitor logs the failure and exits nonzero; configure the check before relying on it.

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
3. Stop either target service temporarily and run the monitor twice, one minute apart. Confirm one Discord failure alert, then restore/reconcile and confirm one recovery alert. Also simulate a failed Discord delivery and verify that the next monitor run retries it. Check that each completed monitor run still reaches Healthchecks while the target is down.
4. Suspend the scheduler monitor long enough to miss the four-minute grace and confirm Healthchecks sends its Discord alert, then restore the cron job.
5. If the Gradio application is expected to respond, confirm it is listening on node port `7860`, then request `http://paffenroth-23.dyn.wpi.edu:8001/` from outside node 24. The gateway rule alone does not start the application.

The 15-minute stall depends on the scanner and gateway keeping the TCP connection open that long. On September 29, 2026, live checks confirmed public `23001` returned the OpenSSH banner, old management port `22024` refused connections, and public `22001` reached the delay service without an immediate SSH banner. A full-duration connection stayed open for 900 seconds, returned the SSH banner at 885 seconds, then closed. Public `8001` reset because no application was listening on node port `7860`; ResumeLens is currently stopped. Cowrie logs on the target can be lost when the VM is rebuilt.

## References

- [Cowrie recommended installation](https://docs.cowrie.org/en/stable/INSTALL.html)
- [Cowrie authentication checker](https://github.com/cowrie/cowrie/blob/v3.0.15/src/cowrie/core/checkers.py)
- [Healthchecks cron monitoring](https://healthchecks.io/docs/monitoring_cron_jobs/)
