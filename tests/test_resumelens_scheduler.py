import importlib.util
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/resumelens_scheduler.py"
spec = importlib.util.spec_from_file_location("resumelens_scheduler", SCRIPT)
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


def valid_values():
    return {
        "HEALTHCHECKS_PING_URL": "https://hc-ping.com/11111111-1111-1111-1111-111111111111",
        "REDTEAM_HEALTHCHECKS_PING_URL": "https://hc-ping.com/22222222-2222-2222-2222-222222222222",
        "VM_HEALTHCHECKS_PING_URL": "https://hc-ping.com/33333333-3333-3333-3333-333333333333",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret-token",
        "APP_SOURCE": "/home/user/cs553-case-study-1",
        "SSH_HOST": "student-admin@example.edu",
        "SSH_PORT": "23001",
        "SSH_KEY": "/home/user/group-key",
        "SSH_BOOTSTRAP_KEY": "/home/user/bootstrap-key",
        "SSH_GROUP_PUBLIC_KEY": "/home/user/group-key.pub",
        "SSH_JUMP": "",
    }


def test_configuration_validates_distinct_healthchecks_and_webhook():
    values = valid_values()
    scheduler.valid_config(values)
    values["VM_HEALTHCHECKS_PING_URL"] = values["HEALTHCHECKS_PING_URL"]
    with pytest.raises(ValueError, match="three separate"):
        scheduler.valid_config(values)
    values = valid_values()
    values["DISCORD_WEBHOOK_URL"] = "https://example.com/webhook"
    with pytest.raises(ValueError, match="Discord webhook"):
        scheduler.valid_config(values)


def test_env_update_is_atomic_and_preserves_unrelated_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("CUSTOM_SETTING=keep\nDISCORD_WEBHOOK_URL=old\nDISCORD_WEBHOOK_URL=duplicate\n# comment\n")
    scheduler.update_env(path, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/new-token"})
    parsed = scheduler.parse_env(path)
    assert parsed["CUSTOM_SETTING"] == "keep"
    assert parsed["DISCORD_WEBHOOK_URL"].endswith("new-token")
    assert path.read_text().count("DISCORD_WEBHOOK_URL=") == 1
    assert "# comment" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_prompt_collection_hides_secrets_and_allows_disabling_jump_host(tmp_path, monkeypatch):
    import builtins

    monkeypatch.setattr(scheduler, "ROOT", tmp_path)
    answers = {
        "Cowrie Healthchecks URL": "https://hc-ping.com/11111111-1111-1111-1111-111111111111",
        "Redteam Healthchecks URL": "https://hc-ping.com/22222222-2222-2222-2222-222222222222",
        "ResumeLens VM Healthchecks URL": "https://hc-ping.com/33333333-3333-3333-3333-333333333333",
        "Managed ResumeLens checkout path": str(tmp_path / "cs553-case-study-1"),
        "Application VM SSH user@host": "student-admin@example.edu",
        "Application VM SSH port": "23001",
        "Group SSH private key path": str(tmp_path / "group-key"),
        "Bootstrap SSH private key path": str(tmp_path / "bootstrap-key"),
        "Group SSH public key path": str(tmp_path / "group-key.pub"),
        "Optional SSH jump host": "-",
    }
    secret_answers = {
        "Discord webhook URL": "https://discord.com/api/webhooks/123/private-token",
        "Hugging Face token": "hf_abcdefghijklmnopqrstuvwxyz",
    }
    for name in ("group-key", "bootstrap-key", "group-key.pub"):
        key_path = tmp_path / name
        key_path.write_text("test key\n")
        key_path.chmod(0o600 if name != "group-key.pub" else 0o644)
    monkeypatch.setattr(builtins, "input", lambda prompt: next(value for label, value in answers.items() if label in prompt))
    monkeypatch.setattr(scheduler.getpass, "getpass", lambda prompt: next(value for label, value in secret_answers.items() if label in prompt))
    values, token = scheduler.gather_config()
    scheduler.validate_key_files(values)
    assert values["SSH_JUMP"] == ""
    assert token == secret_answers["Hugging Face token"]
    scheduler.update_env(tmp_path / ".env", values)
    scheduler.atomic_write(tmp_path / ".hf.env", token + "\n")
    assert "private-token" in (tmp_path / ".env").read_text()
    assert "private-token" not in (tmp_path / ".hf.env").read_text()
    assert token in (tmp_path / ".hf.env").read_text()
    assert (tmp_path / ".hf.env").stat().st_mode & 0o777 == 0o600


def test_integration_preflight_tests_all_checks_discord_and_hf_without_echoing_secrets(monkeypatch, capsys):
    import json

    calls = []
    def fake_http(url, data=None, headers=None, timeout=8):
        calls.append((url, data, headers))
        return 204
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(scheduler, "http_request", fake_http)
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", lambda request, timeout: Response())
    token = "hf_supersecret123456789"
    scheduler.test_integrations(valid_values(), token)
    assert len(calls) == 4
    embed = json.loads(calls[-1][1])
    assert embed["embeds"][0]["title"] == "ResumeLens setup test"
    assert token not in capsys.readouterr().out


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def test_source_checkout_clones_only_branch_and_fast_forwards(tmp_path, monkeypatch):
    bare = tmp_path / "cs553-case-study-1.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "local-deploy-main", str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.email", "test@example.edu")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "commit.gpgsign", "false")
    (seed / "app.py").write_text("def resource_capacity_status(): pass\ngr.Timer\ninitial\n")
    git(seed, "add", "app.py")
    git(seed, "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True)
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "local-deploy-main")
    monkeypatch.setattr(scheduler, "APP_GIT_URL", str(bare))
    managed = tmp_path / "managed"
    scheduler.ensure_app_source(managed, refresh=False)
    assert git(managed, "branch", "--show-current").stdout.strip() == "local-deploy-main"

    (seed / "app.py").write_text("def resource_capacity_status(): pass\ngr.Timer\nupdated\n")
    git(seed, "commit", "-am", "update")
    git(seed, "push", "origin", "local-deploy-main")
    assert scheduler.ensure_app_source(managed)
    assert (managed / "app.py").read_text().endswith("updated\n")

    git(managed, "config", "user.email", "test@example.edu")
    git(managed, "config", "user.name", "Test")
    git(managed, "config", "commit.gpgsign", "false")
    (managed / "app.py").write_text((managed / "app.py").read_text() + "unpublished\n")
    git(managed, "commit", "-am", "unpublished scheduler change")
    with pytest.raises(RuntimeError, match="only the GitHub branch"):
        scheduler.ensure_app_source(managed)

    (managed / "app.py").write_text("local edit\n")
    with pytest.raises(RuntimeError, match="local changes"):
        scheduler.ensure_app_source(managed)


