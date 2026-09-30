#!/usr/bin/env python3
"""Three-scheduler Cowrie and red-team coordinator.

The shared directory must be a real filesystem with cross-host POSIX locks.
Replicated local directories are intentionally rejected by the preflight.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib import error, request
from zoneinfo import ZoneInfo


NODES = ("01", "02", "03")
STANDBY = "04"
DEFAULT_HOSTS = {
    "01": "ccc-app-p-u60",
    "02": "ccc-app-p-u61",
    "03": "ccc-app-p-u62",
    "04": "ccc-app-p-u63",
}
ROOT = Path(__file__).resolve().parents[1]
PERSISTENT = ROOT / "cowrie" / "persistent.sh"
ENV_FILE = ROOT / ".env"
TIMEZONE = ZoneInfo("America/New_York")
SCAN_START = datetime(2026, 9, 29, 12, tzinfo=TIMEZONE).timestamp()
SCAN_END = datetime(2026, 10, 1, 12, tzinfo=TIMEZONE).timestamp()
ROUTE_INTERVAL = 2700
SCAN_INTERVAL = 2700
# Include one default NFSv3 directory-cache window, allowed clock skew, and a
# deep-check round trip. Verdicts still inspect only the current and prior slot.
VOTE_TTL = 150
SOLO_AFTER = 180
SHARD_GRACE = 300
THIRD_GRACE = 480
REPAIR_COOLDOWN = 300
# NFSv3 clients commonly cache directory attributes and negative lookups for
# up to 60 seconds. Leave margin for those defaults during the preflight; the
# lock-exclusion check still has to pass on every active scheduler.
NFS_LOOKUP_GRACE = 75
PREFLIGHT_READY_TIMEOUT = 150
PREFLIGHT_READY_MAX_AGE = 180
PREFLIGHT_CLOCK_SKEW = 45
SSH_HOST = "paffenroth-23.dyn.wpi.edu"
HC_RE = re.compile(r"https://hc-ping\.com/[0-9a-fA-F-]{36}$")
DISCORD_RE = re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+$")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,48}$")
SOURCE_FILES = (
    Path(__file__).resolve(),
    PERSISTENT,
    ROOT / "cowrie/deploy.sh",
    ROOT / "cowrie/delay_proxy.py",
    ROOT / "scripts/ssh_key_access.sh",
)
source_hasher = hashlib.sha256()
for source_file in SOURCE_FILES:
    source_hasher.update(source_file.relative_to(ROOT).as_posix().encode())
    source_hasher.update(source_file.read_bytes())
SOURCE_DIGEST = source_hasher.hexdigest()


def fail(message):
    raise RuntimeError(message)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        if path.is_symlink():
            fail(f"refusing symlink: {path}")
        with path.open() as source:
            return json.load(source)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        fail(f"cannot read shared state {path}: {exc}")


@contextmanager
def lock(path, wait=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail(f"refusing symlink lock: {path}")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail(f"shared lock unavailable: {path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Shared:
    def __init__(self, directory):
        self.root = Path(directory)
        if not self.root.is_absolute() or not self.root.is_dir() or self.root.is_symlink():
            fail("--shared-dir must be an existing absolute, non-symlink directory")
        if not os.access(self.root, os.R_OK | os.W_OK | os.X_OK):
            fail(f"shared directory is not writable: {self.root}")
        stat = self.root.stat()
        self.identity = (stat.st_dev, stat.st_ino)
        self.lock_path = self.root / "cluster.lock"

    def check_identity(self):
        stat = self.root.stat()
        if (stat.st_dev, stat.st_ino) != self.identity:
            fail("shared filesystem identity changed; coordination is paused")

    def path(self, *parts):
        return self.root.joinpath(*parts)

    def get(self, *parts, default=None):
        self.check_identity()
        return read_json(self.path(*parts), default)

    def put(self, *parts, value):
        self.check_identity()
        atomic_json(self.path(*parts), value)

    @contextmanager
    def locked(self):
        self.check_identity()
        with lock(self.lock_path, wait=45):
            self.check_identity()
            yield


def wait_until(predicate, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    fail(f"timed out waiting for {description}")


def hostname():
    return os.environ.get("SHARDED_TEST_HOSTNAME", socket.gethostname()).split(".")[0].lower()


def roster(shared, generation):
    row = shared.get("rosters", f"{generation}.json")
    if not row or row.get("generation") != generation:
        fail(f"generation {generation} has no physical-host roster")
    members = row.get("members", {})
    if set(members) != set(NODES + (STANDBY,)):
        fail("roster must map logical slots 01, 02, 03 and standby 04")
    if len(set(members.values())) != 4:
        fail("roster contains duplicate physical hosts")
    return row


def register_roster(shared, generation, members):
    normalized = {key: value.split(".")[0].lower() for key, value in members.items()}
    if set(normalized) != set(NODES + (STANDBY,)) or len(set(normalized.values())) != 4:
        fail("provide four distinct physical hosts for slots 01-04")
    with shared.locked():
        if shared.get("rosters", f"{generation}.json"):
            fail("generation roster already exists; use a new generation")
        shared.put("rosters", f"{generation}.json",
                   value={"generation": generation, "members": normalized,
                          "time": time.time()})
    print(f"Registered physical-host roster for {generation}.")


def require_host(shared, node, generation):
    expected = roster(shared, generation)["members"][node]
    if hostname() != expected:
        fail(f"host {hostname()} cannot claim slot {node}; expected {expected}")


def preflight(shared, node, generation):
    """Three concurrent processes prove visibility and exclusion both ways."""
    require_host(shared, node, generation)
    expected_hosts = roster(shared, generation)["members"]
    base = shared.path("preflight", generation)
    base.mkdir(parents=True, exist_ok=True)
    now = time.time()
    token = os.urandom(12).hex()
    atomic_json(base / f"ready-{node}.json",
                {"node": node, "host": hostname(), "digest": SOURCE_DIGEST,
                 "time": now, "token": token})

    def ready():
        rows = [read_json(base / f"ready-{peer}.json") for peer in NODES]
        if not all(rows):
            return None
        if any(row.get("digest") != SOURCE_DIGEST for row in rows):
            fail("the three schedulers do not have identical sharded code")
        if any(row.get("host") != expected_hosts[peer]
               for row, peer in zip(rows, NODES)):
            fail("a scheduler claimed the wrong physical host")
        stamps = [row.get("time", 0) for row in rows]
        if max(stamps) - min(stamps) > PREFLIGHT_CLOCK_SKEW:
            fail("preflight participants have excessive clock skew")
        if time.time() - min(stamps) > PREFLIGHT_READY_MAX_AGE:
            fail("a stale preflight participant was found; use a new --generation")
        return rows

    ready_rows = wait_until(ready, PREFLIGHT_READY_TIMEOUT,
                            "all three fresh preflight participants")
    ready_tokens = {row["node"]: row["token"] for row in ready_rows}
    test_lock = base / "cross-host.lock"
    for holder in NODES:
        if holder != NODES[0]:
            previous = NODES[NODES.index(holder) - 1]
            done = wait_until(lambda: read_json(base / f"done-{previous}.json"),
                              NFS_LOOKUP_GRACE, f"lock round {previous}")
            if not done.get("passed"):
                fail(f"lock round {previous} failed")

        challenge_file = base / f"challenge-{holder}.json"
        if node == holder:
            with lock(test_lock, wait=5):
                challenge = {"token": token, "holder": holder, "time": time.time()}
                atomic_json(challenge_file, challenge)

                def responses():
                    rows = [read_json(base / f"response-{holder}-{peer}.json")
                            for peer in NODES if peer != holder]
                    if all(rows):
                        return rows
                    return None

                failure = None
                try:
                    rows = wait_until(responses, NFS_LOOKUP_GRACE,
                                      f"two lock observers for {holder}")
                    passed = all(row.get("token") == token and
                                 row.get("result") == "blocked" for row in rows)
                    if not passed:
                        failure = "an observer acquired the lock or returned a stale response"
                except RuntimeError as exc:
                    passed = False
                    failure = str(exc)
                atomic_json(base / f"done-{holder}.json",
                            {"passed": passed, "time": time.time(),
                             "error": failure})
            if not passed:
                fail(f"cross-host lock round {holder} failed: {failure}")
        else:
            challenge = wait_until(lambda: read_json(challenge_file), NFS_LOOKUP_GRACE,
                                   f"lock challenge {holder}")
            age = time.time() - challenge.get("time", 0)
            if (challenge.get("holder") != holder or
                    challenge.get("token") != ready_tokens[holder] or
                    age < -PREFLIGHT_CLOCK_SKEW or
                    age > NFS_LOOKUP_GRACE + PREFLIGHT_CLOCK_SKEW):
                fail(f"clock skew or stale lock challenge from {holder}")
            fd = os.open(test_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = "acquired"
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    result = "blocked"
            finally:
                os.close(fd)
            atomic_json(base / f"response-{holder}-{node}.json",
                        {"token": challenge["token"], "result": result})
            if result != "blocked":
                fail("cross-host file locks are not shared; file replication is insufficient")
            done = wait_until(lambda: read_json(base / f"done-{holder}.json"),
                              NFS_LOOKUP_GRACE,
                              f"completed lock round {holder}")
            if not done.get("passed"):
                fail(done.get("error") or
                     f"cross-host lock round {holder} was not excluded")

    if node == NODES[-1]:
        shared.put("preflight", generation, "proof.json",
                   value={"generation": generation, "digest": SOURCE_DIGEST,
                          "time": time.time(), "nodes": list(NODES),
                          "roster": expected_hosts})
    wait_until(lambda: shared.get("preflight", generation, "proof.json"),
               NFS_LOOKUP_GRACE, "preflight proof")
    print(f"Preflight passed on scheduler {node}; generation {generation}.")


def proof(shared, generation):
    value = shared.get("preflight", generation, "proof.json")
    if (not value or value.get("digest") != SOURCE_DIGEST or
            value.get("nodes") != list(NODES) or
            value.get("roster") != roster(shared, generation)["members"]):
        fail("a successful three-node preflight is required")
    if abs(time.time() - value.get("time", 0)) > 900:
        fail("preflight is older than 15 minutes; use a new generation")
    return value


def local_state(node):
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = base / "cowrie-sharded" / node
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def mount_identity(shared):
    stat = shared.root.stat()
    identity = {"path": str(shared.root), "device": stat.st_dev, "inode": stat.st_ino}
    if sys.platform == "linux":
        result = subprocess.run(
            ["findmnt", "-T", str(shared.root), "-n", "-o", "SOURCE,FSTYPE,TARGET", "-P"],
            text=True, capture_output=True)
        if result.returncode or not result.stdout.strip():
            fail("cannot identify the shared filesystem mount")
        identity["mount"] = result.stdout.strip()
    return identity


def require_mount(shared, node, generation):
    expected = read_json(local_state(node) / "mount.json")
    if (not expected or expected.get("generation") != generation or
            any(expected.get(key) != value for key, value in mount_identity(shared).items())):
        fail("shared mount identity changed or is unconfigured; sharded work is paused")


def persistent_env(node):
    env = os.environ.copy()
    env["COWRIE_PERSISTENT_STATE_DIR"] = str(local_state(node) / "persistent")
    return env


def config():
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file():
        fail(f"missing private notification configuration: {ENV_FILE}")
    if ENV_FILE.stat().st_mode & 0o077:
        fail(f"notification configuration must have mode 600: {ENV_FILE}")
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if not DISCORD_RE.fullmatch(values.get("DISCORD_WEBHOOK_URL", "")):
        fail("DISCORD_WEBHOOK_URL is missing or invalid")
    for key in ("HEALTHCHECKS_PING_URL", "REDTEAM_HEALTHCHECKS_PING_URL"):
        if not HC_RE.fullmatch(values.get(key, "")):
            fail(f"{key} is missing or invalid")
    return values


def check_keys():
    directory = Path.home() / ".ssh/mlops"
    for name in ("id_ed25519_group_key", "id_ed25519_group_key.pub", "student-admin_key"):
        path = directory / name
        if not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
            fail(f"missing local SSH key: {path}")


def check_commands():
    for command in ("ssh", "ssh-keygen", "crontab", "findmnt"):
        if not shutil.which(command):
            fail(f"missing scheduler command: {command}")
    for command in ("/usr/bin/python3", "/usr/bin/timeout"):
        if not Path(command).is_file():
            fail(f"missing scheduler command: {command}")


def current_crontab():
    result = subprocess.run(["crontab", "-l"], text=True, capture_output=True)
    if result.returncode == 0:
        return result.stdout
    if "no crontab" in result.stderr.lower():
        return ""
    fail(f"could not read crontab: {result.stderr.strip()}")


def install_crontab(content):
    result = subprocess.run(["crontab", "-"], input=content, text=True, capture_output=True)
    if result.returncode:
        fail(f"could not install crontab: {result.stderr.strip()}")
    if current_crontab() != content:
        fail("installed crontab differs from requested schedule")


def cron_lines(node, shared_dir, generation):
    script = str(Path(__file__).resolve())
    args = (f"--node-id {node} --shared-dir {shlex.quote(str(shared_dir))} "
            f"--generation {generation}")
    logdir = local_state(node)
    return (
        f'* * * * * /usr/bin/timeout --kill-after=1s 180s /usr/bin/python3 "{script}" '
        f'monitor {args} >> "{logdir}/monitor.log" 2>&1 # cowrie-sharded-monitor\n'
        f'* * * * * /usr/bin/timeout --kill-after=1s 240s /usr/bin/python3 "{script}" '
        f'scan {args} >> "{logdir}/scan.log" 2>&1 # redteam-sharded-scan\n'
    )


def prepare(shared, node, generation, dry_run=False):
    proof(shared, generation)
    require_host(shared, node, generation)
    config()
    check_keys()
    check_commands()
    current = current_crontab()
    retained = "\n".join(
        line for line in current.splitlines()
        if "# cowrie-sharded-monitor" not in line and "# redteam-sharded-scan" not in line)
    if retained:
        retained += "\n"
    proposed = retained + cron_lines(node, shared.root, generation)
    if dry_run:
        print(proposed, end="")
        return
    install_crontab(proposed)
    identity = mount_identity(shared)
    identity["generation"] = generation
    atomic_json(local_state(node) / "mount.json", identity)
    # The new master has a separate socket and never repairs the target here.
    run_persistent(node, "--transport-reconnect", timeout=30)
    transport_ready = run_persistent(node, "--transport-check", timeout=12).returncode == 0
    shared.put("prepared", f"{node}.json",
               value={"generation": generation, "digest": SOURCE_DIGEST,
                      "host": hostname(), "transport_ready": transport_ready,
                      "time": time.time()})
    print(f"Prepared scheduler {node}; transport {'ready' if transport_ready else 'retrying'}; "
          "jobs remain dormant until activation.")


def activate(shared, node, generation):
    proof(shared, generation)
    require_host(shared, node, generation)
    require_mount(shared, node, generation)
    with shared.locked():
        expected_hosts = roster(shared, generation)["members"]
        ready_transports = 0
        for peer in NODES:
            row = shared.get("prepared", f"{peer}.json")
            if (not row or row.get("generation") != generation or
                    row.get("digest") != SOURCE_DIGEST or
                    row.get("host") != expected_hosts[peer] or
                    time.time() - row.get("time", 0) > 900):
                fail(f"scheduler {peer} has not prepared this generation in the last 15 minutes")
            ready_transports += bool(row.get("transport_ready"))
        if ready_transports < 2:
            fail("at least two existing SSH masters are required before activation")
        for peer in NODES:
            row = shared.get("cutover", generation, f"{peer}.json")
            if (not row or row.get("host") != expected_hosts[peer] or
                    time.time() - row.get("time", 0) > 900):
                fail(f"host {peer} has not confirmed legacy cron retirement")
        shared.put("active.json", value={"generation": generation,
                   "digest": SOURCE_DIGEST, "roster": expected_hosts,
                   "active": True, "time": time.time()})
        shared.put("target-status.json", value={"generation": generation})
        shared.put("route-status.json", value={"generation": generation})
    print(f"Activated three-scheduler generation {generation}.")


def is_legacy_job(line):
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or str(ROOT) not in line:
        return False
    return any(name in line for name in (
        "redteam_scan_flag.sh", "cowrie/monitor.sh", "cowrie/persistent.sh",
        "cowrie/reconcile.sh", "ssh_key_access.sh",
    ))


def cutover(shared, node, generation):
    require_host(shared, node, generation)
    proof(shared, generation)
    if node in NODES:
        require_mount(shared, node, generation)
    current = current_crontab()
    if node in NODES and (
            "# cowrie-sharded-monitor" not in current or
            "# redteam-sharded-scan" not in current):
        fail("this active scheduler has not staged its sharded cron jobs")
    snapshot = local_state(node) / f"rollback-{generation}.json"
    prior = read_json(snapshot)
    if prior:
        if prior.get("host") != hostname():
            fail("rollback snapshot belongs to another host")
    else:
        atomic_json(snapshot, {"cron": current, "host": hostname(),
                               "persistent": "# cowrie-persistent-monitor" in current})
    retained = "\n".join(line for line in current.splitlines() if not is_legacy_job(line))
    if retained:
        retained += "\n"
    if retained != current:
        install_crontab(retained)
    if "# cowrie-persistent-monitor" in current:
        stopped = subprocess.run(["/bin/bash", str(PERSISTENT), "--stop-keep-state"],
                                 capture_output=True, text=True)
        if stopped.returncode:
            install_crontab(current)
            fail(f"legacy persistent master did not stop: {stopped.stderr.strip()}")
    shared.put("cutover", generation, f"{node}.json",
               value={"host": hostname(), "time": time.time()})
    print(f"Host {hostname()} confirmed legacy cron retirement for {generation}.")


def rollback(shared, node, generation):
    require_host(shared, node, generation)
    current_active = shared.get("active.json", default={})
    if current_active.get("active") and current_active.get("generation") == generation:
        fail("deactivate this generation before restoring legacy cron")
    snapshot = read_json(local_state(node) / f"rollback-{generation}.json")
    if not snapshot or snapshot.get("host") != hostname():
        fail("no matching local rollback snapshot")
    install_crontab(snapshot["cron"])
    if snapshot.get("persistent"):
        result = subprocess.run(["/bin/bash", str(PERSISTENT), "--initialize"],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            fail("legacy cron restored, but its persistent SSH master did not initialize")
    print(f"Restored pre-cutover cron on {hostname()}.")


def deactivate(shared):
    with shared.locked():
        active = shared.get("active.json")
        if active:
            active["active"] = False
            active["time"] = time.time()
            shared.put("active.json", value=active)
    print("Sharded jobs are deactivated on all schedulers.")


def remove(node):
    current = current_crontab()
    retained = "\n".join(
        line for line in current.splitlines()
        if "# cowrie-sharded-monitor" not in line and "# redteam-sharded-scan" not in line)
    if retained:
        retained += "\n"
    if retained != current:
        install_crontab(retained)
    result = subprocess.run(["/bin/bash", str(PERSISTENT), "--stop-keep-state"],
                            env=persistent_env(node), capture_output=True, text=True)
    if result.returncode:
        fail(f"could not stop scheduler {node} persistent connection: {result.stderr}")
    print(f"Removed sharded jobs and stopped the connection on scheduler {node}.")


def active(shared, generation):
    row = shared.get("active.json")
    return bool(row and row.get("active") and row.get("generation") == generation
                and row.get("digest") == SOURCE_DIGEST
                and row.get("roster") == roster(shared, generation)["members"])


def http(url, payload=None):
    try:
        data = None if payload is None else json.dumps({"content": payload}).encode()
        headers = {} if payload is None else {"Content-Type": "application/json"}
        req = request.Request(url, data=data, headers=headers)
        with request.urlopen(req, timeout=7) as response:
            return 200 <= response.status < 300
    except (error.URLError, TimeoutError, OSError):
        return False


def notify(cfg, message):
    return http(cfg["DISCORD_WEBHOOK_URL"], message)


def ping(cfg, key, suffix=""):
    return http(cfg[key] + suffix)


def run_persistent(node, mode, timeout=30):
    try:
        return subprocess.run(["/bin/bash", str(PERSISTENT), mode],
                              env=persistent_env(node), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 124, "", "persistent command timed out")


def write_vote(shared, slot, node, vote):
    generation = shared.get("active.json", default={}).get("generation")
    vote.update({"node": node, "time": time.time(), "digest": SOURCE_DIGEST,
                 "generation": generation})
    shared.put("votes", str(slot), f"{node}.json", value=vote)


def votes(shared, slot):
    result = {}
    generation = shared.get("active.json", default={}).get("generation")
    for node in NODES:
        row = shared.get("votes", str(slot), f"{node}.json")
        if (row and row.get("digest") == SOURCE_DIGEST and
                row.get("generation") == generation and
                abs(time.time() - row.get("time", 0)) <= VOTE_TTL):
            result[node] = row
    return result


def quick_check(node):
    result = run_persistent(node, "--transport-check", timeout=12)
    if result.returncode != 0:
        # This mode reconnects transport only; it cannot change node 24.
        subprocess.Popen(["/bin/bash", str(PERSISTENT), "--transport-reconnect"],
                         env=persistent_env(node), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return "healthy" if result.returncode == 0 else (
        "local_error" if result.returncode == 4 else "failed")


def deep_check(node):
    result = run_persistent(node, "--check", timeout=27)
    return {0: "healthy", 1: "deployment", 2: "transport",
            3: "authorized_keys", 4: "local_error"}.get(result.returncode, "transport")


def vote_verdict(rows, require_two_healthy=False):
    quick_bad = sum(row.get("quick") == "failed" for row in rows.values())
    quick_good = sum(row.get("quick") == "healthy" for row in rows.values())
    deep_bad = sum(row.get("deep") in ("deployment", "authorized_keys")
                   for row in rows.values())
    deep_good = sum(row.get("deep") == "healthy" for row in rows.values())
    if quick_bad >= 2:
        return "failed", "management SSH"
    if deep_bad >= 2:
        return "failed", "Cowrie deployment"
    if quick_good >= 2 and deep_good >= (2 if require_two_healthy else 1):
        return "healthy", "Cowrie deployment"
    return "unknown", "insufficient matching votes"


def solo_allowed(shared, node, now):
    active_row = shared.get("active.json", default={})
    origin = active_row.get("time", now)
    for peer in NODES:
        if peer == node:
            continue
        heartbeat = shared.get("nodes", f"{peer}.json", default={})
        if heartbeat.get("generation") != active_row.get("generation"):
            heartbeat = {}
        if now - heartbeat.get("time", origin) < SOLO_AFTER:
            return False
    return True


def resolved_verdict(shared, slot, node, now, announced_down=False):
    rows = votes(shared, slot)
    verdict, reason = vote_verdict(rows, require_two_healthy=announced_down)
    if verdict != "unknown":
        return verdict, reason
    if not solo_allowed(shared, node, now):
        return verdict, reason
    current = rows.get(node, {})
    prior = shared.get("votes", str(slot - 1), f"{node}.json", default={})
    if prior.get("generation") != shared.get("active.json", default={}).get("generation"):
        prior = {}
    if (current.get("quick") == "failed" or
            current.get("deep") in ("deployment", "authorized_keys")) and (
            prior.get("quick") == "failed" or
            prior.get("deep") in ("deployment", "authorized_keys")):
        return "failed", "three-minute single-scheduler fallback"
    return verdict, reason


def alert(cfg, state, key, is_down, down_text, up_text):
    old = state.get(key, False)
    if old == is_down:
        return False
    if notify(cfg, down_text if is_down else up_text):
        state[key] = is_down
        print(f"Discord {key} {'DOWN' if is_down else 'RECOVERED'} delivered.")
        return True
    print(f"Discord {key} delivery failed; will retry.", file=sys.stderr)
    return False


def route_cycle(now):
    if SCAN_START <= now < SCAN_END:
        return int((now - SCAN_START) // ROUTE_INTERVAL)
    return None


def probe_route():
    try:
        with socket.create_connection((SSH_HOST, 22001), timeout=3) as channel:
            channel.settimeout(1.25)
            try:
                data = channel.recv(1)
            except socket.timeout:
                return "healthy"
            return "early_banner" if data else "early_close"
    except OSError:
        return "connect_failed"


def run_route(shared, node, now):
    cycle = route_cycle(now)
    if cycle is None:
        return
    owner = NODES[cycle % 3]
    generation = shared.get("active.json", default={}).get("generation")
    existing = shared.get("route", str(cycle), f"{node}.json")
    if existing and existing.get("generation") == generation:
        return
    owner_row = shared.get("route", str(cycle), f"{owner}.json")
    if owner_row and owner_row.get("generation") != generation:
        owner_row = None
    route_state = shared.get("route-status.json", default={})
    backup = NODES[(NODES.index(owner) + 1) % 3]
    due = (node == owner or
           owner_row and owner_row.get("status") != "healthy" or
           owner_row and route_state.get("down") or
           not owner_row and node == backup and
           now - (SCAN_START + cycle * ROUTE_INTERVAL) >= 90)
    if not due:
        return
    result = probe_route()
    shared.put("route", str(cycle), f"{node}.json",
               value={"node": node, "time": time.time(), "status": result,
                      "generation": generation})
    print(f"Public 22001 route probe from {node}: {result}.")


def process_route(shared, cfg, now):
    cycle = route_cycle(now)
    if cycle is None:
        return
    rows = [shared.get("route", str(cycle), f"{node}.json")
            for node in NODES]
    generation = shared.get("active.json", default={}).get("generation")
    rows = [row for row in rows if row and row.get("generation") == generation]
    bad = sum(row.get("status") != "healthy" for row in rows)
    good = sum(row.get("status") == "healthy" for row in rows)
    state = shared.get("route-status.json", default={})
    if state.get("generation") != generation:
        state = {"generation": generation}
    old_cycle = state.get("cycle", -1)
    down = state.get("down", False)
    if cycle != old_cycle:
        if bad >= 2:
            state["streak"] = state.get("streak", 0) + 1 if old_cycle == cycle - 1 else 1
            state["cycle"] = cycle
        elif good >= (2 if down else 1):
            state["streak"] = 0
            state["cycle"] = cycle
        else:
            return
    desired = state.get("streak", 0) >= 2
    events = shared.get("alerts.json", default={})
    if desired:
        alert(cfg, events, "route", True,
              "Cowrie node 24 public port 22001 DOWN: two scheduled cycles failed from two schedulers.",
              "Cowrie node 24 public port 22001 RECOVERED.")
    elif down and good >= 2:
        alert(cfg, events, "route", False,
              "Cowrie node 24 public port 22001 DOWN.",
              "Cowrie node 24 public port 22001 RECOVERED.")
    state["down"] = events.get("route", False)
    shared.put("alerts.json", value=events)
    shared.put("route-status.json", value=state)


def process_monitor(shared, cfg, node, slot, generation):
    now = time.time()
    with shared.locked():
        if not active(shared, generation):
            return
        state = shared.get("target-status.json", default={})
        if state.get("generation") != generation:
            state = {"generation": generation}
        events = shared.get("alerts.json", default={})
        for check_slot in (slot - 1, slot):
            verdict, reason = resolved_verdict(
                shared, check_slot, node, now, events.get("target", False))
            if verdict == "unknown" or state.get("slot", -1) >= check_slot:
                continue
            previous_slot = state.get("slot", -1)
            previous_failed = state.get("last_verdict") == "failed"
            if verdict == "failed":
                state["streak"] = (state.get("streak", 0) + 1
                                   if previous_failed and previous_slot == check_slot - 1 else 1)
            else:
                state["streak"] = 0
            state.update({"slot": check_slot, "last_verdict": verdict, "reason": reason})
            print(f"Target round {check_slot}: {verdict} ({reason}).")
            if verdict == "failed" and now - state.get("last_repair", 0) >= REPAIR_COOLDOWN:
                state["last_repair"] = now
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "repair",
                                  "--node-id", node, "--shared-dir", str(shared.root),
                                  "--generation", generation],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        if state.get("streak", 0) >= 2:
            alert(cfg, events, "target", True,
                  f"Cowrie node 24 DOWN: {state.get('reason', 'target failure')}. One repairer is retrying.",
                  "Cowrie node 24 RECOVERED: SSH and Cowrie deployment checks passed.")
        elif state.get("last_verdict") == "healthy" and events.get("target"):
            alert(cfg, events, "target", False,
                  "Cowrie node 24 DOWN.",
                  "Cowrie node 24 RECOVERED: SSH and Cowrie deployment checks passed.")

        active_row = shared.get("active.json", default={})
        for peer in NODES:
            heartbeat = shared.get("nodes", f"{peer}.json", default={})
            if heartbeat.get("generation") != active_row.get("generation"):
                heartbeat = {}
            missing = now - heartbeat.get("time", active_row.get("time", now)) >= 120
            alert(cfg, events, f"coverage-{peer}", missing,
                  f"Scheduler node {peer} stopped reporting; Cowrie coverage is reduced.",
                  f"Scheduler node {peer} is reporting again.")
        current_votes = votes(shared, slot)
        quick_healthy = sum(row.get("quick") == "healthy" for row in current_votes.values())
        def bad_vote(row):
            return row.get("quick") == "failed" or row.get("deep") in (
                "deployment", "authorized_keys", "transport", "local_error")
        isolated_count = sum(bad_vote(row) for row in current_votes.values())
        for peer in NODES:
            peer_vote = current_votes.get(peer, {})
            isolated = (quick_healthy >= 2 and isolated_count == 1 and bad_vote(peer_vote))
            if isolated or (events.get(f"connection-{peer}") and
                            peer_vote.get("deep") == "healthy"):
                alert(cfg, events, f"connection-{peer}", isolated,
                      f"Scheduler node {peer} reported an isolated Cowrie check failure; "
                      "other schedulers still report the target healthy.",
                      f"Scheduler node {peer} Cowrie checks recovered.")
        shared.put("alerts.json", value=events)
        shared.put("target-status.json", value=state)
        process_route(shared, cfg, now)
        heartbeat = shared.get("heartbeat.json", default={})
        if heartbeat.get("slot") != slot and ping(cfg, "HEALTHCHECKS_PING_URL"):
            shared.put("heartbeat.json", value={"slot": slot, "time": now})


def monitor(shared, node, generation):
    require_host(shared, node, generation)
    require_mount(shared, node, generation)
    if not active(shared, generation):
        return
    local = local_state(node)
    with lock(local / "monitor.lock", wait=0):
        cfg = config()
        now = time.time()
        slot = int(now // 60)
        quick = quick_check(node)
        vote = {"quick": quick, "deep": None}
        shared.put("nodes", f"{node}.json",
                   value={"time": time.time(), "node": node, "generation": generation})
        write_vote(shared, slot, node, vote.copy())
        owner = NODES[slot % 3]
        if quick == "healthy" and node == owner:
            vote["deep"] = deep_check(node)
            write_vote(shared, slot, node, vote.copy())
        elif quick == "healthy":
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                owner_vote = shared.get("votes", str(slot), f"{owner}.json", default={})
                if owner_vote.get("deep") or owner_vote.get("quick") == "failed":
                    break
                time.sleep(0.25)
            owner_vote = shared.get("votes", str(slot), f"{owner}.json", default={})
            target_down = shared.get("alerts.json", default={}).get("target", False)
            backup = NODES[(NODES.index(owner) + 1) % 3]
            should_confirm = owner_vote.get("deep") in (
                "deployment", "authorized_keys", "transport", "local_error")
            should_recover = target_down and owner_vote.get("deep") == "healthy"
            should_backup = not owner_vote.get("deep") and node == backup
            if should_confirm or should_recover or should_backup:
                vote["deep"] = deep_check(node)
                write_vote(shared, slot, node, vote.copy())
        run_route(shared, node, time.time())
        process_monitor(shared, cfg, node, slot, generation)


def repair(shared, node, generation):
    require_host(shared, node, generation)
    require_mount(shared, node, generation)
    if not active(shared, generation):
        return
    with lock(shared.path("repair.lock"), wait=0):
        with shared.locked():
            if not active(shared, generation):
                return
            state = shared.get("target-status.json", default={})
            if state.get("last_verdict") != "failed" or time.time() - state.get("last_repair", 0) > 90:
                return
        require_mount(shared, node, generation)
        result = run_persistent(node, "--recover", timeout=240)
        print(f"Scheduler {node} repair exited {result.returncode}: {result.stderr[-200:]}")


def scan_window(now):
    return SCAN_START <= now < SCAN_END


def start_scan_cycle(shared, cfg, generation, now):
    current = shared.get("scan-current.json")
    if (scan_window(now) and
            (not current or current.get("generation") != generation or
             now - current.get("start", 0) >= SCAN_INTERVAL)):
        cycle = int(now // 900)
        current = {"id": cycle, "start": now, "generation": generation}
        shared.put("scan-current.json", value=current)
    if current and current.get("generation") == generation and not current.get("started"):
        if ping(cfg, "REDTEAM_HEALTHCHECKS_PING_URL", "/start"):
            current["started"] = True
            shared.put("scan-current.json", value=current)
    return current


def scan_ports(owner):
    first = {"01": 2, "02": 10, "03": 18}[owner]
    ports = [22000 + node for node in range(first, first + 8)]
    random.shuffle(ports)
    key = Path.home() / ".ssh/mlops/student-admin_key"
    if not key.is_file() or key.stat().st_size == 0:
        fail("red-team student-admin key is unavailable")
    results = []
    for index, port in enumerate(ports):
        if not scan_window(time.time()):
            fail("red-team scan window closed during this shard")
        cmd = ["ssh", "-T", "-F", "/dev/null", "-i", str(key), "-p", str(port),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "GlobalKnownHostsFile=/dev/null",
               f"student-admin@{SSH_HOST}", "true"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=9)
            ok = result.returncode == 0
            detail = result.stderr.decode(errors="replace")[-180:]
        except subprocess.TimeoutExpired:
            ok = False
            detail = "SSH timeout"
        results.append({"node": port - 22000, "port": port, "ok": ok,
                        "detail": detail if not ok else ""})
        if index < len(ports) - 1:
            time.sleep(random.randint(1, 3))
    return results


def scan_shard(shared, node, generation, current, owner):
    cycle = current["id"]
    existing = shared.get("shards", str(cycle), f"{owner}.json")
    if existing and existing.get("generation") == generation:
        return
    now = time.time()
    owner_index = NODES.index(owner)
    first_backup = NODES[(owner_index + 1) % 3]
    second_backup = NODES[(owner_index + 2) % 3]
    eligible = (node == owner or
                node == first_backup and now - current["start"] >= SHARD_GRACE or
                node == second_backup and now - current["start"] >= THIRD_GRACE)
    if not eligible or not scan_window(now):
        return
    try:
        with lock(shared.path("shards", str(cycle), f"{owner}.lock"), wait=0):
            current_shard = shared.get("shards", str(cycle), f"{owner}.json")
            if current_shard and current_shard.get("generation") == generation:
                return
            if not active(shared, generation):
                return
            require_mount(shared, node, generation)
            results = scan_ports(owner)
            shared.put("shards", str(cycle), f"{owner}.json",
                       value={"owner": owner, "runner": node, "time": time.time(),
                              "generation": generation,
                              "results": results})
            print(f"Scheduler {node} completed red-team shard {owner} for cycle {cycle}.")
    except RuntimeError as exc:
        if "shared lock unavailable" not in str(exc):
            raise


TIMELINE = (
    (datetime(2026, 9, 29, 13, tzinfo=TIMEZONE), "1 PM check-in on September 29"),
    (datetime(2026, 9, 29, 19, tzinfo=TIMEZONE), "7 PM check-in on September 29"),
    (datetime(2026, 9, 30, 10, tzinfo=TIMEZONE), "10 AM check-in on September 30"),
    (datetime(2026, 10, 1, 11, tzinfo=TIMEZONE),
     "1 hour before the October 1 noon scan deadline"),
    (datetime(2026, 10, 1, 12, tzinfo=TIMEZONE), "October 1 noon scan deadline"),
)


def process_scan(shared, cfg, generation):
    now = time.time()
    with shared.locked():
        if not active(shared, generation):
            return
        current = start_scan_cycle(shared, cfg, generation, now)
        if current and current.get("generation") == generation:
            cycle = current["id"]
            shards = [shared.get("shards", str(cycle), f"{owner}.json")
                      for owner in NODES]
            shards = [row if row and row.get("generation") == generation else None
                      for row in shards]
            all_done = all(shards)
            if all_done:
                results = [entry for shard in shards for entry in shard["results"]]
                successes = sorted(entry["node"] for entry in results if entry["ok"])
                latest = shared.get("latest-scan.json", default={})
                if latest.get("cycle") != cycle:
                    shared.put("latest-scan.json",
                               value={"cycle": cycle, "completed": now,
                                      "successes": successes, "checked": len(results)})
            if not current.get("finished"):
                if all_done:
                    if ping(cfg, "REDTEAM_HEALTHCHECKS_PING_URL"):
                        current["finished"] = "complete"
                elif now - current["start"] >= 600 or now >= SCAN_END:
                    if ping(cfg, "REDTEAM_HEALTHCHECKS_PING_URL", "/fail"):
                        current["finished"] = "incomplete"
                shared.put("scan-current.json", value=current)

        sent = shared.get("timeline.json", default={})
        latest = shared.get("latest-scan.json", default={})
        for when, label in TIMELINE:
            event_id = when.strftime("%Y%m%d%H%M")
            age = now - when.timestamp()
            if not 0 <= age <= 600 or sent.get(event_id):
                continue
            if latest:
                summary = (f"{len(latest['successes'])}/{latest['checked']} ports authenticated "
                           f"(nodes: {', '.join(map(str, latest['successes'])) or 'none'}).")
            else:
                summary = "no complete 24-port scan is available."
            if notify(cfg, f"Redteam timeline: {label}. Latest complete scan: {summary}"):
                sent[event_id] = now
                shared.put("timeline.json", value=sent)


def scan(shared, node, generation):
    require_host(shared, node, generation)
    require_mount(shared, node, generation)
    if not active(shared, generation):
        return
    with lock(local_state(node) / "scan.lock", wait=0):
        cfg = config()
        with shared.locked():
            current = start_scan_cycle(shared, cfg, generation, time.time())
        if current and current.get("generation") == generation:
            # The owner runs first. Backups use the same fixed shard and lock.
            for owner in NODES:
                scan_shard(shared, node, generation, current, owner)
        process_scan(shared, cfg, generation)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("roster", "preflight", "prepare", "cutover",
                                            "activate", "deactivate", "rollback",
                                            "remove", "monitor", "scan", "repair"), nargs="?")
    parser.add_argument("--node-id", choices=NODES + (STANDBY,))
    parser.add_argument("--shared-dir")
    parser.add_argument("--generation")
    parser.add_argument("--member", action="append", default=[], metavar="ID=HOST")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview the prepare crontab after a successful preflight")
    args = parser.parse_args()
    if not args.action:
        if args.dry_run:
            parser.error("--dry-run needs the prepare action and node context; run "
                         "./init_sharded.sh prepare --dry-run --node-id ID "
                         "--shared-dir /absolute/shared/path --generation NAME "
                         "on a scheduler after preflight")
        parser.error("an action is required (see --help)")
    if args.action not in ("remove", "roster") and not args.node_id:
        parser.error("--node-id is required")
    if args.action not in ("remove",) and not args.shared_dir:
        parser.error("--shared-dir is required")
    if args.action not in ("deactivate", "remove") and not args.generation:
        parser.error("--generation is required")
    if args.generation and not ID_RE.fullmatch(args.generation):
        parser.error("--generation must be 1-48 letters, digits, hyphens, or underscores")
    if args.action == "remove" and not args.node_id:
        parser.error("--node-id is required for remove")
    if args.dry_run and args.action != "prepare":
        parser.error("--dry-run applies only to prepare")
    if args.action in ("preflight", "prepare", "activate", "deactivate",
                       "remove", "monitor", "scan", "repair") and args.node_id == STANDBY:
        parser.error("standby slot 04 cannot vote, scan, prepare, or activate")
    if args.action == "roster" and len(args.member) != 4:
        parser.error("roster requires four --member ID=HOST options")
    return args


def main():
    args = parse_args()
    shared = Shared(args.shared_dir) if args.action != "remove" else None
    if args.action == "roster":
        members = dict(member.split("=", 1) for member in args.member)
        register_roster(shared, args.generation, members)
    elif args.action == "preflight":
        preflight(shared, args.node_id, args.generation)
    elif args.action == "prepare":
        prepare(shared, args.node_id, args.generation, args.dry_run)
    elif args.action == "cutover":
        cutover(shared, args.node_id, args.generation)
    elif args.action == "activate":
        activate(shared, args.node_id, args.generation)
    elif args.action == "deactivate":
        active_row = shared.get("active.json", default={})
        require_host(shared, args.node_id, active_row.get("generation"))
        require_mount(shared, args.node_id, active_row.get("generation"))
        deactivate(shared)
    elif args.action == "rollback":
        rollback(shared, args.node_id, args.generation)
    elif args.action == "remove":
        remove(args.node_id)
    elif args.action == "monitor":
        monitor(shared, args.node_id, args.generation)
    elif args.action == "scan":
        scan(shared, args.node_id, args.generation)
    elif args.action == "repair":
        repair(shared, args.node_id, args.generation)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
