#!/usr/bin/env python3
"""Interactive setup and scheduler-side recovery for the ResumeLens VM."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import signal
import shlex
import shutil
import stat
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_SOURCE = Path.home() / "cs553-case-study-1"
APP_GIT_URL = "git@github.com:alexander-krett/cs553-case-study-1.git"
APP_BRANCH = "local-deploy-main"
HC_PATTERN = re.compile(r"^https://hc-ping\.com/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DISCORD_PATTERN = re.compile(r"^https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$")
TOKEN_PATTERN = re.compile(r"^hf_[A-Za-z0-9]{10,}$")
CONFIG_KEYS = (
    "HEALTHCHECKS_PING_URL", "REDTEAM_HEALTHCHECKS_PING_URL", "VM_HEALTHCHECKS_PING_URL",
    "DISCORD_WEBHOOK_URL", "APP_SOURCE", "SSH_HOST", "SSH_PORT", "SSH_KEY",
    "SSH_BOOTSTRAP_KEY", "SSH_GROUP_PUBLIC_KEY", "SSH_JUMP",
)


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"configuration must be a regular file: {path}")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'}:
            parsed = shlex.split(value, posix=True)
            if len(parsed) != 1:
                raise ValueError(f"invalid value for {key} in {path}")
            value = parsed[0]
        result[key] = value
    return result


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def update_env(path: Path, changes: dict[str, str]) -> None:
    """Atomically update named dotenv values while preserving other entries."""
    if any("\n" in value or "\r" in value for value in changes.values()):
        raise ValueError("configuration values must fit on one line")
    old_lines = path.read_text().splitlines() if path.exists() else []
    output = []
    remaining = dict(changes)
    written = set()
    for line in old_lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
            written.add(key)
        elif key in written:
            continue
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    atomic_write(path, "\n".join(output) + "\n")


def valid_config(values: dict[str, str]) -> None:
    healthchecks_urls = [values.get(key, "") for key in ("HEALTHCHECKS_PING_URL", "REDTEAM_HEALTHCHECKS_PING_URL", "VM_HEALTHCHECKS_PING_URL")]
    for key in ("HEALTHCHECKS_PING_URL", "REDTEAM_HEALTHCHECKS_PING_URL", "VM_HEALTHCHECKS_PING_URL"):
        if not HC_PATTERN.fullmatch(values.get(key, "")):
            raise ValueError(f"{key} must be a Healthchecks.io check URL")
    if len(set(healthchecks_urls)) != 3:
        raise ValueError("Cowrie, Redteam, and ResumeLens VM require three separate Healthchecks checks")
    if not DISCORD_PATTERN.fullmatch(values.get("DISCORD_WEBHOOK_URL", "")):
        raise ValueError("DISCORD_WEBHOOK_URL must be a Discord webhook URL")
    if not values.get("SSH_HOST") or any(char.isspace() for char in values["SSH_HOST"]) or values["SSH_HOST"].startswith("-"):
        raise ValueError("SSH_HOST must be a hostname or user@hostname")
    if not values.get("SSH_PORT", "").isdigit() or not 1 <= int(values["SSH_PORT"]) <= 65535:
        raise ValueError("SSH_PORT must be between 1 and 65535")
    for key in ("SSH_KEY", "SSH_BOOTSTRAP_KEY", "SSH_GROUP_PUBLIC_KEY", "APP_SOURCE"):
        if not values.get(key):
            raise ValueError(f"{key} is required")
    if values.get("SSH_JUMP") and any(char.isspace() for char in values["SSH_JUMP"]):
        raise ValueError("SSH_JUMP must be empty or user@hostname")


def http_request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 8) -> int:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def test_integrations(values: dict[str, str], token: str) -> None:
    labels = (
        ("HEALTHCHECKS_PING_URL", "Cowrie"),
        ("REDTEAM_HEALTHCHECKS_PING_URL", "Redteam"),
        ("VM_HEALTHCHECKS_PING_URL", "ResumeLens VM"),
    )
    for key, label in labels:
        try:
            status = http_request(values[key])
        except (OSError, urllib.error.URLError) as error:
            raise RuntimeError(f"{label} Healthchecks test failed ({type(error).__name__})") from error
        if status < 200 or status >= 300:
            raise RuntimeError(f"{label} Healthchecks returned HTTP {status}")
        print(f"{label} Healthchecks test passed.")

    test_embed = {
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": "ResumeLens setup test", "description": "Discord notification delivery is configured.", "color": 0x5865F2, "footer": {"text": "Setup test • no resource alert"}}],
    }
    try:
        status = http_request(values["DISCORD_WEBHOOK_URL"], json.dumps(test_embed).encode(), {"Content-Type": "application/json"})
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"Discord webhook test failed ({type(error).__name__})") from error
    if status < 200 or status >= 300:
        raise RuntimeError(f"Discord webhook returned HTTP {status}")
    print("Discord webhook test passed.")

    request = urllib.request.Request("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {token}", "User-Agent": "ResumeLens-VM-Setup/1"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("Hugging Face token validation failed")
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"Hugging Face token test failed ({type(error).__name__})") from error
    print("Hugging Face token test passed.")


def prompt_value(label: str, key: str, old: str, *, secret: bool = False, default: str | None = None, allow_empty: bool = False) -> str:
    fallback = "direct" if allow_empty and not default else (default or "required")
    prompt = f"{label} [{'<configured>' if old else fallback}] (Enter keeps current): "
    while True:
        entered = getpass.getpass(prompt) if secret else input(prompt)
        if allow_empty and entered == "-":
            return ""
        value = entered or old or default or ""
        if value or allow_empty:
            return value
        print(f"{key} is required.")


def gather_config() -> tuple[dict[str, str], str]:
    env_file = ROOT / ".env"
    old = parse_env(env_file)
    secret_file = ROOT / ".hf.env"
    if secret_file.is_symlink() or (secret_file.exists() and not secret_file.is_file()):
        raise ValueError(".hf.env must be a regular file")
    old_token = ""
    if secret_file.exists():
        contents = secret_file.read_text().strip()
        old_token = contents.split("=", 1)[1].strip().strip("\"'") if contents.startswith("HF_TOKEN=") else contents
    defaults = {
        "APP_SOURCE": str(DEFAULT_APP_SOURCE),
        "SSH_HOST": "student-admin@paffenroth-23.dyn.wpi.edu",
        "SSH_PORT": "23001",
        "SSH_KEY": str(Path.home() / ".ssh/mlops/id_ed25519_group_key"),
        "SSH_BOOTSTRAP_KEY": str(Path.home() / ".ssh/mlops/student-admin_key"),
        "SSH_GROUP_PUBLIC_KEY": str(Path.home() / ".ssh/mlops/id_ed25519_group_key.pub"),
        "SSH_JUMP": "",
    }
    labels = {
        "HEALTHCHECKS_PING_URL": "Cowrie Healthchecks URL",
        "REDTEAM_HEALTHCHECKS_PING_URL": "Redteam Healthchecks URL",
        "VM_HEALTHCHECKS_PING_URL": "ResumeLens VM Healthchecks URL",
        "DISCORD_WEBHOOK_URL": "Discord webhook URL",
        "APP_SOURCE": "Managed ResumeLens checkout path",
        "SSH_HOST": "Application VM SSH user@host",
        "SSH_PORT": "Application VM SSH port",
        "SSH_KEY": "Group SSH private key path",
        "SSH_BOOTSTRAP_KEY": "Bootstrap SSH private key path",
        "SSH_GROUP_PUBLIC_KEY": "Group SSH public key path",
        "SSH_JUMP": "Optional SSH jump host (enter - for direct SSH)",
    }
    values: dict[str, str] = {}
    for key in CONFIG_KEYS:
        secret = key == "DISCORD_WEBHOOK_URL"
        values[key] = prompt_value(labels[key], key, old.get(key, ""), secret=secret, default=defaults.get(key), allow_empty=key == "SSH_JUMP")
    token = prompt_value("Hugging Face token", "HF_TOKEN", old_token, secret=True)
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("HF token must be in the form hf_…; value was not saved")
    for key in ("APP_SOURCE", "SSH_KEY", "SSH_BOOTSTRAP_KEY", "SSH_GROUP_PUBLIC_KEY"):
        values[key] = str(Path(values[key]).expanduser().resolve())
    valid_config(values)
    return values, token


def validate_key_files(values: dict[str, str]) -> None:
    for key in ("SSH_KEY", "SSH_BOOTSTRAP_KEY", "SSH_GROUP_PUBLIC_KEY"):
        path = Path(values[key])
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError(f"{key} must name a readable key file")
        if key != "SSH_GROUP_PUBLIC_KEY" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"{key} permissions are too open; set the private key file to mode 0600")


def ensure_app_source(path: Path, *, refresh: bool = True, log: Callable[[str], None] = print) -> bool:
    """Ensure a clean checkout of the required branch. Return whether refreshed."""
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required to obtain ResumeLens source")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([git, "clone", "--branch", APP_BRANCH, "--single-branch", APP_GIT_URL, str(path)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("could not clone the ResumeLens local-deploy-main branch")
        validate_app_source(path)
        return True
    if not (path / ".git").exists():
        raise RuntimeError("APP_SOURCE exists but is not a Git checkout")
    branch = subprocess.run([git, "-C", str(path), "branch", "--show-current"], capture_output=True, text=True)
    origin = subprocess.run([git, "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
    if branch.returncode or branch.stdout.strip() != APP_BRANCH:
        raise RuntimeError(f"APP_SOURCE must be checked out on {APP_BRANCH}")
    if origin.returncode or origin.stdout.strip().removesuffix(".git") != APP_GIT_URL.removesuffix(".git"):
        raise RuntimeError("APP_SOURCE origin is not the expected ResumeLens repository")
    dirty = subprocess.run([git, "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    if dirty.returncode or dirty.stdout.strip():
        raise RuntimeError("APP_SOURCE has local changes; refusing to overwrite or deploy it")
    if not refresh:
        validate_app_source(path)
        return False
    fetched = subprocess.run([git, "-C", str(path), "fetch", "origin", APP_BRANCH], capture_output=True, text=True)
    if fetched.returncode:
        cached_ahead = subprocess.run([git, "-C", str(path), "merge-base", "--is-ancestor", "HEAD", f"origin/{APP_BRANCH}"])
        cached_behind = subprocess.run([git, "-C", str(path), "merge-base", "--is-ancestor", f"origin/{APP_BRANCH}", "HEAD"])
        if cached_ahead.returncode != 0 and cached_behind.returncode == 0:
            raise RuntimeError("APP_SOURCE is ahead of origin/local-deploy-main; only the GitHub branch may be deployed")
        if cached_ahead.returncode != 0 and cached_behind.returncode != 0:
            raise RuntimeError("APP_SOURCE has no validated cached origin/local-deploy-main revision")
        log("GitHub fetch failed; using the existing validated checkout, which may be behind.")
        validate_app_source(path)
        return False
    ahead = subprocess.run([git, "-C", str(path), "merge-base", "--is-ancestor", "HEAD", f"origin/{APP_BRANCH}"])
    behind = subprocess.run([git, "-C", str(path), "merge-base", "--is-ancestor", f"origin/{APP_BRANCH}", "HEAD"])
    if ahead.returncode != 0 and behind.returncode != 0:
        raise RuntimeError("APP_SOURCE and origin/local-deploy-main have diverged")
    if ahead.returncode != 0 and behind.returncode == 0:
        raise RuntimeError("APP_SOURCE is ahead of origin/local-deploy-main; only the GitHub branch may be deployed")
    if ahead.returncode == 0 and behind.returncode != 0:
        merged = subprocess.run([git, "-C", str(path), "merge", "--ff-only", f"origin/{APP_BRANCH}"], capture_output=True, text=True)
        if merged.returncode:
            raise RuntimeError("could not fast-forward APP_SOURCE to origin/local-deploy-main")
        validate_app_source(path)
        return True
    validate_app_source(path)
    return False


def validate_app_source(path: Path) -> None:
    app_source = path / "app.py"
    if not app_source.is_file():
        raise RuntimeError("APP_SOURCE is missing app.py")
    text = app_source.read_text()
    if "resource_capacity_status" not in text or "gr.Timer" not in text:
        raise RuntimeError("publish the ResumeLens capacity banner changes to local-deploy-main before setup or recovery")


def desired_crontab() -> str:
    command = f'* * * * * /usr/bin/python3 "{ROOT}/scripts/resumelens_scheduler.py" monitor >> "{ROOT}/logs/resumelens-monitor.log" 2>&1 # resumelens-recovery'
    return command


def run_crontab_setup(dry_run: bool = False) -> None:
    installer = ROOT / "cowrie/install_cron.sh"
    args = ["/bin/bash", str(installer), "--mode", "minute"]
    if dry_run:
        args.append("--dry-run")
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    if dry_run:
        print(result.stdout, end="")


def init(mode: str) -> int:
    if mode == "dry-run":
        run_crontab_setup(dry_run=True)
        return 0
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("init requires an interactive terminal so secret prompts stay hidden")
    values, token = gather_config()
    validate_key_files(values)
    test_integrations(values, token)
    update_env(ROOT / ".env", values)
    atomic_write(ROOT / ".hf.env", token + "\n")
    if mode == "test":
        print("Configuration saved and integrations verified; cron and VM were not changed.")
        return 0
    source = Path(values["APP_SOURCE"])
    ensure_app_source(source, refresh=True)
    persistent = ROOT / "cowrie/persistent.sh"
    if persistent.exists():
        subprocess.run(["/bin/bash", str(persistent), "--stop-keep-state"], check=True)
    run_crontab_setup()
    print("Minute Cowrie, redteam, and ResumeLens recovery cron entries are installed.")
    state_dir = Path.home() / ".local/state/resumelens-recovery"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = ROOT / "logs/resumelens-recovery.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "recover"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    print("Initial deployment started in the background; cron will retry if it fails.")
    return 0


def scheduler_values() -> dict[str, str]:
    values = parse_env(ROOT / ".env")
    valid_config({key: values.get(key, "") for key in CONFIG_KEYS})
    return values


def ssh_base(values: dict[str, str]) -> list[str]:
    host = values["SSH_HOST"]
    if "@" not in host:
        host = "student-admin@" + host
    state_dir = Path.home() / ".local/state/resumelens-recovery"
    state_dir.mkdir(parents=True, exist_ok=True)
    known_hosts = state_dir / "known_hosts"
    known_hosts.touch(mode=0o600, exist_ok=True)
    os.chmod(known_hosts, 0o600)
    args = ["ssh", "-T", "-i", values["SSH_KEY"], "-p", values["SSH_PORT"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null"]
    if values.get("SSH_JUMP"):
        args += ["-J", values["SSH_JUMP"]]
    args.append(host)
    return args


def ssh_call(values: dict[str, str], remote_command: str, *, timeout: int = 25, input_text: str | None = None) -> subprocess.CompletedProcess:
    command = ssh_base(values) + [remote_command]
    result = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=timeout)
    if result.returncode and "REMOTE HOST IDENTIFICATION HAS CHANGED" in result.stderr:
        host = values["SSH_HOST"].split("@")[-1]
        known_hosts = Path.home() / ".local/state/resumelens-recovery/known_hosts"
        print("Configured VM SSH host key changed; refreshing this VM's isolated host-key record.", flush=True)
        subprocess.run(["ssh-keygen", "-R", f"[{host}]:{values['SSH_PORT']}", "-f", str(known_hosts)], capture_output=True)
        result = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=timeout)
    return result


REMOTE_STATUS = r'''python3 - <<'PY'
import json, os, subprocess, time
root=os.path.expanduser('~/resumelens')
def run(args):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
required=[root+'/.venv/bin/python',root+'/.runtime/bin/llama-server',root+'/.deploy/models.ini',root+'/.deploy/resumelens-app.service',root+'/.deploy/resumelens-inference.service',root+'/.deploy/resumelens-monitor.service',root+'/.deploy/resumelens-vm-heartbeat.service',root+'/.deploy/resumelens-vm-heartbeat.timer',root+'/.deploy/resumelens_vm_monitor.py',root+'/.deploy/resumelens_vm_heartbeat.py',root+'/.deploy/monitor.env',root+'/models/Qwen3-0.6B-Q8_0.gguf',root+'/models/LFM2.5-1.2B-Instruct-QAD-Q4_0.gguf']
if not all(os.path.isfile(path) for path in required):
    print('missing'); raise SystemExit(0)
units=['resumelens-inference','resumelens-app','resumelens-monitor']
if not all(run(['systemctl','is-enabled',unit]) and run(['systemctl','is-active',unit]) for unit in units) or not run(['systemctl','is-enabled','resumelens-vm-heartbeat.timer']) or not run(['systemctl','is-active','resumelens-vm-heartbeat.timer']):
    print('degraded'); raise SystemExit(0)
if not run(['curl','--fail','--silent','--max-time','2','http://127.0.0.1:8080/health']) or not run(['curl','--fail','--silent','--max-time','2','http://127.0.0.1:7860/']):
    print('degraded'); raise SystemExit(0)
try:
    status=json.load(open('/run/resumelens-monitor/status.json'))
    if time.time()-float(status['timestamp'])>8: raise ValueError()
except Exception:
    print('degraded'); raise SystemExit(0)
print('healthy')
PY'''


def inspect_vm(values: dict[str, str]) -> tuple[str, str]:
    try:
        result = ssh_call(values, REMOTE_STATUS, timeout=20)
    except subprocess.TimeoutExpired:
        return "unreachable", "SSH check timed out"
    if result.returncode:
        return "unreachable", result.stderr[-500:]
    return result.stdout.strip().splitlines()[-1], ""


def remote_restart(values: dict[str, str]) -> bool:
    command = "sudo -n systemctl restart resumelens-inference resumelens-app resumelens-monitor resumelens-vm-heartbeat.timer"
    result = ssh_call(values, command, timeout=40)
    return result.returncode == 0


def ensure_access(values: dict[str, str]) -> bool:
    test = ssh_call(values, "true", timeout=15)
    if test.returncode == 0:
        return True
    environment = os.environ.copy()
    environment.update({
        "RESUMELENS_SSH_HOST": values["SSH_HOST"], "RESUMELENS_SSH_PORT": values["SSH_PORT"],
        "RESUMELENS_SSH_KEY": values["SSH_KEY"], "RESUMELENS_BOOTSTRAP_KEY": values["SSH_BOOTSTRAP_KEY"],
        "RESUMELENS_GROUP_PUBLIC_KEY": values["SSH_GROUP_PUBLIC_KEY"],
    })
    result = subprocess.run(["/bin/bash", str(ROOT / "scripts/ssh_key_access.sh")], env=environment, capture_output=True, text=True, timeout=90)
    if result.returncode:
        print("VM SSH key recovery failed.", flush=True)
        return False
    return True


def deploy(values: dict[str, str], token_file: Path) -> bool:
    source = Path(values["APP_SOURCE"])
    ensure_app_source(source, refresh=True, log=lambda message: print(message, flush=True))
    environment = os.environ.copy()
    environment.update({
        "APP_SOURCE": str(source), "TOKEN_FILE": str(token_file),
        "SSH_HOST": values["SSH_HOST"], "SSH_PORT": values["SSH_PORT"],
        "SSH_KEY": values["SSH_KEY"], "SSH_JUMP": values.get("SSH_JUMP", ""),
        "DISCORD_WEBHOOK_URL": values["DISCORD_WEBHOOK_URL"],
        "VM_HEALTHCHECKS_PING_URL": values["VM_HEALTHCHECKS_PING_URL"],
        "RESUMELENS_DEPLOY_LOCK_HELD": "1",
    })
    process = subprocess.Popen(["/bin/bash", str(ROOT / "scripts/deploy_resumelens.sh")], env=environment, start_new_session=True)
    try:
        return process.wait(timeout=2700) == 0
    except subprocess.TimeoutExpired:
        print("Deployment exceeded its 45-minute limit; stopping its process group.", flush=True)
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        return False


def recover(*, force: bool = False) -> int:
    try:
        values = scheduler_values()
    except Exception as error:
        print(f"Recovery configuration unavailable ({type(error).__name__}).")
        return 1
    state_dir = Path.home() / ".local/state/resumelens-recovery"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "deploy.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another ResumeLens recovery or deployment is already running.")
            return 0
        status, detail = inspect_vm(values)
        print(f"VM status: {status}" + (f" ({detail[:200]})" if detail else ""), flush=True)
        if status == "healthy" and not force:
            return 0
        access_ready = status != "unreachable"
        if not access_ready:
            if not ensure_access(values):
                return 1
            access_ready = True
            status, _ = inspect_vm(values)
        if status == "healthy" and not force:
            print("VM is healthy after SSH access recovery.", flush=True)
            return 0
        if status == "degraded" and not force and remote_restart(values):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                time.sleep(5)
                status, _ = inspect_vm(values)
                if status == "healthy":
                    print("Restarted ResumeLens services successfully.", flush=True)
                    return 0
            print("Service restart did not restore health; starting full deployment.", flush=True)

        if status == "unreachable":
            status, _ = inspect_vm(values)
            if status == "unreachable":
                print("VM is still unreachable; the next minute check will retry.", flush=True)
                return 1

        cooldown_path = state_dir / "last-deploy-failure"
        if not force and cooldown_path.exists():
            try:
                last_failure = float(cooldown_path.read_text().strip())
            except ValueError:
                last_failure = 0
            if time.time() - last_failure < 300:
                print("Full deployment retry is cooling down; the next minute check will retry later.", flush=True)
                return 1
        token_values = parse_env(ROOT / ".hf.env")
        token = token_values.get("HF_TOKEN", "")
        if not token:
            raw = (ROOT / ".hf.env").read_text().strip()
            token = raw.split("=", 1)[1].strip().strip("\"'") if raw.startswith("HF_TOKEN=") else raw
        if not token:
            print("HF token is unavailable; deployment cannot continue.", flush=True)
            return 1
        # A private per-run file lets the existing deployer stage credentials without
        # credentials appearing in command arguments or logs.
        fd, temporary = tempfile.mkstemp(prefix="resumelens-hf-", dir=state_dir)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
        try:
            if deploy(values, Path(temporary)):
                try:
                    cooldown_path.unlink()
                except FileNotFoundError:
                    pass
                print("ResumeLens VM deployment completed.", flush=True)
                return 0
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"ResumeLens deployment failed ({type(error).__name__}).", flush=True)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        atomic_write(cooldown_path, str(time.time()) + "\n")
        return 1


def monitor() -> int:
    try:
        values = scheduler_values()
    except Exception as error:
        print(f"ResumeLens scheduler configuration unavailable ({type(error).__name__}).", flush=True)
        return 1
    status, detail = inspect_vm(values)
    if status == "healthy":
        return 0
    state_dir = Path.home() / ".local/state/resumelens-recovery"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = ROOT / "logs/resumelens-recovery.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "recover"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    print(f"ResumeLens VM is {status}; started recovery worker." + (f" {detail[:200]}" if detail else ""), flush=True)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init_command = commands.add_parser("init", help="configure integrations, cron, and initial VM deployment")
    group = init_command.add_mutually_exclusive_group()
    group.add_argument("--test", action="store_true", help="test integrations without changing cron or deploying")
    group.add_argument("--dry-run", action="store_true", help="preview cron changes without prompts or network calls")
    commands.add_parser("monitor", help="short once-a-minute VM health check")
    commands.add_parser("recover", help="locked ResumeLens VM recovery worker")
    commands.add_parser("deploy", help="fetch local-deploy-main and force a locked VM deployment")
    args = parser.parse_args()
    try:
        if args.command == "init":
            mode = "dry-run" if args.dry_run else "test" if args.test else "install"
            return init(mode)
        if args.command == "monitor":
            return monitor()
        return recover(force=args.command == "deploy")
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
