"""Mac rollout orchestration tests; no SSH connection is made."""

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_sharded_macos.py"
SPEC = importlib.util.spec_from_file_location("deploy_sharded_macos", SCRIPT)
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)


class RolloutTests(unittest.TestCase):
    def test_parallel_preflight_reports_each_failed_node(self):
        def run(_ssh, node, _action, *_args, **_kwargs):
            if node in ("01", "03"):
                raise RuntimeError(f"failure on {node}")
            return "passed"

        with mock.patch.object(rollout, "controller", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "01: failure on 01; 03: failure on 03"):
                rollout.parallel_controller(object(), "preflight", rollout.REMOTE_DEFAULT,
                                            "test1")

    def test_ssh_uses_explicit_alias_and_batch_mode(self):
        runner = rollout.SSH({"01": "u60-private-alias"})
        completed = subprocess.CompletedProcess([], 0, b"ready\n", b"")
        with mock.patch.object(rollout.subprocess, "run", return_value=completed) as run:
            self.assertEqual(runner.run("01", ["hostname", "-s"]), "ready\n")
        argv = run.call_args.args[0]
        self.assertIn("u60-private-alias", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-1], "hostname -s")

    def test_stage_uses_one_upload_then_three_concurrent_preflights(self):
        events = []
        rows = {node: {"host": host, "files": {}, "cron": "", "mount": "shared"}
                for node, host in rollout.HOSTS.items()}
        with (mock.patch.object(rollout, "inspect_all", side_effect=lambda *a, **k: events.append("inspect") or rows),
              mock.patch.object(rollout, "upload", side_effect=lambda *a: events.append("upload")),
              mock.patch.object(rollout, "controller", side_effect=lambda *a, **k: events.append(a[2])),
              mock.patch.object(rollout, "parallel_controller", side_effect=lambda *a, **k: events.append(a[1]))):
            rollout.stage(object(), rollout.REMOTE_DEFAULT, "test1", rollout.HOSTS, {})
        self.assertEqual(events, ["inspect", "upload", "inspect", "roster", "preflight", "prepare"])

    def test_stage_refuses_to_replace_an_active_generation(self):
        rows = {node: {"host": host, "files": {}, "cron": "", "mount": "shared",
                       "active": {"active": True, "generation": "old"}}
                for node, host in rollout.HOSTS.items()}
        with (mock.patch.object(rollout, "inspect_all", return_value=rows),
              mock.patch.object(rollout, "upload") as upload):
            with self.assertRaisesRegex(RuntimeError, "deactivate"):
                rollout.stage(object(), rollout.REMOTE_DEFAULT, "new", rollout.HOSTS, {})
        upload.assert_not_called()

    def test_cutover_failure_rolls_back_legacy_schedule(self):
        legacy = f"* * * * * {rollout.REMOTE_DEFAULT}/cowrie/monitor.sh\n"
        rows = {node: {"host": host, "files": {}, "cron": legacy if node == "02" else "",
                       "mount": "shared"} for node, host in rollout.HOSTS.items()}
        events = []
        def controller(_ssh, node, action, *_args, **_kwargs):
            events.append((node, action))
            if (node, action) == ("02", "cutover"):
                raise RuntimeError("cutover failure")
        ssh = mock.Mock()
        ssh.run.return_value = "test1:False"
        with (mock.patch.object(rollout, "inspect_all", return_value=rows),
              mock.patch.object(rollout, "controller", side_effect=controller)):
            with self.assertRaisesRegex(RuntimeError, "cutover failure"):
                rollout.activate(ssh, rollout.REMOTE_DEFAULT, "test1", rollout.HOSTS, {})
        self.assertEqual(events, [("01", "cutover"), ("02", "cutover"),
                                  ("02", "rollback"), ("01", "rollback")])

    def test_check_requires_all_four_ssh_targets(self):
        with mock.patch.object(rollout, "SSH") as ssh:
            result = rollout.main(["check", "--target", "01=one"])
        self.assertEqual(result, 1)
        ssh.assert_not_called()

    def test_release_excludes_private_and_state_files(self):
        self.assertTrue({"sharded/cluster.py", "init_sharded.sh"}.issubset(rollout.manifest()))
        self.assertFalse(any(".env" in name or ".sharded-state" in name
                             for name in rollout.RELEASE))


if __name__ == "__main__":
    unittest.main()
