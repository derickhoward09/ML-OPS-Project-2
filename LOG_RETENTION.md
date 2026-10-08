# Ubuntu VM log retention

Install on the development VM after cloning (requires Ubuntu with systemd,
logrotate, and rsyslog):

```sh
sudo bash scripts/install_log_rotation.sh "$PWD" "$USER"
```

The installer covers repo logs and default Cowrie state logs (10 MiB, seven
archives), Ops Agent logs (25 MiB, five archives), and Ubuntu rsyslog files
(25 MiB, seven archives). Logs rotate daily or when above the size threshold.
The system timer checks every 15 minutes, so these thresholds are not hard
caps. Compressed archives use less disk; one archive is left uncompressed.
The journal has a 200 MiB budget, seven-day retention, and reserves 1 GiB free.
Custom Cowrie state directories need an additional logrotate entry.

Shell jobs keep their open file descriptors through `copytruncate`. There is
a small copy/truncate race in which log lines can be lost. Rsyslog instead uses
Ubuntu's existing reopen hook. Existing configuration backups are saved under
`/var/backups/log-retention.*`. The installer validates the complete logrotate
configuration before activating timers and running normal rotation.

Verify with `systemctl status logrotate.timer`, `journalctl -u logrotate.service`,
and `sudo logrotate --debug /etc/logrotate.conf`.

Rotation controls local storage; it does not fix failed cloud uploads. The VM's
Ops Agent was reporting `logging.logEntries.create` permission denied. Correct
the attached service account's logging access separately, then confirm exports
succeed. Temporary Playwright environments and VS Code installations also use
substantial disk and are outside this retention policy.
