#!/usr/bin/env python3
"""Sample VM CPU and memory, publish app status, and notify on transitions."""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


STATUS_PATH = Path(os.getenv("RESUMELENS_STATUS_PATH", "/run/resumelens-monitor/status.json"))
STATE_PATH = Path(os.getenv("RESUMELENS_STATE_PATH", "/var/lib/resumelens-monitor/state.json"))
SAMPLE_SECONDS = float(os.getenv("RESUMELENS_SAMPLE_SECONDS", "1"))
HIGH_PERCENT = float(os.getenv("RESUMELENS_HIGH_PERCENT", "80"))
HIGH_SECONDS = float(os.getenv("RESUMELENS_HIGH_SECONDS", "5"))
RECOVERY_PERCENT = float(os.getenv("RESUMELENS_RECOVERY_PERCENT", "70"))
RECOVERY_SECONDS = float(os.getenv("RESUMELENS_RECOVERY_SECONDS", "10"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")


def read_cpu_counters(path: Path = Path("/proc/stat")) -> tuple[int, int]:
    line = path.read_text().splitlines()[0]
    fields = [int(value) for value in line.split()[1:]]
    # Linux reports guest time inside user/nice as well, so omit it from totals.
    while len(fields) < 8:
        fields.append(0)
    user, nice, system, idle, iowait, irq, softirq, steal = fields[:8]
    return user + nice + system + idle + iowait + irq + softirq + steal, idle + iowait


def read_memory_percent(path: Path = Path("/proc/meminfo")) -> float:
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(value.strip().split()[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total <= 0 or available < 0:
        raise ValueError("MemTotal or MemAvailable is unavailable")
    return max(0.0, min(100.0, (total - available) * 100.0 / total))


def cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n")
    temporary.chmod(0o644 if path == STATUS_PATH else 0o600)
    temporary.replace(path)


def discord_embed(webhook: str, title: str, color: int, description: str, fields: list[dict]) -> None:
    payload = {
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": f"ResumeLens VM • {socket.gethostname()}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    request = urllib.request.Request(
        webhook, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "ResumeLens-VM-Monitor/1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def notification_worker(notifications: queue.Queue, state: dict, state_lock: threading.Lock) -> None:
    while True:
        event = notifications.get()
        if event is None:
            notifications.task_done()
            return
        kind, episode = event
        if kind == "overload":
            title, color = "ResumeLens VM near capacity", 0xF0A22E
            description = "Sustained resource use has crossed the configured threshold."
            duration = "Overload begins when CPU or RAM exceeds 80% for more than 5 seconds."
            event_id = episode["id"]
        else:
            title, color = "ResumeLens VM recovered", 0x3BA55D
            description = "Resource use has returned below the recovery threshold."
            duration = f"Overload episode: {episode.get('duration_seconds', 0):.0f} seconds."
            event_id = episode["id"] + ":recovered"
        fields = [
            {"name": "CPU", "value": f"{episode['cpu_percent']:.1f}%", "inline": True},
            {"name": "Memory", "value": f"{episode['memory_percent']:.1f}%", "inline": True},
            {"name": "Trigger", "value": episode.get("trigger", "CPU or memory") if kind == "overload" else "CPU and memory below 70% for 10 seconds", "inline": False},
            {"name": "Threshold", "value": "Overload: >80% for >5 sec\nRecovery: both <70% for 10 sec", "inline": True},
            {"name": "Automatic action", "value": "Capacity warning displayed; reviews remain available." if kind == "overload" else "Capacity warning cleared; normal operation resumed.", "inline": False},
            {"name": "Duration", "value": duration, "inline": False},
        ]
        delay = 1
        while True:
            try:
                discord_embed(WEBHOOK, title, color, description, fields)
                with state_lock:
                    state.setdefault("delivered", []).append(event_id)
                    state["delivered"] = state["delivered"][-100:]
                    state["pending"] = [item for item in state.get("pending", []) if _event_id(item) != event_id]
                    atomic_json(STATE_PATH, state)
                break
            except urllib.error.HTTPError as error:
                try:
                    retry_after = float(error.headers.get("Retry-After", delay))
                except (TypeError, ValueError):
                    retry_after = delay
                wait = min(max(retry_after, 1), 300) if error.code == 429 else delay
                print(f"Discord notification failed (HTTP {error.code}); retrying in {wait:g}s", flush=True)
                time.sleep(wait)
                delay = min(delay * 2, 60)
            except (OSError, urllib.error.URLError, RuntimeError) as error:
                print(f"Discord notification failed ({type(error).__name__}); retrying in {delay}s", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        notifications.task_done()


def _event_id(item: dict) -> str:
    episode_id = item["episode"]["id"]
    return episode_id if item["kind"] == "overload" else episode_id + ":recovered"


class ThresholdTracker:
    """Track independent sustained breaches and one hysteretic episode."""

    def __init__(self, episode: dict | None = None):
        self.episode = episode
        self.high_since: dict[str, float | None] = {"CPU": None, "memory": None}
        self.recovery_since: float | None = None

    def observe(self, cpu: float, memory: float, now: float, wall_time: float) -> tuple[bool, dict | None, dict | None]:
        for name, value in (("CPU", cpu), ("memory", memory)):
            if value > HIGH_PERCENT:
                if self.high_since[name] is None:
                    self.high_since[name] = now
            else:
                self.high_since[name] = None
        reasons = [name for name, since in self.high_since.items() if since is not None and now - since > HIGH_SECONDS]
        started = None
        if self.episode is None and reasons:
            self.episode = {
                "id": str(time.time_ns()), "started_at": wall_time,
                "started_monotonic": now, "trigger": " and ".join(reasons),
                "cpu_percent": cpu, "memory_percent": memory,
            }
            started = dict(self.episode)

        recovered = None
        if self.episode is not None:
            if cpu < RECOVERY_PERCENT and memory < RECOVERY_PERCENT:
                if self.recovery_since is None:
                    self.recovery_since = now
                if now - self.recovery_since >= RECOVERY_SECONDS:
                    self.episode["duration_seconds"] = max(0.0, wall_time - self.episode.get("started_at", wall_time - RECOVERY_SECONDS))
                    self.episode["cpu_percent"], self.episode["memory_percent"] = cpu, memory
                    recovered = dict(self.episode)
                    self.episode = None
                    self.recovery_since = None
            else:
                self.recovery_since = None
        return self.episode is not None, started, recovered


def run() -> None:
    if SAMPLE_SECONDS <= 0 or HIGH_SECONDS < 0 or RECOVERY_SECONDS < 0:
        raise ValueError("monitor durations must be non-negative and sample interval positive")
    if not WEBHOOK:
        raise ValueError("DISCORD_WEBHOOK_URL is required")
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {"active_episode": None, "delivered": [], "pending": []}
    state.setdefault("active_episode", None)
    state.setdefault("delivered", [])
    state.setdefault("pending", [])
    state_lock = threading.Lock()
    notifications: queue.Queue = queue.Queue()
    threading.Thread(target=notification_worker, args=(notifications, state, state_lock), daemon=True).start()
    for item in state["pending"]:
        notifications.put((item["kind"], item["episode"]))
    cpu_previous = read_cpu_counters()
    episode = state.get("active_episode")
    if episode:
        episode.setdefault("id", str(int(time.time())))
        if episode["id"] not in state["delivered"] and not any(_event_id(item) == episode["id"] for item in state["pending"]):
            state["pending"].append({"kind": "overload", "episode": dict(episode)})
    tracker = ThresholdTracker(episode)
    while True:
        tick = time.monotonic()
        try:
            cpu_current = read_cpu_counters()
            cpu = cpu_percent(cpu_previous, cpu_current)
            cpu_previous = cpu_current
            memory = read_memory_percent()
        except (OSError, ValueError) as error:
            print(f"Resource sample unavailable ({type(error).__name__})", flush=True)
            time.sleep(SAMPLE_SECONDS)
            continue

        overloaded, started, recovered = tracker.observe(cpu, memory, tick, time.time())
        episode = tracker.episode

        status = {
            "timestamp": time.time(), "cpu_percent": round(cpu, 2),
            "memory_percent": round(memory, 2), "overloaded": overloaded,
            "threshold_percent": HIGH_PERCENT, "threshold_seconds": HIGH_SECONDS,
            "recovery_percent": RECOVERY_PERCENT, "recovery_seconds": RECOVERY_SECONDS,
            "trigger": episode.get("trigger") if episode else None,
        }
        atomic_json(STATUS_PATH, status)
        if started:
            state["active_episode"] = episode
            if started["id"] not in state["delivered"]:
                item = {"kind": "overload", "episode": started}
                state["pending"].append(item)
                with state_lock:
                    atomic_json(STATE_PATH, state)
                notifications.put((item["kind"], item["episode"]))
        if recovered:
            recovered_episode = recovered
            state["active_episode"] = None
            if recovered_episode["id"] + ":recovered" not in state["delivered"]:
                item = {"kind": "recovery", "episode": recovered_episode}
                state["pending"].append(item)
                with state_lock:
                    atomic_json(STATE_PATH, state)
                notifications.put((item["kind"], item["episode"]))
        with state_lock:
            atomic_json(STATE_PATH, state)
        time.sleep(max(0.0, SAMPLE_SECONDS - (time.monotonic() - tick)))


if __name__ == "__main__":
    run()
