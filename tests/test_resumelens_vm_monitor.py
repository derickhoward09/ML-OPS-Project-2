import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/resumelens_vm_monitor.py"
spec = importlib.util.spec_from_file_location("resumelens_vm_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def test_cpu_counters_normalize_all_cpus_and_exclude_guest_double_count(tmp_path):
    path = tmp_path / "stat"
    path.write_text("cpu 100 10 20 200 10 5 5 0 500 20\n")
    assert monitor.read_cpu_counters(path) == (350, 210)
    assert monitor.cpu_percent((1000, 400), (1200, 480)) == pytest.approx(60.0)
    assert monitor.cpu_percent((1000, 400), (1000, 400)) == 0


def test_memory_uses_available_memory_and_excludes_swap(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 250 kB\nSwapTotal: 5000 kB\n")
    assert monitor.read_memory_percent(path) == pytest.approx(75.0)


def test_brief_spike_resets_and_strict_sustained_threshold_triggers(monkeypatch):
    monkeypatch.setattr(monitor, "HIGH_PERCENT", 80)
    monkeypatch.setattr(monitor, "HIGH_SECONDS", 5)
    tracker = monitor.ThresholdTracker()
    assert tracker.observe(81, 20, 0, 1000)[0] is False
    assert tracker.observe(80, 20, 2, 1002)[0] is False  # exactly 80 is not high
    assert tracker.high_since["CPU"] is None
    assert tracker.observe(85, 20, 3, 1003)[1] is None
    assert tracker.observe(20, 20, 7, 1007)[0] is False  # four seconds, then reset
    assert tracker.observe(85, 20, 8, 1008)[1] is None
    assert tracker.observe(85, 20, 13, 1013)[1] is None  # exactly five seconds
    active, started, _ = tracker.observe(85, 20, 13.01, 1013.01)
    assert active is True
    assert started["trigger"] == "CPU"


def test_either_resource_triggers_and_both_must_recover_for_full_duration(monkeypatch):
    monkeypatch.setattr(monitor, "HIGH_PERCENT", 80)
    monkeypatch.setattr(monitor, "HIGH_SECONDS", 5)
    monkeypatch.setattr(monitor, "RECOVERY_PERCENT", 70)
    monkeypatch.setattr(monitor, "RECOVERY_SECONDS", 10)
    tracker = monitor.ThresholdTracker()
    tracker.observe(40, 90, 0, 100)
    active, started, _ = tracker.observe(40, 90, 5.1, 105.1)
    assert active and started["trigger"] == "memory"
    assert tracker.observe(50, 70, 8, 108)[0] is True  # 70 is not recovered
    assert tracker.recovery_since is None
    tracker.observe(50, 69, 9, 109)
    assert tracker.observe(50, 69, 18.9, 118.9)[2] is None
    active, _, recovered = tracker.observe(50, 69, 19, 119)
    assert active is False
    assert recovered["duration_seconds"] == pytest.approx(13.9)


def test_discord_alert_is_an_embed_without_mentions_or_webhook_leak(monkeypatch):
    import json

    captured = {}
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data)
        captured["url"] = request.full_url
        return Response()
    monkeypatch.setattr(monitor.urllib.request, "urlopen", fake_urlopen)
    monitor.discord_embed(
        "https://discord.com/api/webhooks/123/private-token",
        "ResumeLens VM near capacity", 0xF0A22E, "Sustained use crossed threshold.",
        [{"name": "CPU", "value": "91.0%", "inline": True}],
    )
    payload = captured["payload"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["embeds"][0]["title"] == "ResumeLens VM near capacity"
    assert payload["embeds"][0]["color"] == 0xF0A22E
    assert "private-token" not in json.dumps(payload)
    assert captured["timeout"] == 8


def test_notification_worker_retries_in_background_and_persists_delivery(tmp_path, monkeypatch):
    import queue
    import threading

    monkeypatch.setattr(monitor, "WEBHOOK", "https://discord.com/api/webhooks/123/private-token")
    monkeypatch.setattr(monitor, "STATE_PATH", tmp_path / "state.json")
    attempts = []
    def fake_embed(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("temporary network failure")
    monkeypatch.setattr(monitor, "discord_embed", fake_embed)
    episode = {
        "id": "episode-1", "started_at": 10, "trigger": "CPU",
        "cpu_percent": 91, "memory_percent": 40,
    }
    state = {"active_episode": episode, "delivered": [], "pending": [{"kind": "overload", "episode": episode}]}
    events = queue.Queue()
    events.put(("overload", episode))
    events.put(None)
    worker = threading.Thread(target=monitor.notification_worker, args=(events, state, threading.Lock()))
    worker.start()
    worker.join(timeout=4)
    assert not worker.is_alive()
    assert len(attempts) == 2
    persisted = __import__("json").loads((tmp_path / "state.json").read_text())
    assert persisted["delivered"] == ["episode-1"]
    assert persisted["pending"] == []
