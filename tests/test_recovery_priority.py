"""Offline checks that app recovery gates honeypot restoration."""
import importlib.util
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("priority_scheduler", ROOT / "scripts/resumelens_scheduler.py")
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class RecoveryPriorityTests(unittest.TestCase):
    def test_app_recovery_finishes_before_health_confirmation(self):
        events = []
        with patch.object(scheduler, "recover", side_effect=lambda: events.append("recover") or 0), \
             patch.object(scheduler, "scheduler_values", return_value={}), \
             patch.object(scheduler, "inspect_vm", side_effect=lambda values: events.append("check") or ("healthy", "")):
            self.assertEqual(scheduler.prepare_honeypot(), 0)
        self.assertEqual(events, ["recover", "check"])

    def test_failed_app_recovery_defers_honeypot(self):
        with patch.object(scheduler, "recover", return_value=1), \
             patch.object(scheduler, "inspect_vm") as inspect:
            self.assertEqual(scheduler.prepare_honeypot(), 1)
            inspect.assert_not_called()

    def test_busy_app_worker_requires_healthy_app(self):
        for status in ("degraded", "unreachable", "missing"):
            with self.subTest(status=status), \
                 patch.object(scheduler, "recover", return_value=0), \
                 patch.object(scheduler, "scheduler_values", return_value={}), \
                 patch.object(scheduler, "inspect_vm", return_value=(status, "")):
                self.assertEqual(scheduler.prepare_honeypot(), 1)

    def test_reconcile_gates_every_repair_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cowrie = root / "cowrie"
            cowrie.mkdir()
            shutil.copy2(ROOT / "cowrie/reconcile.sh", cowrie / "reconcile.sh")
            commands = root / "bin"
            commands.mkdir()
            for path, body in (
                (commands / "python3", 'echo app >> "$EVENTS"; exit "$APP_STATUS"'),
                (cowrie / "retry_access.sh", 'echo access >> "$EVENTS"'),
                (cowrie / "deploy.sh", 'echo deploy >> "$EVENTS"'),
            ):
                path.write_text("#!/bin/bash\n" + body + "\n")
                path.chmod(0o755)
            events = root / "events"
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                       XDG_STATE_HOME=str(root / "state"), EVENTS=str(events))
            for mode in ("full", "--repair-access", "--repair-deploy"):
                for app_status in ("0", "1"):
                    with self.subTest(mode=mode, app_status=app_status):
                        events.write_text("")
                        result = subprocess.run(["bash", str(cowrie / "reconcile.sh"), mode],
                                                env=dict(env, APP_STATUS=app_status), capture_output=True, timeout=10)
                        seen = events.read_text().splitlines()
                        self.assertEqual(seen[0], "app")
                        if app_status == "1":
                            self.assertEqual(result.returncode, 1)
                            self.assertEqual(seen, ["app"])
                        else:
                            self.assertEqual(result.returncode, 0)
                            self.assertIn("deploy", seen)


if __name__ == "__main__":
    unittest.main()
