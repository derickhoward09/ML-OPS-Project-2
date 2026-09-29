"""Offline state-machine tests for Cowrie monitoring; no network calls."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo


MONITOR = Path(__file__).resolve().parents[1] / "cowrie" / "monitor.sh"
TIMEZONE = ZoneInfo("America/New_York")


class CowrieMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        mock_bin = root / "bin"
        mock_bin.mkdir()
        home = root / "home"
        key_dir = home / ".ssh" / "mlops"
        key_dir.mkdir(parents=True)
        (key_dir / "id_ed25519_group_key").write_text("test-key\n")
        (key_dir / "id_ed25519_group_key.pub").write_text("ssh-ed25519 AAAATEST test\n")
        config = root / "config"
        config.write_text(
            "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123/test-token\n"
            "HEALTHCHECKS_PING_URL=https://hc-ping.com/12345678-1234-1234-1234-123456789abc\n"
        )
        config.chmod(0o600)

        def command(name, body):
            path = mock_bin / name
            path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
            path.chmod(0o755)

        command("flock", "exit 0\n")
        command("timeout", 'shift 2\nexec "$@"\n')
        command(
            "ssh",
            'printf "%s\\n" "$*" >> "$MOCK_SSH_ARGS"\n'
            'printf "ssh\\n" >> "$MOCK_SSH_CALLS"\n'
            'cat > "$MOCK_SSH_SCRIPT"\n'
            'case "${MOCK_SSH_STATUS:-0}" in\n'
            '  0) exit 0 ;;\n'
            '  1) echo "Cowrie unhealthy: cowrie.service is inactive" >&2; exit 1 ;;\n'
            '  20) echo "Cowrie unhealthy: authorized_keys differs" >&2; exit 20 ;;\n'
            '  *) exit 255 ;;\n'
            'esac\n',
        )
        command(
            "python3",
            'if [[ "${1:-}" == - ]]; then\n'
            '    cat >/dev/null\n'
            '    printf "probe\\n" >> "$MOCK_ROUTE_CALLS"\n'
            '    printf "%s\\n" "${MOCK_ROUTE_RESULT:-ok}"\n'
            'else\n'
            '    exec "$REAL_PYTHON" "$@"\n'
            'fi\n',
        )
        command(
            "date",
            'if [[ "$*" == "+%s" ]]; then printf "%s\\n" "${MOCK_NOW_EPOCH:-0}";\n'
            'elif [[ "$*" == "+%Y%m%d%H%M%S" ]]; then printf "%s\\n" "${MOCK_NOW_LOCAL:-20261002000000}";\n'
            'else exec /bin/date "$@"; fi\n',
        )
        command(
            "curl",
            'config="$(cat)"\n'
            'if [[ "$*" == *"discord.com/api/webhooks"* || "$*" == *"hc-ping.com"* ]]; then\n'
            '    echo "secret URL appeared in curl arguments" >&2; exit 98\n'
            'fi\n'
            'if [[ "$config" == *"discord.com"* ]]; then\n'
            '    if [[ "$*" == *"public port 22001 OUTAGE RECOVERED"* ]]; then kind=route-delayed;\n'
            '    elif [[ "$*" == *"public port 22001 RECOVERED"* ]]; then kind=route-recovery;\n'
            '    elif [[ "$*" == *"public port 22001 DOWN"* ]]; then kind=route-failure;\n'
            '    elif [[ "$*" == *"OUTAGE RECOVERED"* ]]; then kind=discord-delayed;\n'
            '    elif [[ "$*" == *"RECOVERED"* ]]; then kind=discord-recovery;\n'
            '    else kind=discord-failure; fi\n'
            '    printf "%s\\n" "$kind" >> "$MOCK_EVENTS"\n'
            '    if [[ "${MOCK_DISCORD_FAIL:-0}" == 1 ]]; then printf 503; exit 0; fi\n'
            '    printf 204\n'
            'else\n'
            '    printf "heartbeat\\n" >> "$MOCK_EVENTS"\n'
            '    printf 200\n'
            'fi\n',
        )
        command(
            "nohup",
            'printf "%s\\n" "$*" >> "$MOCK_REPAIRS"\n'
            'exit 0\n',
        )

        self.state_dir = root / "state"
        self.events = root / "events"
        self.ssh_args = root / "ssh_args"
        self.ssh_script = root / "ssh_script"
        self.ssh_calls = root / "ssh_calls"
        self.route_calls = root / "route_calls"
        self.repairs = root / "repairs"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(home),
                "PATH": str(mock_bin) + os.pathsep + self.env["PATH"],
                "REAL_PYTHON": sys.executable,
                "COWRIE_MONITOR_CONFIG": str(config),
                "COWRIE_MONITOR_STATE_DIR": str(self.state_dir),
                "COWRIE_MONITOR_RECONCILE_LOG": str(root / "logs" / "reconcile.log"),
                "COWRIE_MONITOR_JITTER_MAX_SECONDS": "0",
                "MOCK_EVENTS": str(self.events),
                "MOCK_SSH_ARGS": str(self.ssh_args),
                "MOCK_SSH_SCRIPT": str(self.ssh_script),
                "MOCK_SSH_CALLS": str(self.ssh_calls),
                "MOCK_ROUTE_CALLS": str(self.route_calls),
                "MOCK_REPAIRS": str(self.repairs),
            }
        )

    def run_monitor(self, **overrides):
        env = self.env.copy()
        env.update(overrides)
        return subprocess.run(
            ["bash", str(MONITOR)],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    @staticmethod
    def epoch(year, month, day, hour, minute=0):
        return int(datetime(year, month, day, hour, minute, tzinfo=TIMEZONE).timestamp())

    def events_seen(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def count_lines(self, path):
        return len(path.read_text().splitlines()) if path.exists() else 0

    def wait_for_lines(self, path, expected):
        for _ in range(100):
            if self.count_lines(path) >= expected:
                return
            time.sleep(0.01)
        self.fail(f"Timed out waiting for {expected} lines in {path}")

    def test_healthy_minute_uses_one_ssh_session_and_pings_heartbeat(self):
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.count_lines(self.ssh_calls), 1)
        args = self.ssh_args.read_text()
        self.assertIn("id_ed25519_group_key", args)
        self.assertIn("-p 23001", args)
        self.assertIn("-F /dev/null", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("CertificateFile=none", args)
        self.assertIn("authorized_keys", self.ssh_script.read_text())
        self.assertEqual(self.events_seen(), ["heartbeat"])

    def test_persistent_minute_uses_checker_and_starts_recovery_only_on_failure(self):
        fixture = self.state_dir.parent / "repo" / "cowrie"
        fixture.mkdir(parents=True)
        shutil.copy2(MONITOR, fixture / "monitor.sh")
        checker = fixture / "persistent.sh"
        checker.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$1" >> "$MOCK_PERSISTENT_CALLS"\n'
            'exit "${MOCK_PERSISTENT_STATUS:-0}"\n'
        )
        checker.chmod(0o755)
        persistent_calls = self.state_dir.parent / "persistent_calls"
        env = self.env.copy()
        env["MOCK_PERSISTENT_CALLS"] = str(persistent_calls)

        healthy = subprocess.run(
            ["bash", str(fixture / "monitor.sh"), "--persistent"],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertEqual(persistent_calls.read_text().splitlines(), ["--check"])
        self.assertEqual(self.count_lines(self.ssh_calls), 0)
        self.assertEqual(self.count_lines(self.repairs), 0)

        env["MOCK_PERSISTENT_STATUS"] = "2"
        lost = subprocess.run(
            ["bash", str(fixture / "monitor.sh"), "--persistent"],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(lost.returncode, 1, lost.stdout + lost.stderr)
        self.assertEqual(persistent_calls.read_text().splitlines(), ["--check", "--check"])
        self.assertEqual(self.count_lines(self.ssh_calls), 0)
        self.wait_for_lines(self.repairs, 1)
        self.assertIn("persistent.sh --recover", self.repairs.read_text())
        self.assertEqual(self.events_seen().count("heartbeat"), 2)

    def test_two_management_failures_then_one_recovery_and_heartbeat_each_run(self):
        bad = {"MOCK_SSH_STATUS": "1"}
        for _ in range(3):
            result = self.run_monitor(**bad)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for _ in range(2):
            result = self.run_monitor()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        events = self.events_seen()
        self.assertEqual(events.count("discord-failure"), 1)
        self.assertEqual(events.count("discord-recovery"), 1)
        self.assertEqual(events.count("heartbeat"), 5)
        self.assertEqual((self.state_dir / "status").read_text(), "0 healthy 0\n")
        self.assertEqual(self.count_lines(self.ssh_calls), 5)
        self.wait_for_lines(self.repairs, 3)
        self.assertEqual(self.count_lines(self.repairs), 3)
        self.assertTrue(all("--repair-deploy" in line for line in self.repairs.read_text().splitlines()))

    def test_management_transport_failure_defers_key_repair(self):
        result = self.run_monitor(MOCK_SSH_STATUS="255")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.count_lines(self.ssh_calls), 1)
        self.wait_for_lines(self.repairs, 1)
        self.assertIn("--repair-access", self.repairs.read_text())

    def test_authorized_keys_mismatch_selects_access_repair(self):
        result = self.run_monitor(MOCK_SSH_STATUS="20")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.count_lines(self.ssh_calls), 1)
        self.wait_for_lines(self.repairs, 1)
        self.assertIn("--repair-access", self.repairs.read_text())

    def test_webhook_failures_retry_and_single_bad_sample_does_not_recover(self):
        bad = {"MOCK_SSH_STATUS": "1"}
        self.assertEqual(self.run_monitor(**bad).returncode, 1)
        self.assertEqual(self.run_monitor(**bad, MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "2 healthy 1\n")
        self.assertEqual(self.run_monitor(**bad).returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "2 failing 0\n")

        self.assertEqual(self.run_monitor(MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "0 failing 0\n")
        self.assertEqual(self.run_monitor(**bad).returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "1 failing 0\n")
        self.assertEqual(self.run_monitor().returncode, 0)
        events = self.events_seen()
        self.assertEqual(events.count("discord-failure"), 2)
        self.assertEqual(events.count("discord-recovery"), 2)
        self.assertEqual(events.count("heartbeat"), 6)

    def test_failed_down_alert_is_delivered_as_one_delayed_notice_after_recovery(self):
        bad = {"MOCK_SSH_STATUS": "1"}
        self.assertEqual(self.run_monitor(**bad).returncode, 1)
        self.assertEqual(self.run_monitor(**bad, MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "2 healthy 1\n")

        self.assertEqual(self.run_monitor(MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "0 healthy 1\n")
        self.assertEqual(self.run_monitor().returncode, 0)
        self.assertEqual((self.state_dir / "status").read_text(), "0 healthy 0\n")

        events = self.events_seen()
        self.assertEqual(events.count("discord-failure"), 1)
        self.assertEqual(events.count("discord-delayed"), 2)
        self.assertEqual(events.count("discord-recovery"), 0)
        self.assertEqual(events.count("heartbeat"), 4)

    def test_route_probe_timer_persists_and_alerts_after_two_due_failures(self):
        start = self.epoch(2026, 9, 29, 12)
        start_local = "20260929120000"
        failed = {"MOCK_ROUTE_RESULT": "connect failed"}
        before_window = self.run_monitor(
            MOCK_NOW_EPOCH=str(start - 60),
            MOCK_NOW_LOCAL="20260929115900",
            **failed,
        )
        self.assertEqual(before_window.returncode, 0, before_window.stdout + before_window.stderr)
        self.assertEqual(self.count_lines(self.route_calls), 0)

        self.assertEqual(
            self.run_monitor(MOCK_NOW_EPOCH=str(start), MOCK_NOW_LOCAL=start_local, **failed).returncode,
            1,
        )
        self.assertEqual(self.count_lines(self.route_calls), 1)
        self.assertEqual((self.state_dir / "route-status").read_text(), f"{start} 1 healthy none\n")

        self.assertEqual(
            self.run_monitor(
                MOCK_NOW_EPOCH=str(start + 44 * 60),
                MOCK_NOW_LOCAL="20260929124400",
                **failed,
            ).returncode,
            0,
        )
        self.assertEqual(self.count_lines(self.route_calls), 1)

        self.assertEqual(
            self.run_monitor(
                MOCK_NOW_EPOCH=str(start + 45 * 60),
                MOCK_NOW_LOCAL="20260929124500",
                **failed,
            ).returncode,
            1,
        )
        self.assertEqual(self.count_lines(self.route_calls), 2)
        self.assertIn("route-failure", self.events_seen())
        self.assertEqual((self.state_dir / "route-status").read_text(), f"{start + 45 * 60} 2 failing none\n")

        recovered = self.run_monitor(
            MOCK_NOW_EPOCH=str(start + 90 * 60),
            MOCK_NOW_LOCAL="20260929133000",
            MOCK_ROUTE_RESULT="ok",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(self.count_lines(self.route_calls), 3)
        self.assertIn("route-recovery", self.events_seen())

        end = self.epoch(2026, 10, 1, 12)
        result = self.run_monitor(
            MOCK_NOW_EPOCH=str(end),
            MOCK_NOW_LOCAL="20261001120000",
            MOCK_ROUTE_RESULT="early banner",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.count_lines(self.route_calls), 3)

    def test_route_alert_delivery_retries_without_waiting_for_next_probe(self):
        start = self.epoch(2026, 9, 29, 12)
        failed = {"MOCK_ROUTE_RESULT": "early banner"}
        self.assertEqual(
            self.run_monitor(MOCK_NOW_EPOCH=str(start), MOCK_NOW_LOCAL="20260929120000", **failed).returncode,
            1,
        )
        second = self.run_monitor(
            MOCK_NOW_EPOCH=str(start + 45 * 60),
            MOCK_NOW_LOCAL="20260929124500",
            MOCK_DISCORD_FAIL="1",
            **failed,
        )
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertIn(" down\n", (self.state_dir / "route-status").read_text())

        retried = self.run_monitor(
            MOCK_NOW_EPOCH=str(start + 46 * 60),
            MOCK_NOW_LOCAL="20260929124600",
            **failed,
        )
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(self.count_lines(self.route_calls), 2)
        self.assertEqual(self.events_seen().count("route-failure"), 2)

    def test_route_probe_rejects_banner_close_and_connect_failure(self):
        start = self.epoch(2026, 9, 29, 12)
        for index, route_result in enumerate(("early banner", "early close", "connect failed")):
            (self.state_dir / "route-status").unlink(missing_ok=True)
            result = self.run_monitor(
                MOCK_NOW_EPOCH=str(start + index),
                MOCK_NOW_LOCAL="20260929120000",
                MOCK_ROUTE_RESULT=route_result,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual((self.state_dir / "route-status").read_text().split()[1], "1")

    def test_route_silent_delay_is_healthy(self):
        start = self.epoch(2026, 9, 29, 12)
        result = self.run_monitor(
            MOCK_NOW_EPOCH=str(start),
            MOCK_NOW_LOCAL="20260929120000",
            MOCK_ROUTE_RESULT="ok",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.count_lines(self.route_calls), 1)
        self.assertEqual((self.state_dir / "route-status").read_text(), f"{start} 0 healthy none\n")


if __name__ == "__main__":
    unittest.main()
