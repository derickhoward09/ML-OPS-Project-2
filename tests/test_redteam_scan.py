"""Offline redteam logging and scheduler tests; all network calls are mocked."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("America/New_York")


class RedteamScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        shutil.copy2(ROOT / "redteam_scan_flag.sh", self.repo)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.keys = self.home / ".ssh" / "mlops"
        self.keys.mkdir(parents=True)
        for key in ("student-admin_key", "id_ed25519_group_key"):
            (self.keys / key).write_text("mock key\n")
        self.config = self.root / "config" / "redteam-scan"
        self.config.mkdir(parents=True)
        self.events = self.root / "events"
        self.env = os.environ.copy()
        self.env.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.root / "config"),
            PATH=str(self.bin) + os.pathsep + self.env["PATH"],
            MOCK_EVENTS=str(self.events),
            MOCK_NOW="20261001120000",
        )
        self.command("ssh", 'printf "ssh\\n" >> "$MOCK_EVENTS"\nexit "${MOCK_SSH_STATUS:-0}"\n')
        self.command("sleep", "exit 0\n")
        self.command("python3", 'printf "ntp\\n" >> "$MOCK_EVENTS"\necho "mock NTP unavailable" >&2\nexit 1\n')
        self.command("curl", '''config="$(cat)"
if [[ "$config $*" == *discord* || "$config" != *hc-ping.com* ]]; then
    printf 'unexpected-request\n' >> "$MOCK_EVENTS"
    exit 99
fi
case "$config" in
    *'/start"'*) kind=start ;;
    *'/fail"'*) kind=fail ;;
    *) kind=complete ;;
esac
printf 'healthchecks-%s\n' "$kind" >> "$MOCK_EVENTS"
printf '%s' "${MOCK_HEALTHCHECKS_STATUS:-200}"
''')
        date = self.bin / "date"
        date.write_text(f"#!{sys.executable}\n" + '''from datetime import datetime
import os
import sys
from zoneinfo import ZoneInfo
args = sys.argv[1:]
zone = ZoneInfo("America/New_York")
if "-j" in args:
    value = datetime.strptime(args[3], "%Y%m%d%H%M").replace(tzinfo=zone)
    print(int(value.timestamp()))
else:
    value = datetime.strptime(os.environ["MOCK_NOW"], "%Y%m%d%H%M%S").replace(tzinfo=zone)
    print(int(value.timestamp()) if args[-1] == "+%s" else value.strftime(args[-1][1:]))
''')
        date.chmod(0o755)

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
        path.chmod(0o755)

    def run_scan(self, *args, **overrides):
        env = self.env.copy()
        env.update(overrides)
        return subprocess.run(
            ["bash", str(self.repo / "redteam_scan_flag.sh"), *args],
            env=env, text=True, capture_output=True, timeout=15,
        )

    def log(self):
        path = self.repo / "redteam_scan.log"
        return path.read_text() if path.exists() else ""

    def events_seen(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def legacy_env(self):
        (self.repo / ".env").write_text(
            "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123/legacy\n"
            "REDTEAM_HEALTHCHECKS_PING_URL=https://hc-ping.com/12345678-1234-1234-1234-123456789abc\n"
        )

    def test_test_mode_logs_both_keys_and_ntp_without_env(self):
        result = self.run_scan("--test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events_seen(), ["ntp", "ssh", "ssh"])
        self.assertIn("NTP check warning", self.log())
        self.assertIn("student-admin key=succeeded group key=succeeded", self.log())
        self.assertIn("Test node 24 SSH login succeeded", self.log())

    def test_jump_variant_retains_jump_option_without_discord(self):
        jump = ROOT / "redteam_scan_flag_jump.sh"
        if not jump.exists():
            self.skipTest("Optional local jump scanner is absent")
        shutil.copy2(jump, self.repo / "redteam_scan_flag.sh")
        self.command("ssh", '[[ "$*" == *"-J akrett@turing.wpi.edu"* ]]\nprintf "ssh\\n" >> "$MOCK_EVENTS"\n')
        self.legacy_env()
        result = self.run_scan("--test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events_seen(), ["ntp", "ssh", "ssh"])
        self.assertIn("Test node 24 SSH login succeeded", self.log())

    def test_failed_test_keys_are_logged_with_legacy_webhook_present(self):
        self.legacy_env()
        result = self.run_scan("-t", MOCK_SSH_STATUS="255")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Test node 24 SSH login failed", self.log())
        self.assertEqual(self.events_seen(), ["ntp", "ssh", "ssh"])

    def test_missing_keys_fails_and_logs_without_delivery(self):
        for key in self.keys.iterdir():
            key.unlink()
        result = self.run_scan("--test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no configured test key is available", self.log())
        self.assertEqual(self.events_seen(), ["ntp"])

    def test_removed_init_argument_and_help(self):
        result = self.run_scan("--init-discord")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)
        help_result = self.run_scan("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn("Discord", help_result.stdout)
        self.assertNotIn("--init-discord", help_result.stdout)
        self.assertEqual(self.events_seen(), [])

    def test_each_milestone_logs_latest_scan_once(self):
        self.legacy_env()
        for stamp in ("202609291300", "202609291900", "202609301000", "202610011100", "202610011200"):
            with self.subTest(stamp=stamp):
                for _ in range(2):
                    result = self.run_scan(MOCK_NOW=stamp + "00")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log().count("Redteam timeline:"), 5)
        self.assertIn("24/25 groups authenticated", self.log())
        self.assertEqual(len((self.config / "logged_milestones").read_text().splitlines()), 5)
        self.assertEqual(set(self.events_seen()), {"ssh"})
        self.assertFalse((self.config / "milestones.lock").exists())

    def test_legacy_ids_imported_and_deadline_uses_persisted_scan(self):
        (self.config / "sent_notifications").write_text("202609291300\n202610011100\ninvalid\n")
        (self.config / "last_scan").write_text("earlier scan|3|2, 3, 24\n")
        self.assertEqual(self.run_scan().returncode, 0)
        self.assertEqual(self.run_scan().returncode, 0)
        self.assertEqual(self.log().count("Redteam timeline:"), 1)
        self.assertIn("earlier scan: 3/25 groups authenticated (nodes: 2, 3, 24)", self.log())
        self.assertEqual((self.config / "logged_milestones").read_text().splitlines(),
                         ["202609291300", "202610011100", "202610011200"])
        self.assertEqual(self.events_seen(), [])
        self.assertEqual((self.config / "logged_milestones").stat().st_mode & 0o777, 0o600)

    def test_milestone_eligibility_window(self):
        for now in ("20260929115900", "20261001121001"):
            result = self.run_scan(MOCK_NOW=now)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log(), "")
        self.assertEqual(self.events_seen(), [])
        result = self.run_scan(MOCK_NOW="20261001121000")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Latest full scan at unavailable", self.log())

    def test_scheduled_scan_keeps_heartbeats_and_scan_interval(self):
        self.legacy_env()
        result = self.run_scan("--run-scheduled", MOCK_NOW="20260930100000")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events_seen(), ["healthchecks-start"] + ["ssh"] * 24 + ["healthchecks-complete"])
        self.assertIn("Redteam timeline:", self.log())
        self.assertFalse((self.config / "schedule.lock").exists())
        result = self.run_scan("--run-scheduled", MOCK_NOW="20260930101500")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.events_seen()), 26)

    def test_scheduled_scan_preserves_check_and_heartbeat_failures(self):
        self.legacy_env()
        (self.keys / "student-admin_key").unlink()
        result = self.run_scan("--run-scheduled", MOCK_NOW="20260930100000")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.events_seen(), ["healthchecks-start", "healthchecks-fail"])
        self.assertIn("Redteam timeline:", self.log())
        result = self.run_scan("--run-scheduled", MOCK_HEALTHCHECKS_STATUS="503")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.events_seen()[-2:], ["healthchecks-start", "healthchecks-complete"])


if __name__ == "__main__":
    unittest.main()
