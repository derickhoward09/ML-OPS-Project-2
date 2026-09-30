#!/usr/bin/env python3
"""Stage and activate one sharded scheduler release from a Mac.

All targets are explicit SSH destinations. The command never deletes remote files.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]
REMOTE_DEFAULT = "/home/akrett/github/ML-OPS-Project-2"
HOSTS = {"01": "ccc-app-p-u60", "02": "ccc-app-p-u61",
         "03": "ccc-app-p-u62", "04": "ccc-app-p-u63"}
ACTIVE = ("01", "02", "03")
RELEASE = (
    ".gitignore", "COWRIE_README.md", "clean.sh", "init_sharded.sh",
    "sharded/cluster.py", "cowrie/persistent.sh", "cowrie/deploy.sh",
    "cowrie/delay_proxy.py", "cowrie/init_common.sh",
    "cowrie/install_cron.sh", "scripts/ssh_key_access.sh",
    "scripts/deploy_sharded_macos.py",
)
ID = re.compile(r"[A-Za-z0-9_-]{1,48}$")


def fail(message):
    raise RuntimeError(message)


def parse_mapping(values, label):
    result = {}
    for value in values:
        if "=" not in value:
            fail(f"{label} must have ID=VALUE form")
        key, item = value.split("=", 1)
        if key not in HOSTS or not item or key in result:
            fail(f"invalid or duplicate {label}: {value}")
        result[key] = item
    return result


def manifest():
    files = {}
    for name in RELEASE:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            fail(f"missing or linked release file: {path}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def release_tar():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in RELEASE:
            archive.add(ROOT / name, arcname=name, recursive=False)
    return buffer.getvalue()


REMOTE_INSPECT = r'''
import hashlib, json, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1]); names = json.loads(sys.argv[2])
if not root.is_dir() or root.is_symlink():
    raise SystemExit('remote checkout missing or symlinked')
files = {}
for name in names:
    path = root / name
    files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
if cron.returncode and 'no crontab' not in cron.stderr.lower():
    raise SystemExit('cannot read crontab: ' + cron.stderr)
mount = subprocess.run(['findmnt', '-T', str(root), '-n', '-o', 'SOURCE,FSTYPE,TARGET', '-P'], capture_output=True, text=True)
if mount.returncode or not mount.stdout.strip():
    raise SystemExit('cannot identify checkout mount')
active_file = root / '.sharded-state' / 'active.json'
active = json.loads(active_file.read_text()) if active_file.is_file() else {}
print(json.dumps({'host': os.uname().nodename.split('.')[0].lower(), 'files': files,
                  'cron': cron.stdout, 'mount': mount.stdout.strip(), 'active': active}))
'''


class SSH:
    def __init__(self, targets):
        self.targets = targets

    def run(self, node, command, data=None, timeout=45):
        target = self.targets[node]
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                target, shlex.join(command)]
        result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        if result.returncode:
            fail(f"SSH {node} ({target}) failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout.decode()


def inspect(ssh, node, root, names):
    out = ssh.run(node, ["python3", "-c", REMOTE_INSPECT, root, json.dumps(names)], timeout=25)
    return json.loads(out)


def inspect_all(ssh, root, names, hosts, expected=None):
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(inspect, ssh, node, root, names): node for node in HOSTS}
        rows = {}
        for future in as_completed(futures):
            node = futures[future]
            rows[node] = future.result()
    for node, row in rows.items():
        if row["host"] != hosts[node].split(".")[0].lower():
            fail(f"SSH target {node} reached {row['host']}, expected {hosts[node]}")
        if expected is not None and row["files"] != expected:
            fail(f"release digest mismatch on {node} ({row['host']})")
    return rows


def controller(ssh, node, action, root, generation=None, extra=(), timeout=60):
    args = ["python3", f"{root}/sharded/cluster.py", action,
            "--shared-dir", f"{root}/.sharded-state"]
    if action != "roster":
        args += ["--node-id", node]
    if generation:
        args += ["--generation", generation]
    args += list(extra)
    return ssh.run(node, args, timeout=timeout)


def detect_legacy(rows, root):
    script_names = ("redteam_scan_flag.sh", "cowrie/monitor.sh", "cowrie/persistent.sh",
                    "cowrie/reconcile.sh", "ssh_key_access.sh")
    found = []
    for node, row in rows.items():
        if any(line.lstrip() and not line.lstrip().startswith("#") and root in line and
               any(name in line for name in script_names)
               for line in row["cron"].splitlines()):
            found.append(node)
    if len(found) > 1:
        fail(f"legacy jobs found on multiple hosts: {', '.join(sorted(found))}")
    return found[0] if found else None


def upload(ssh, root):
    # No --delete: private .env, keys, logs, Git history and .sharded-state survive.
    ssh.run("01", ["tar", "-xpf", "-", "-C", root], data=release_tar(), timeout=60)
    ssh.run("01", ["mkdir", "-p", f"{root}/.sharded-state"])


def parallel_controller(ssh, action, root, generation, nodes=ACTIVE, timeout=60):
    with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        futures = {pool.submit(controller, ssh, node, action, root, generation,
                               timeout=timeout): node for node in nodes}
        results = {}
        errors = {}
        for future in as_completed(futures):
            node = futures[future]
            try:
                results[node] = future.result()
                print(f"{action} passed on {node}.", flush=True)
            except Exception as exc:
                errors[node] = str(exc)
    if errors:
        fail(f"{action} failed: " + "; ".join(
            f"{node}: {errors[node]}" for node in sorted(errors)))
    return results


def stage(ssh, root, generation, hosts, files):
    # Reachability and host identity must pass before the first remote change.
    before = inspect_all(ssh, root, files, hosts)
    if any(row.get("active", {}).get("active") for row in before.values()):
        fail("deactivate the currently active sharded generation before staging another")
    legacy = detect_legacy(before, root)
    print(f"Connected to all four hosts; legacy cron host: {legacy or 'none'}.")
    upload(ssh, root)
    print("Release uploaded; verifying all four hosts.", flush=True)
    inspect_all(ssh, root, files, hosts, expected=files)
    members = tuple(item for node in HOSTS
                    for item in ("--member", f"{node}={hosts[node]}") )
    controller(ssh, "01", "roster", root, generation, members)
    print("Roster registered; running three-node lock preflight.", flush=True)
    parallel_controller(ssh, "preflight", root, generation, timeout=900)
    print("Preflight passed; preparing dormant schedulers.", flush=True)
    parallel_controller(ssh, "prepare", root, generation, timeout=90)
    print(f"Generation {generation} staged with dormant jobs. Legacy cron remains active.")


def activate(ssh, root, generation, hosts, files):
    rows = inspect_all(ssh, root, files, hosts, expected=files)
    legacy = detect_legacy(rows, root)
    touched = []
    try:
        # Every active host acknowledges cutover; standby is touched only if it owns legacy cron.
        for node in ACTIVE + ((legacy,) if legacy == "04" else ()):
            touched.append(node)
            controller(ssh, node, "cutover", root, generation)
        controller(ssh, "01", "activate", root, generation)
    except Exception:
        # SSH can time out after the activation write. Check the marker before
        # restoring legacy cron, so a late acknowledgement cannot leave both live.
        try:
            marker = ssh.run("01", ["python3", "-c",
                "import json,sys; print(json.load(open(sys.argv[1])).get('generation','') + ':' + str(json.load(open(sys.argv[1])).get('active',False)))",
                f"{root}/.sharded-state/active.json"], timeout=12).strip()
            if marker == f"{generation}:True":
                controller(ssh, "01", "deactivate", root)
        except Exception as deactivate_error:
            print(f"Could not confirm inactive generation before rollback: {deactivate_error}",
                  file=sys.stderr)
        # A failed activation restores the exact saved legacy crontab on each cutover host.
        for node in reversed(touched):
            try:
                controller(ssh, node, "rollback", root, generation)
            except Exception as rollback_error:
                print(f"ROLLBACK FAILED on {node}: {rollback_error}", file=sys.stderr)
        raise
    print(f"Generation {generation} active; legacy host was {legacy or 'none'}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "stage", "activate", "rollout"))
    parser.add_argument("--target", action="append", required=True, metavar="ID=SSH_TARGET",
                        help="repeat for all four slots; FQDNs, IPs and SSH aliases work")
    parser.add_argument("--host", action="append", default=[], metavar="ID=PHYSICAL_HOST")
    parser.add_argument("--remote-root", default=REMOTE_DEFAULT)
    parser.add_argument("--generation")
    args = parser.parse_args(argv)
    try:
        targets = parse_mapping(args.target, "target")
        hosts = HOSTS | parse_mapping(args.host, "host")
        if set(targets) != set(HOSTS):
            fail("four --target values, for slots 01-04, are required")
        if len({host.split(".")[0].lower() for host in hosts.values()}) != 4:
            fail("physical hosts must be unique")
        if not args.remote_root.startswith("/") or ".." in Path(args.remote_root).parts:
            fail("--remote-root must be an absolute path without '..'")
        if args.action != "check" and (not args.generation or not ID.fullmatch(args.generation)):
            fail("a unique --generation (1-48 safe characters) is required")
        ssh = SSH(targets)
        files = manifest()
        if args.action == "check":
            rows = inspect_all(ssh, args.remote_root, files, hosts)
            print(f"Connected to all four hosts; legacy cron host: {detect_legacy(rows, args.remote_root) or 'none'}.")
            for node in sorted(rows):
                print(f"{node}: {rows[node]['host']} mount {rows[node]['mount']}")
        if args.action in ("stage", "rollout"):
            stage(ssh, args.remote_root, args.generation, hosts, files)
        if args.action in ("activate", "rollout"):
            activate(ssh, args.remote_root, args.generation, hosts, files)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