def test_source_uses_clean_existing_checkout_when_fetch_is_temporarily_unavailable(tmp_path, monkeypatch):
    bare = tmp_path / "cs553-case-study-1.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "local-deploy-main", str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.email", "test@example.edu")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "commit.gpgsign", "false")
    (seed / "app.py").write_text("def resource_capacity_status(): pass\ngr.Timer\nstable\n")
    git(seed, "add", "app.py")
    git(seed, "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True)
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "local-deploy-main")
    monkeypatch.setattr(scheduler, "APP_GIT_URL", str(bare))
    managed = tmp_path / "managed"
    scheduler.ensure_app_source(managed, refresh=False)
    original_run = scheduler.subprocess.run
    def fail_fetch(args, **kwargs):
        if Path(args[0]).name == "git" and "fetch" in args:
            return subprocess.CompletedProcess(args, 128, "", "network unavailable")
        return original_run(args, **kwargs)
    monkeypatch.setattr(scheduler.subprocess, "run", fail_fetch)
    messages = []
    assert scheduler.ensure_app_source(managed, log=messages.append) is False
    assert "may be behind" in messages[0]


def test_direct_ssh_is_default_and_host_key_store_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.Path, "home", lambda: tmp_path)
    args = scheduler.ssh_base(valid_values())
    assert "-J" not in args
    assert "StrictHostKeyChecking=accept-new" in args
    assert f"UserKnownHostsFile={tmp_path}/.local/state/resumelens-recovery/known_hosts" in args
    assert "GlobalKnownHostsFile=/dev/null" in args


def test_cron_has_no_credentials_and_uses_once_per_minute_schedule():
    line = scheduler.desired_crontab()
    assert line.startswith("* * * * *")
    assert "resumelens_scheduler.py\" monitor" in line
    assert "DISCORD" not in line and "HEALTHCHECKS" not in line and "HF_TOKEN" not in line
