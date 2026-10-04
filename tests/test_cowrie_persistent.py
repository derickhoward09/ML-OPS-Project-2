"""Offline integration tests for the persistent Cowrie management connection."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class PersistentConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.cowrie = self.repo / "cowrie"
        self.cowrie.mkdir(parents=True)
        for name in ("persistent.sh", "deploy.sh", "retry_access.sh", "delay_proxy.py"):
            shutil.copy2(REPO_ROOT / "cowrie" / name, self.cowrie / name)
        (self.repo / "scripts").mkdir()
        shutil.copy2(REPO_ROOT / "scripts" / "ssh_key_access.sh", self.repo / "scripts" / "ssh_key_access.sh")
        shutil.copy2(REPO_ROOT / "scripts" / "ssh_config", self.repo / "scripts" / "ssh_config")

        self.home = self.root / "home"
        keys = self.home / ".ssh" / "mlops"
        keys.mkdir(parents=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / "id_ed25519_group_key")],
            check=True,
            capture_output=True,
        )
        (keys / "student-admin_key").write_text("bootstrap-key\n")
        self.public_key = (keys / "id_ed25519_group_key.pub").read_text()
        self.remote_auth = self.root / "remote_authorized_keys"
        self.remote_auth.write_text(self.public_key)
        self.master = self.root / "master-alive"
        self.events = self.root / "ssh-events"

        mock_bin = self.root / "bin"
        mock_bin.mkdir()

        def command(name, body):
            path = mock_bin / name
            path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body)
            path.chmod(0o755)

        command("flock", "exit 0\n")
        command("sleep", "exit 0\n")
        command(
            "timeout",
            'if [[ "${1:-}" == --kill-after=* ]]; then shift; fi\n'
            'shift\nexec "$@"\n',
        )
        command(
            "date",
            'if [[ "${*: -1}" == +%s ]]; then printf "%s\\n" "${MOCK_NOW_EPOCH:-0}"; '
            'else exec /bin/date "$@"; fi\n',
        )
        command(
            "python3",
            'if [[ "${1:-}" == - ]]; then cat >/dev/null; exit 0; fi\n'
            'if [[ "${2:-}" == prepare-honeypot ]]; then exit "${MOCK_APP_RECOVERY_STATUS:-0}"; fi\n'
            'exec "$REAL_PYTHON" "$@"\n',
        )
        command(
            "ssh",
            r'''key="" control="" operation="" jump="" is_master=false command_arg="" proxy_blocked=false
for ((i=1; i<=$#; i++)); do
    arg="${!i}"
    case "$arg" in
        -J) next=$((i+1)); jump="${!next}" ;;
        -i) next=$((i+1)); key="${!next}" ;;
        -S) next=$((i+1)); control="${!next}" ;;
        -O) next=$((i+1)); operation="${!next}" ;;
        -N|-MN|-NM) is_master=true ;;
        ProxyCommand=/bin/false) proxy_blocked=true ;;
    esac
    [[ "$arg" == -oProxyCommand=/bin/false ]] && proxy_blocked=true
done
command_arg="${!#}"
if [[ "$operation" == check ]]; then
    printf 'CONTROL_CHECK\n' >> "$FAKE_SSH_EVENTS"
    [[ -f "$FAKE_MASTER" ]] || exit 255
    printf 'Master running (pid=1234)\n'
    exit 0
fi
if [[ "$operation" == exit ]]; then
    printf 'CONTROL_EXIT\n' >> "$FAKE_SSH_EVENTS"
    rm -f "$FAKE_MASTER"
    rm -f "$control"
    exit 0
fi
if [[ -z "$operation" && ( -z "$control" || "$is_master" == true ) ]]; then
    [[ "$jump" == "${FAKE_EXPECTED_JUMP:-turing.wpi.edu}" ]] || {
        echo "Unexpected jump host: $jump" >&2
        exit 96
    }
fi
if "$is_master"; then
    printf 'MASTER_START\n' >> "$FAKE_SSH_EVENTS"
    [[ "$key" == */id_ed25519_group_key ]] || exit 255
    if [[ "${FAKE_GROUP_TRANSPORT_FAIL:-0}" == 1 ]]; then
        echo 'Connection timed out.' >&2
        exit 255
    fi
    if ! grep -Fqx -- "$(cat "$HOME/.ssh/mlops/id_ed25519_group_key.pub")" "$FAKE_REMOTE_AUTH"; then
        echo 'Permission denied (publickey).' >&2
        exit 255
    fi
    : > "$control"
    : > "$FAKE_MASTER"
    exit 0
fi
if [[ -n "$control" ]]; then
    [[ -f "$FAKE_MASTER" ]] || {
        printf 'MUX_WITHOUT_MASTER\n' >> "$FAKE_SSH_EVENTS"
        exit 255
    }
    "$proxy_blocked" || {
        printf 'MUX_WITHOUT_PROXY_BLOCK\n' >> "$FAKE_SSH_EVENTS"
        exit 97
    }
    printf 'MUX_COMMAND\n' >> "$FAKE_SSH_EVENTS"
    if [[ "$command_arg" == true ]]; then
        exit 0
    fi
    if [[ "$command_arg" == sudo\ -n\ bash\ -s* ]]; then
        cat >/dev/null
        case "${FAKE_DEPLOY_STATUS:-auto}" in
            auto)
                if cmp -s "$FAKE_REMOTE_AUTH" "$HOME/.ssh/mlops/id_ed25519_group_key.pub"; then
                    exit 0
                fi
                echo 'Cowrie unhealthy: authorized_keys differs' >&2
                exit 20
                ;;
            0) exit 0 ;;
            20) echo 'Cowrie unhealthy: authorized_keys differs' >&2; exit 20 ;;
            *) echo 'Cowrie unhealthy: service inactive' >&2; exit 1 ;;
        esac
    fi
    if [[ "$command_arg" == 'cat "$HOME/.ssh/authorized_keys"' ]]; then
        cat "$FAKE_REMOTE_AUTH"
        exit 0
    fi
    if [[ "$command_arg" == *'auth="$HOME/.ssh/authorized_keys"'*'tmp=$(mktemp'* ||
          "$command_arg" == *'mktemp'*'authorized_keys'* ]]; then
        cat > "$FAKE_REMOTE_AUTH"
        exit 0
    fi
    echo "Unexpected mux command: $command_arg" >&2
    exit 98
fi
if [[ "$key" == */id_ed25519_group_key ]]; then
    printf 'DIRECT_GROUP\n' >> "$FAKE_SSH_EVENTS"
    grep -Fqx -- "$(cat "$HOME/.ssh/mlops/id_ed25519_group_key.pub")" "$FAKE_REMOTE_AUTH" || exit 255
elif [[ "$key" == */student-admin_key ]]; then
    printf 'DIRECT_STUDENT\n' >> "$FAKE_SSH_EVENTS"
    grep -Fqx 'bootstrap-key' "$FAKE_REMOTE_AUTH" || exit 255
else
    printf 'DIRECT_UNKNOWN\n' >> "$FAKE_SSH_EVENTS"
    exit 255
fi
if [[ "$command_arg" == sudo\ -n\ bash\ -s* ]]; then
    printf 'DEPLOY_CHECK\n' >> "$FAKE_SSH_EVENTS"
    cat >/dev/null
    exit "${FAKE_DIRECT_CHECK_STATUS:-0}"
fi
if [[ "$command_arg" == 'sudo -n bash -c true' ]]; then
    printf 'INSTALL_STARTED\n' >> "$FAKE_SSH_EVENTS"
    exit 99
fi
case "$command_arg" in
    true) exit 0 ;;
    'cat "$HOME/.ssh/authorized_keys"') cat "$FAKE_REMOTE_AUTH" ;;
    *'incoming=$(cat)'*)
        incoming="$(cat)"
        if ! grep -Fqx -- "$incoming" "$FAKE_REMOTE_AUTH"; then
            printf '\n%s\n' "$incoming" >> "$FAKE_REMOTE_AUTH"
        fi
        ;;
    *'mktemp'*'authorized_keys'*) cat > "$FAKE_REMOTE_AUTH" ;;
    *) echo "Unexpected direct command: $command_arg" >&2; exit 99 ;;
esac
''',
        )

        self.env = os.environ.copy()
        self.env.pop("SSH_JUMP", None)
        self.env.pop("RESUMELENS_SSH_JUMP", None)
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": str(mock_bin) + os.pathsep + self.env["PATH"],
                "XDG_STATE_HOME": str(self.root / "state"),
                "COWRIE_PERSISTENT_STATE_DIR": str(self.root / "persistent-state"),
                "FAKE_REMOTE_AUTH": str(self.remote_auth),
                "FAKE_MASTER": str(self.master),
                "FAKE_SSH_EVENTS": str(self.events),
                "REAL_PYTHON": shutil.which("python3") or "/usr/bin/python3",
                "MOCK_NOW_EPOCH": "1000000",
            }
        )

    def run_persistent(self, mode, *, epoch=1_000_000, **extra_env):
        env = self.env.copy()
        env.update({"MOCK_NOW_EPOCH": str(epoch), **extra_env})
        return subprocess.run(
            ["bash", str(self.cowrie / "persistent.sh"), mode],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def assert_status(self, expected, mode, **kwargs):
        result = self.run_persistent(mode, **kwargs)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def seen(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_app_recovery_failure_defers_persistent_restoration(self):
        result = self.assert_status(1, "--recover", MOCK_APP_RECOVERY_STATUS="1")
        self.assertIn("deferring honeypot recovery", result.stdout)
        self.assertEqual(self.seen(), [])

    def test_healthy_checks_reuse_one_master_and_block_direct_fallback(self):
        self.assert_status(0, "--initialize")
        self.events.write_text("")
        self.assert_status(0, "--check")
        self.assert_status(0, "--check")
        self.assertEqual(self.seen().count("CONTROL_CHECK"), 2)
        self.assertEqual(self.seen().count("MUX_COMMAND"), 2)
        self.assertFalse(any(event.startswith("DIRECT_") or event == "MASTER_START" for event in self.seen()))

        self.master.unlink()
        self.events.write_text("")
        self.assert_status(2, "--check")
        self.assertEqual(self.seen(), ["CONTROL_CHECK"])

    def test_lost_master_recovers_group_access_through_student_key(self):
        self.assert_status(0, "--initialize")
        self.master.unlink()
        self.remote_auth.write_text("bootstrap-key\n")
        self.events.write_text("")
        self.assert_status(2, "--check")
        self.assert_status(0, "--recover")
        self.assertTrue(self.master.exists())
        self.assertEqual(self.remote_auth.read_text(), self.public_key)
        events = self.seen()
        self.assertIn("DIRECT_STUDENT", events)
        self.assertGreater(events.index("DIRECT_STUDENT"), events.index("MASTER_START"))
        self.assert_status(0, "--check")

    def test_alive_master_detects_authorized_keys_mismatch(self):
        self.assert_status(0, "--initialize")
        self.remote_auth.write_text(self.public_key + "unexpected-extra-key\n")
        self.events.write_text("")
        self.assert_status(3, "--check")
        self.assertTrue(self.master.exists())
        self.assert_status(0, "--recover")
        self.assertEqual(self.remote_auth.read_text(), self.public_key)
        self.assertFalse(any(event.startswith("DIRECT_") or event == "MASTER_START" for event in self.seen()))

    def test_jump_host_precedence_for_deploy_and_master(self):
        for overrides, expected in (
            ({}, "turing.wpi.edu"),
            ({"SSH_JUMP": "legacy.example"}, "legacy.example"),
            ({"SSH_JUMP": "legacy.example", "RESUMELENS_SSH_JUMP": "preferred.example"}, "preferred.example"),
        ):
            with self.subTest(expected=expected):
                env = dict(self.env, **overrides, FAKE_EXPECTED_JUMP=expected)
                result = subprocess.run(
                    ["bash", str(self.cowrie / "deploy.sh"), "--check"],
                    env=env, text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = self.run_persistent("--initialize", **overrides, FAKE_EXPECTED_JUMP=expected)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_status(0, "--stop")

    def test_deploy_only_installs_after_confirmed_unhealthy_check(self):
        for remote_status, expected, installs in ((255, 2, False), (20, 3, False), (99, 1, False), (1, 99, True)):
            with self.subTest(remote_status=remote_status):
                self.events.write_text("")
                result = subprocess.run(
                    ["bash", str(self.cowrie / "deploy.sh")],
                    env=dict(self.env, FAKE_DIRECT_CHECK_STATUS=str(remote_status)),
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual("INSTALL_STARTED" in self.seen(), installs)
                if remote_status == 255:
                    self.assertIn("Cowrie health unknown; management SSH unavailable", result.stderr)

    def test_stale_slave_socket_never_opens_direct_connection(self):
        control_path = self.root / "persistent-state" / "master.sock"
        control_path.parent.mkdir(parents=True)
        control_path.write_text("stale socket marker\n")
        env = self.env.copy()
        env["COWRIE_SSH_CONTROL_PATH"] = str(control_path)
        result = subprocess.run(
            ["bash", str(self.cowrie / "deploy.sh"), "--check"],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("MUX_WITHOUT_MASTER", self.seen())
        self.assertFalse(any(event.startswith("DIRECT_") for event in self.seen()))

    def test_stop_closes_master_and_next_check_does_not_reauthenticate(self):
        self.assert_status(0, "--initialize")
        control_path = self.root / "persistent-state" / "master.sock"
        self.assertTrue(self.master.exists())
        self.assertTrue(control_path.exists())

        self.events.write_text("")
        self.assert_status(0, "--stop")
        self.assertIn("CONTROL_EXIT", self.seen())
        self.assertFalse(self.master.exists())
        self.assertFalse(control_path.exists())

        self.assert_status(2, "--check")
        self.assertFalse(any(event.startswith("DIRECT_") or event == "MASTER_START" for event in self.seen()))

    def test_cleanup_stop_preserves_retry_state(self):
        self.assert_status(0, "--initialize")
        retry_state = self.root / "persistent-state" / "retry-state"
        retry_state.write_text("1000000 16666\n")

        self.assert_status(0, "--stop-keep-state")
        self.assertFalse(self.master.exists())
        self.assertEqual(retry_state.read_text(), "1000000 16666\n")

    def test_transport_failure_does_not_probe_student_key(self):
        self.assertNotEqual(
            self.run_persistent("--recover", FAKE_GROUP_TRANSPORT_FAIL="1").returncode,
            0,
        )
        self.assertIn("MASTER_START", self.seen())
        self.assertNotIn("DIRECT_STUDENT", self.seen())

    def test_existing_cooldown_state_allows_retry_in_next_minute(self):
        self.remote_auth.write_text("")
        state_dir = self.root / "persistent-state"
        state_dir.mkdir()
        start = 1_000_000
        (state_dir / "retry-state").write_text(f"{start} {start // 60}\n")

        self.assertNotEqual(self.run_persistent("--recover", epoch=start + 25 * 60).returncode, 0)
        self.assertEqual(self.seen().count("MASTER_START"), 1)
        self.assertEqual(self.seen().count("DIRECT_STUDENT"), 1)

    def test_retries_continue_without_cooldown_once_per_minute(self):
        self.remote_auth.write_text("")
        start = 1_000_000
        for minute in range(20):
            self.assertNotEqual(self.run_persistent("--recover", epoch=start + 60 * minute).returncode, 0)
        first_attempts = self.seen().count("DIRECT_STUDENT")
        self.assertEqual(first_attempts, 20)
        self.assertEqual(self.seen().count("MASTER_START"), 20)

        self.assertNotEqual(self.run_persistent("--recover", epoch=start + 60 * 19).returncode, 0)
        self.assertEqual(self.seen().count("DIRECT_STUDENT"), first_attempts)

        for attempt, minute in enumerate((20, 25, 49, 50, 1440), start=1):
            self.assertNotEqual(self.run_persistent("--recover", epoch=start + 60 * minute).returncode, 0)
            self.assertEqual(self.seen().count("DIRECT_STUDENT"), first_attempts + attempt)
            self.assertEqual(self.seen().count("MASTER_START"), first_attempts + attempt)
            self.assertNotEqual(self.run_persistent("--recover", epoch=start + 60 * minute + 1).returncode, 0)
            self.assertEqual(self.seen().count("MASTER_START"), first_attempts + attempt)


if __name__ == "__main__":
    unittest.main()
