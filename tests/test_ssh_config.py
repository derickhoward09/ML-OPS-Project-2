"""Verify jump-host policy using the installed OpenSSH client, without networking."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

CONFIG = Path(__file__).resolve().parents[1] / "scripts/ssh_config"


@unittest.skipUnless(shutil.which("ssh"), "OpenSSH client required")
class JumpHostPolicyTests(unittest.TestCase):
    def test_only_turing_ignores_stored_host_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "ssh_config"
            # Isolate this test from the developer's authentication settings.
            config.write_text(CONFIG.read_text().replace("Include ~/.ssh/config", "# User configuration excluded for this test"))
            for host in ("turing.wpi.edu", "unrelated.example.edu"):
                result = subprocess.run(["ssh", "-G", "-F", str(config), host],
                                        capture_output=True, text=True, check=True)
                options = dict(line.split(" ", 1) for line in result.stdout.splitlines())
                if host == "turing.wpi.edu":
                    self.assertEqual(options["stricthostkeychecking"], "false")
                    self.assertEqual(options["userknownhostsfile"], "/dev/null")
                    self.assertEqual(options["globalknownhostsfile"], "/dev/null")
                else:
                    self.assertEqual(options["stricthostkeychecking"], "ask")
                    self.assertNotEqual(options["userknownhostsfile"], "/dev/null")

    def test_proxyjump_passes_configuration_to_jump_process(self):
        result = subprocess.run(["ssh", "-vvG", "-F", str(CONFIG), "-J",
                                 "akrett@turing.wpi.edu", "student-admin@example.edu"],
                                capture_output=True, text=True, check=True)
        proxy = next(line for line in result.stderr.splitlines() if "implicit ProxyCommand" in line)
        self.assertIn(f"-F {CONFIG}", proxy)
        self.assertIn("turing.wpi.edu", proxy)


if __name__ == "__main__":
    unittest.main()
