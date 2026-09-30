"""Offline quorum, takeover, and preflight tests for the sharded scheduler."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "sharded" / "cluster.py"
SPEC = importlib.util.spec_from_file_location("sharded_cluster", SCRIPT)
cluster = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cluster)


class ShardedTests(unittest.TestCase):
    def test_bare_dry_run_explains_required_prepare_context(self):
        wrapper = SCRIPT.parents[1] / "init_sharded.sh"
        result = subprocess.run([str(wrapper), "--dry-run"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("prepare --dry-run --node-id ID", result.stderr)
        self.assertIn("after preflight", result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shared = cluster.Shared(self.temp.name)
        self.generation = "test-generation"
        self.now = cluster.SCAN_START + 1000
        self.shared.put("rosters", f"{self.generation}.json", value={
            "generation": self.generation, "members": cluster.DEFAULT_HOSTS})
        self.shared.put("active.json", value={
            "generation": self.generation, "digest": cluster.SOURCE_DIGEST,
            "roster": cluster.DEFAULT_HOSTS, "active": True, "time": self.now - 1000})
        self.state_dir = str(Path(self.temp.name) / "local")
        self.env_patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_dir})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for node in cluster.NODES:
            cluster.atomic_json(cluster.local_state(node) / "mount.json",
                                cluster.mount_identity(self.shared) | {"generation": self.generation})

    def vote(self, slot, node, quick, deep=None):
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            cluster.write_vote(self.shared, slot, node, {"quick": quick, "deep": deep})

    def test_two_matching_failures_and_conflicting_vote(self):
        slot = int(self.now // 60)
        self.vote(slot, "01", "healthy", "deployment")
        self.vote(slot, "02", "healthy", "healthy")
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.resolved_verdict(
                self.shared, slot, "01", self.now)[0], "healthy")
        self.vote(slot, "03", "healthy", "deployment")
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.resolved_verdict(
                self.shared, slot, "01", self.now)[0], "failed")

    def test_stale_votes_are_excluded(self):
        slot = int(self.now // 60)
        self.vote(slot, "01", "failed")
        self.vote(slot, "02", "failed")
        self.shared.put("votes", str(slot), "02.json",
                        value={"node": "02", "quick": "failed",
                               "time": self.now - 91, "digest": cluster.SOURCE_DIGEST})
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.resolved_verdict(
                self.shared, slot, "01", self.now)[0], "unknown")

    def test_old_generation_votes_do_not_count_after_promotion(self):
        slot = int(self.now // 60)
        self.vote(slot, "01", "failed")
        self.vote(slot, "02", "failed")
        self.shared.put("active.json", value={"generation": "promoted",
                        "digest": cluster.SOURCE_DIGEST, "active": True,
                        "roster": cluster.DEFAULT_HOSTS, "time": self.now})
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.votes(self.shared, slot), {})

    def test_solo_requires_three_minutes_and_two_local_failures(self):
        slot = int(self.now // 60)
        self.vote(slot - 1, "01", "failed")
        self.vote(slot, "01", "failed")
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.resolved_verdict(
                self.shared, slot, "01", self.now)[0], "failed")
        self.shared.put("nodes", "02.json", value={
            "node": "02", "time": self.now - 179, "generation": self.generation})
        with mock.patch.object(cluster.time, "time", return_value=self.now):
            self.assertEqual(cluster.resolved_verdict(
                self.shared, slot, "01", self.now)[0], "unknown")

    def test_single_repair_and_alert_after_two_failed_rounds(self):
        slot = int(self.now // 60)
        for peer in cluster.NODES:
            self.shared.put("nodes", f"{peer}.json", value={
                "node": peer, "time": self.now, "generation": self.generation})
        sent = []
        with (mock.patch.object(cluster.time, "time", return_value=self.now),
              mock.patch.object(cluster, "notify", side_effect=lambda _cfg, text: sent.append(text) or True),
              mock.patch.object(cluster, "ping", return_value=True),
              mock.patch.object(cluster.subprocess, "Popen") as launch):
            self.vote(slot, "01", "failed")
            self.vote(slot, "02", "failed")
            cluster.process_monitor(self.shared, {}, "01", slot, self.generation)
            cluster.process_monitor(self.shared, {}, "02", slot, self.generation)
            self.assertEqual(launch.call_count, 1)
            self.assertFalse(any("Cowrie node 24 DOWN" in text for text in sent))
            self.vote(slot + 1, "01", "failed")
            self.vote(slot + 1, "02", "failed")
            cluster.process_monitor(self.shared, {}, "01", slot + 1, self.generation)
            self.assertEqual(sum("Cowrie node 24 DOWN" in text for text in sent), 1)
            self.vote(slot + 2, "01", "healthy", "healthy")
            self.vote(slot + 2, "02", "healthy", "healthy")
            cluster.process_monitor(self.shared, {}, "01", slot + 2, self.generation)
            self.assertEqual(sum("Cowrie node 24 RECOVERED" in text for text in sent), 1)

    def test_fixed_shards_and_backup_grace(self):
        cycle = {"id": 123, "start": self.now - 299, "generation": self.generation}
        with (mock.patch.object(cluster.time, "time", return_value=self.now),
              mock.patch.object(cluster, "scan_ports", return_value=[
                  {"node": node, "port": 22000 + node, "ok": False, "detail": ""}
                  for node in range(2, 10)]) as scanner):
            cluster.scan_shard(self.shared, "02", self.generation, cycle, "01")
            scanner.assert_not_called()
            cycle["start"] = self.now - 300
            cluster.scan_shard(self.shared, "02", self.generation, cycle, "01")
            scanner.assert_called_once_with("01")
            row = self.shared.get("shards", "123", "01.json")
            self.assertEqual(row["runner"], "02")
            self.assertEqual([entry["node"] for entry in row["results"]], list(range(2, 10)))

    def test_one_aggregate_redteam_result_and_scan_window(self):
        self.assertFalse(cluster.scan_window(cluster.SCAN_START - 1))
        self.assertTrue(cluster.scan_window(cluster.SCAN_START))
        self.assertFalse(cluster.scan_window(cluster.SCAN_END))
        cycle = 987
        self.shared.put("scan-current.json", value={
            "id": cycle, "start": self.now, "generation": self.generation,
            "started": True})
        for owner, first in (("01", 2), ("02", 10), ("03", 18)):
            self.shared.put("shards", str(cycle), f"{owner}.json", value={
                "owner": owner, "runner": owner, "generation": self.generation,
                "results": [{"node": number, "ok": True}
                            for number in range(first, first + 8)]})
        with (mock.patch.object(cluster.time, "time", return_value=self.now),
              mock.patch.object(cluster, "ping", return_value=True) as ping):
            cluster.process_scan(self.shared, {}, self.generation)
            cluster.process_scan(self.shared, {}, self.generation)
        self.assertEqual(ping.call_count, 1)
        self.assertEqual(self.shared.get("latest-scan.json")["checked"], 24)

    def test_three_process_preflight_checks_shared_locks(self):
        generation = "process-test"
        self.shared.put("rosters", f"{generation}.json", value={
            "generation": generation, "members": cluster.DEFAULT_HOSTS})
        commands = [[sys.executable, str(SCRIPT), "preflight",
                     "--shared-dir", self.temp.name, "--generation", generation,
                     "--node-id", node] for node in cluster.NODES]
        processes = [subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      env=os.environ | {"SHARDED_TEST_HOSTNAME": cluster.DEFAULT_HOSTS[node]})
                     for node, cmd in zip(cluster.NODES, commands)]
        for node, process in zip(cluster.NODES, processes):
            out, err = process.communicate(timeout=70)
            self.assertEqual(process.returncode, 0, f"{node}: {out}\n{err}")
        proof = self.shared.get("preflight", generation, "proof.json")
        self.assertEqual(proof["nodes"], list(cluster.NODES))

    def test_roster_rejects_duplicate_physical_host_and_wrong_claim(self):
        with self.assertRaisesRegex(RuntimeError, "distinct physical hosts"):
            cluster.register_roster(self.shared, "duplicate",
                                    cluster.DEFAULT_HOSTS | {"02": cluster.DEFAULT_HOSTS["01"]})
        with mock.patch.dict(os.environ, {"SHARDED_TEST_HOSTNAME": cluster.DEFAULT_HOSTS["02"]}):
            with self.assertRaisesRegex(RuntimeError, "cannot claim slot 01"):
                cluster.require_host(self.shared, "01", self.generation)

    def test_lost_mount_pauses_work(self):
        with mock.patch.object(cluster, "mount_identity", return_value={"path": "changed"}):
            with self.assertRaisesRegex(RuntimeError, "mount identity changed"):
                cluster.require_mount(self.shared, "01", self.generation)

    def test_standby_promotion_requires_new_roster_and_old_generation_is_inert(self):
        new_generation = "promoted"
        promoted = cluster.DEFAULT_HOSTS | {"03": cluster.DEFAULT_HOSTS["04"],
                                            "04": cluster.DEFAULT_HOSTS["03"]}
        cluster.register_roster(self.shared, new_generation, promoted)
        with mock.patch.dict(os.environ, {"SHARDED_TEST_HOSTNAME": promoted["03"]}):
            cluster.require_host(self.shared, "03", new_generation)
            with self.assertRaisesRegex(RuntimeError, "cannot claim"):
                cluster.require_host(self.shared, "03", self.generation)
        self.assertFalse(cluster.active(self.shared, new_generation))
        self.shared.put("active.json", value={"generation": new_generation,
                        "digest": cluster.SOURCE_DIGEST, "roster": promoted,
                        "active": True, "time": self.now})
        self.assertFalse(cluster.active(self.shared, self.generation))
        self.assertTrue(cluster.active(self.shared, new_generation))

    def test_prepare_stages_dormant_jobs_without_retiring_legacy(self):
        current = "* * * * * /repo/cowrie/monitor.sh # legacy\n"
        installed = []
        success = subprocess.CompletedProcess([], 0, "", "")
        with (mock.patch.object(cluster, "proof"),
              mock.patch.object(cluster, "require_host"),
              mock.patch.object(cluster, "config", return_value={}),
              mock.patch.object(cluster, "check_keys"),
              mock.patch.object(cluster, "check_commands"),
              mock.patch.object(cluster, "current_crontab", return_value=current),
              mock.patch.object(cluster, "install_crontab", side_effect=installed.append),
              mock.patch.object(cluster, "run_persistent", return_value=success),
              mock.patch.object(cluster, "hostname", return_value=cluster.DEFAULT_HOSTS["01"])):
            cluster.prepare(self.shared, "01", self.generation)
        self.assertIn(current, installed[0])
        self.assertIn("# cowrie-sharded-monitor", installed[0])
        self.assertTrue(self.shared.get("active.json")["active"])
        self.assertTrue(self.shared.get("prepared", "01.json")["transport_ready"])

    def test_cutover_rollback_restores_exact_cron(self):
        current = (f"* * * * * {cluster.ROOT}/cowrie/monitor.sh # legacy\n"
                   + cluster.cron_lines("01", self.shared.root, self.generation))
        cron = [current]
        def install(value):
            cron[0] = value
        self.shared.put("active.json", value={"generation": self.generation,
                        "digest": cluster.SOURCE_DIGEST, "active": False,
                        "roster": cluster.DEFAULT_HOSTS})
        with (mock.patch.object(cluster, "proof"),
              mock.patch.object(cluster, "require_host"),
              mock.patch.object(cluster, "hostname", return_value=cluster.DEFAULT_HOSTS["01"]),
              mock.patch.object(cluster, "current_crontab", side_effect=lambda: cron[0]),
              mock.patch.object(cluster, "install_crontab", side_effect=install),
              mock.patch.object(cluster.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))):
            cluster.cutover(self.shared, "01", self.generation)
            self.assertNotIn("# legacy", cron[0])
            cluster.rollback(self.shared, "01", self.generation)
        self.assertEqual(cron[0], current)


if __name__ == "__main__":
    unittest.main()
