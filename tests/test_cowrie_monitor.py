"""Offline state-machine tests for the scheduler monitor; no network calls."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MONITOR = Path(__file__).resolve().parents[1] / "cowrie" / "monitor.sh"


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
        command("timeout", "shift 2\nexec \"$@\"\n")
        command(
            "ssh",
            'printf "%s\\n" "$*" > "$MOCK_SSH_ARGS"\n'
            'if [[ "${MOCK_SSH_FAIL:-0}" == 1 ]]; then exit 1; fi\n'
            'printf "%b" "${MOCK_SSH_OUTPUT:-active\\nactive\\n}"\n',
        )
        command(
            "python3",
            'if [[ "${1:-}" == - ]]; then\n'
            '    cat >/dev/null\n'
            '    printf "%s\\n" "${MOCK_ROUTE_RESULT:-ok}"\n'
            'else\n'
            '    exec "$REAL_PYTHON" "$@"\n'
            'fi\n',
        )
        command(
            "curl",
            'config="$(cat)"\n'
            'if [[ "$*" == *"discord.com/api/webhooks"* || "$*" == *"hc-ping.com"* ]]; then\n'
            '    echo "secret URL appeared in curl arguments" >&2; exit 98\n'
            'fi\n'
            'if [[ "$config" == *"discord.com"* ]]; then\n'
            '    if [[ "$*" == *"OUTAGE RECOVERED"* ]]; then kind=discord-delayed;\n'
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
        self.state_dir = root / "state"
        self.events = root / "events"
        self.ssh_args = root / "ssh_args"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(home),
                "PATH": str(mock_bin) + os.pathsep + self.env["PATH"],
                "REAL_PYTHON": sys.executable,
                "COWRIE_MONITOR_CONFIG": str(config),
                "COWRIE_MONITOR_STATE_DIR": str(self.state_dir),
                "MOCK_EVENTS": str(self.events),
                "MOCK_SSH_ARGS": str(self.ssh_args),
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

    def events_seen(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_two_failures_then_one_recovery_and_heartbeat_each_run(self):
        bad = {"MOCK_SSH_OUTPUT": "active\\ninactive\\n"}
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
        args = self.ssh_args.read_text()
        self.assertIn("id_ed25519_group_key", args)
        self.assertIn("-p 23001", args)
        self.assertIn("-F /dev/null", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("CertificateFile=none", args)
        self.assertIn("PreferredAuthentications=publickey", args)

    def test_webhook_failures_retry_and_single_bad_sample_does_not_recover(self):
        bad = {"MOCK_ROUTE_RESULT": "connect failed"}
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
        bad = {"MOCK_SSH_OUTPUT": "inactive\\nactive\\n"}
        self.assertEqual(self.run_monitor(**bad).returncode, 1)
        self.assertEqual(self.run_monitor(**bad, MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "2 healthy 1\n")

        self.assertEqual(self.run_monitor(MOCK_DISCORD_FAIL="1").returncode, 1)
        self.assertEqual((self.state_dir / "status").read_text(), "0 healthy 1\n")
        self.assertEqual(self.run_monitor().returncode, 0)
        self.assertEqual((self.state_dir / "status").read_text(), "0 healthy 0\n")
        self.assertEqual(self.run_monitor().returncode, 0)

        events = self.events_seen()
        self.assertEqual(events.count("discord-failure"), 1)
        self.assertEqual(events.count("discord-delayed"), 2)
        self.assertEqual(events.count("discord-recovery"), 0)
        self.assertEqual(events.count("heartbeat"), 5)


if __name__ == "__main__":
    unittest.main()
