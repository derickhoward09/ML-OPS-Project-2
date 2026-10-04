"""Exercise target-side recovery with simulated systemd state and boot time."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "cowrie/remote_recovery.sh"
SHELL_FIXTURE = r'''
RECOVERY_DIR="$FIXTURE_STATE"
configuration_health() { return "${CONFIG_STATUS:-0}"; }
prepare_recovery_directory() { mkdir -p "$RECOVERY_DIR"; chmod 700 "$RECOVERY_DIR"; }
monotonic_seconds() { printf '%s\n' "$NOW"; }
boot_age_seconds() { printf '%s\n' "${BOOT_AGE:-$NOW}"; }
ss() {
    [[ "${SS_FAIL:-0}" == 0 ]] || return 1
    cat "$FIXTURE_SOCKETS"
}
systemctl() {
    case "$1" in
        show)
            if [[ "$2" == cowrie.service ]]; then
                printf 'ActiveState=%s\nExecMainStartTimestampMonotonic=%s\nNRestarts=%s\n' \
                    "${COWRIE_ACTIVE:-active}" "${COWRIE_START:-1000000}" "${COWRIE_RESTARTS:-0}"
            else
                printf 'ActiveState=%s\nExecMainStartTimestampMonotonic=%s\nNRestarts=%s\n' \
                    "${PROXY_ACTIVE:-active}" "${PROXY_START:-1000000}" "${PROXY_RESTARTS:-0}"
            fi
            ;;
        restart)
            printf '%s\n' "$*" >> "$FIXTURE_EVENTS"
            if [[ "${RESTART_FIXES:-0}" == 1 ]]; then
                printf 'LISTEN 0 50 127.0.0.1:2222 0.0.0.0:*\nLISTEN 0 100 0.0.0.0:22001 0.0.0.0:*\n' > "$FIXTURE_SOCKETS"
            fi
            return "${RESTART_STATUS:-0}"
            ;;
        *) return 99 ;;
    esac
}
'''


class TargetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "run"
        self.sockets = self.root / "sockets"
        self.events = self.root / "events"
        self.set_sockets()
        self.env = dict(os.environ, NOW="1000", FIXTURE_STATE=str(self.state),
                        FIXTURE_SOCKETS=str(self.sockets), FIXTURE_EVENTS=str(self.events))

    def set_sockets(self, cowrie="127.0.0.1:2222", proxy="0.0.0.0:22001"):
        self.sockets.write_text("".join(f"LISTEN 0 50 {address} 0.0.0.0:*\n"
                                        for address in (cowrie, proxy) if address))

    def run_helper(self, action="remote_health", **env):
        script = f"source {shlex.quote(str(HELPER))}\n" + SHELL_FIXTURE + f"\n{action}\n"
        return subprocess.run(["bash", "-c", script], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=10)

    def assert_code(self, expected, action="remote_health", **env):
        result = self.run_helper(action, **env)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def seen(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_healthy_check_is_read_only(self):
        self.assert_code(0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.seen(), [])

    def test_clock_matches_systemd_monotonic_clock(self):
        result = subprocess.run(
            ["bash", "-c", f"source {shlex.quote(str(HELPER))}; monotonic_seconds"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertLess(abs(int(result.stdout) - time.monotonic()), 5)

    def test_slow_startup_and_grace_boundary(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, NOW="1179", COWRIE_START="1000000000")
        self.assert_code(6, NOW="1180", COWRIE_START="1000000000")
        self.assertFalse(self.state.exists())
        self.assert_code(5, "recover_runtime", NOW="1179", COWRIE_START="1000000000")
        self.assertEqual(self.seen(), [])

    def test_activating_service_receives_grace(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, COWRIE_ACTIVE="activating", COWRIE_START="950000000")

    def test_automatic_restart_loop_does_not_renew_grace(self):
        self.set_sockets(cowrie=None)
        self.assert_code(6, COWRIE_START="999000000", COWRIE_RESTARTS="10")

    def test_container_boot_grace_uses_vm_uptime_for_restart_loops(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, NOW="6500000", COWRIE_START="6499999000000", COWRIE_RESTARTS="10", BOOT_AGE="179")
        self.assert_code(6, NOW="6500000", COWRIE_START="6499999000000", COWRIE_RESTARTS="10", BOOT_AGE="180")

    def test_cowrie_restart_includes_dependent_proxy(self):
        self.set_sockets(cowrie=None)
        self.assert_code(0, "recover_runtime", RESTART_FIXES="1")
        self.assertEqual(self.seen(), ["restart cowrie.service cowrie-delay.service"])
        self.assert_code(0)

    def test_proxy_restart_does_not_interrupt_cowrie(self):
        self.set_sockets(proxy=None)
        self.assert_code(0, "recover_runtime", RESTART_FIXES="1")
        self.assertEqual(self.seen(), ["restart cowrie-delay.service"])

    def test_failed_restart_waits_then_requests_reconciliation(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, "recover_runtime", RESTART_STATUS="1")
        self.assert_code(5, "recover_runtime", NOW="1179")
        self.assert_code(1, "recover_runtime", NOW="1180")
        self.assertEqual(len(self.seen()), 1)

    def test_reconciliation_and_restart_rate_limits(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, "recover_runtime")
        self.assert_code(1, "recover_runtime", NOW="1180")
        self.assert_code(0, "recovery_lock && reserve_reconciliation", NOW="1180")
        self.assert_code(6, "recovery_lock && reserve_reconciliation", NOW="1479")
        self.assert_code(6, "recover_runtime", NOW="1479")
        self.assertEqual(len(self.seen()), 1)
        self.assert_code(0, "recovery_lock && reserve_reconciliation", NOW="1480")

    def test_next_recovery_cycle_is_allowed_after_five_minutes(self):
        self.set_sockets(cowrie=None)
        self.assert_code(5, "recover_runtime")
        self.assert_code(0, "recovery_lock && reserve_reconciliation", NOW="1180")
        self.assert_code(5, "recover_runtime", NOW="1480")
        self.assertEqual(len(self.seen()), 2)

    def test_config_drift_and_public_binding_bypass_startup_grace(self):
        self.assert_code(1, "recover_runtime", CONFIG_STATUS="1")
        self.set_sockets(cowrie="0.0.0.0:2222")
        result = self.assert_code(1, "recover_runtime", COWRIE_START="999000000")
        self.assertIn("unexpected binding", result.stderr)
        self.assertEqual(self.seen(), [])

    def test_inconclusive_observation_never_restarts(self):
        self.assert_code(7, "recover_runtime", SS_FAIL="1")
        self.assert_code(7, "recover_runtime", COWRIE_ACTIVE="unknown")
        self.assert_code(7, "recover_runtime", COWRIE_START="1001000000")
        self.assert_code(7, "recover_runtime", NOW="unknown")
        self.assertEqual(self.seen(), [])

    def test_missing_listener_is_reported_separately(self):
        self.set_sockets(cowrie=None)
        result = self.assert_code(6)
        self.assertIn("port 2222 is not listening", result.stderr)
        self.assertNotIn("unexpected binding", result.stderr)

    def test_boot_clears_repair_state(self):
        self.assert_code(0, "recovery_lock && reserve_reconciliation")
        for child in self.state.iterdir():
            child.unlink()
        self.state.rmdir()
        self.set_sockets(cowrie=None)
        self.assert_code(5, NOW="60", COWRIE_START="1000000")
        self.assertFalse(self.state.exists())

    def test_real_flock_prevents_overlapping_mutations(self):
        self.state.mkdir()
        with (self.state / "lock").open("w") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.set_sockets(cowrie=None)
            self.assert_code(5, "recover_runtime")
            self.assert_code(5, "recovery_lock && reserve_reconciliation")
            self.assertEqual(self.seen(), [])
            self.assertFalse((self.state / "restart").exists())
            self.assertFalse((self.state / "reconcile").exists())


if __name__ == "__main__":
    unittest.main()
