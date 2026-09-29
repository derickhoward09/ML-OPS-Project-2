# Node 24 Cowrie recovery

This setup runs a deny-all Cowrie decoy on node 24 and restores it after the target VM is rebuilt. The scheduler VM runs the jobs as its ordinary user; it needs no local sudo. The target's `student-admin` account must have passwordless `sudo -n` for installation.

## Routing and behavior

| Public port | Destination on node 24 | Purpose |
| --- | --- | --- |
| `22024` | real SSH server on port `22` | Management with `~/.ssh/mlops/id_ed25519_group_key` |
| `22001` | delay front end on port `22001` | Decoy, which later relays to Cowrie on `127.0.0.1:2222` |

The gateway's `22001` rule must actually forward to **node 24's port 22001**. Do not move the management SSH service to the decoy port. The front end accepts TCP, waits 14 minutes 45 seconds before sending an SSH banner, briefly relays to Cowrie, and closes the connection at 15 minutes. It limits concurrent clients and logs arrivals. Cowrie 3.0.15 runs as the dedicated `cowrie` user under systemd with public-key, password, no-auth, and keyboard-interactive logins denied; `hostname` must never succeed.

The legacy [`red_team_v1.sh`](rcpaffenroth/red_team_v1.sh) scans `22001` through `22021` **in order**. A held attempt on `22001` prevents that script from reaching later ports until it exits. The separate [`redteam_scan_flag.sh`](redteam_scan_flag.sh) has different behavior: it checks ports `22001` through `22025` in shuffled order.

The previous VM helper scripts remain under [`old_scripts/`](old_scripts/) and are not part of this Cowrie scheduler. The active scan's NTP helper is [`ntp_clock_check.py`](ntp_clock_check.py) beside `redteam_scan_flag.sh`.

## Scheduler configuration

1. Put the group private/public key at `~/.ssh/mlops/id_ed25519_group_key{,.pub}` on the scheduler. Keep `student-admin_key` there only for [`ssh_key_access.sh`](ssh_key_access.sh) to restore the authorized group public key after a rebuild. All deployment and monitoring SSH calls use the group private key on public `22024`.
2. Make sure the scheduler user has `bash`, OpenSSH, `curl`, Python 3, GNU `timeout`, and `flock`. Ubuntu 22.04 provides these through its usual packages; install missing packages as an administrator if needed. The target needs Python 3, `venv`, and passwordless `sudo -n` for `student-admin`.
3. Add these two lines to the repo-root `.env` (mode `600`). The existing red-team script can create the Discord line with `./redteam_scan_flag.sh --init-discord`. Create a separate Healthchecks.io check and copy its ping URL; the monitor reads the file as data and never executes it.

   ```dotenv
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_WITH_YOURS
   HEALTHCHECKS_PING_URL=https://hc-ping.com/REPLACE_WITH_CHECK_UUID
   ```

   Run `chmod 600 .env` after editing it. Keep the real URLs out of Git and command arguments.

4. In Healthchecks.io, set the check's period to **1 minute**, grace to **4 minutes**, and connect its Discord integration. This heartbeat reports whether the scheduler monitor ran, even when node 24 is down. A missed heartbeat covers a dead scheduler, disabled cron, or a monitor that cannot complete.
5. Review and install the two independent user-cron entries. The installer preserves unrelated crontab lines and removes prior direct `ssh_key_access.sh` jobs so key repair runs inside the locked reconciler.

   ```bash
   bash cowrie/install_cron.sh --dry-run
   bash cowrie/install_cron.sh
   crontab -l
   ```

`cowrie/reconcile.sh` runs every minute under a lock: restore the group key if needed, check deployment health, and deploy only when missing or unhealthy. `cowrie/monitor.sh` runs under its own lock with short timeouts: authenticate with the group key on `22024`, check `cowrie.service` and `cowrie-delay.service`, and connect to public `22001` to confirm no immediate SSH banner. After **two consecutive bad samples**, it sends one Discord failure alert. It sends one recovery alert when checks pass again. Failed Discord deliveries remain pending and are retried. If the target recovers before an undelivered failure alert can be sent, the monitor sends one delayed outage-recovered notice. Its state and log are under `${XDG_STATE_HOME:-$HOME/.local/state}/cowrie-monitor/`; the cron installer also writes `logs/cowrie_monitor.log` and `logs/cowrie_reconcile.log` in this repo.

The monitor pings the Healthchecks URL after **every completed run**, including runs that find node 24 down. Thus a target outage produces a Discord target alert while a missing heartbeat means the scheduler monitor stopped running. If the Healthchecks URL is missing or unreachable, the monitor logs the failure and exits nonzero; configure the check before relying on it.

## Verification

1. Run `bash cowrie/reconcile.sh` twice. The first run installs or repairs Cowrie; the second should pass its deployment check without reinstalling. Confirm group-key access on `22024` before and after, using `ssh -i ~/.ssh/mlops/id_ed25519_group_key -p 22024 -o IdentitiesOnly=yes -o BatchMode=yes student-admin@paffenroth-23.dyn.wpi.edu true`.
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

The 15-minute stall depends on the scanner and gateway keeping the TCP connection open that long. The outside-network SSH test is the acceptance check. This development workspace has not yet reached the gateway, so target installation and live-network behavior remain unverified here. Cowrie logs on the target can be lost when the VM is rebuilt.

## References

- [Cowrie recommended installation](https://docs.cowrie.org/en/stable/INSTALL.html)
- [Cowrie authentication checker](https://github.com/cowrie/cowrie/blob/v3.0.15/src/cowrie/core/checkers.py)
- [Healthchecks cron monitoring](https://healthchecks.io/docs/monitoring_cron_jobs/)
