"""Provision pinned models and configuration without exposing credentials."""
import os
import pwd
import re
import sys
from pathlib import Path
from dotenv import dotenv_values, set_key
from huggingface_hub import hf_hub_download

MODELS = [
    ('Qwen/Qwen3-0.6B-GGUF', 'Qwen3-0.6B-Q8_0.gguf', '23749fefcc72300e3a2ad315e1317431b06b590a'),
    ('LiquidAI/LFM2.5-1.2B-Instruct-GGUF', 'LFM2.5-1.2B-Instruct-QAD-Q4_0.gguf', '8ed288026e23958ad9dfa92d53ed773a8eee7125'),
]

def configure(root):
    token_path = root / '.deploy/token'
    token = token_path.read_text().strip()
    if not re.fullmatch(r'hf_[A-Za-z0-9]+', token):
        raise ValueError('Invalid credential format')
    env = root / '.env'
    env.touch(mode=0o600, exist_ok=True)
    env.chmod(0o600)
    set_key(str(env), 'HF_TOKEN', token)
    current = dotenv_values(env)
    defaults = {'LOCAL_BASE_URL':'http://127.0.0.1:8080', 'LOCAL_CONTEXT_SIZE':'4096', 'INFERENCE_TIMEOUT':'300'}
    for key,value in defaults.items():
        if key not in current: set_key(str(env),key,value)
    for repo,filename,revision in MODELS:
        hf_hub_download(repo_id=repo,filename=filename,revision=revision,local_dir=root/'models',token=token)
    token_path.unlink()
    ctx = int(dotenv_values(env)['LOCAL_CONTEXT_SIZE'])
    if not 512 <= ctx <= 4096:
        raise ValueError('LOCAL_CONTEXT_SIZE must be between 512 and 4096 for this deployment')
    preset = f'''version = 1
[*]
ctx-size = {ctx}
threads = 2
threads-batch = 2
parallel = 1
batch-size = 128
ubatch-size = 64
n-gpu-layers = 0
jinja = true
chat-template-kwargs = {{"enable_thinking": false}}
cache-ram = 0
'''
    for repo,filename,_ in MODELS:
        preset += f'\n[{repo}]\nmodel = {root / "models" / filename}\n'
        if repo == MODELS[0][0]:
            preset += 'load-on-startup = true\n'
    (root/'.deploy/models.ini').write_text(preset)
    user = pwd.getpwuid(os.getuid()).pw_name
    common = f'''[Unit]
After=network-online.target
Wants=network-online.target
[Service]
User={user}
WorkingDirectory={root}
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
'''
    app = common.replace('[Service]',f'[Service]\nEnvironmentFile={env}\nExecStart={root}/.venv/bin/python {root}/app.py')
    inference = common.replace('[Service]',f'[Service]\nExecStart={root}/.runtime/bin/llama-server --models-preset {root}/.deploy/models.ini --models-max 1 --host 127.0.0.1 --port 8080 --parallel 1 --threads 2 --threads-batch 2 --ctx-size {ctx} --batch-size 128 --ubatch-size 64 --n-gpu-layers 0 --no-context-shift --cache-ram 0')
    (root/'.deploy/resumelens-app.service').write_text(app)
    (root/'.deploy/resumelens-inference.service').write_text(inference)
    monitor = f'''[Unit]
Description=ResumeLens CPU and memory monitor
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User={user}
WorkingDirectory={root}
EnvironmentFile={root}/.deploy/monitor.env
ExecStart={root}/.venv/bin/python {root}/.deploy/resumelens_vm_monitor.py
Restart=on-failure
RestartSec=2
RuntimeDirectory=resumelens-monitor
RuntimeDirectoryMode=0755
StateDirectory=resumelens-monitor
StateDirectoryMode=0700
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/run/resumelens-monitor /var/lib/resumelens-monitor
[Install]
WantedBy=multi-user.target
'''
    heartbeat = f'''[Unit]
Description=Ping ResumeLens VM Healthchecks.io check
[Service]
Type=oneshot
User={user}
EnvironmentFile={root}/.deploy/monitor.env
ExecStart={root}/.venv/bin/python {root}/.deploy/resumelens_vm_heartbeat.py
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
'''
    timer = '''[Unit]
Description=ResumeLens VM liveness heartbeat schedule
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=1s
Unit=resumelens-vm-heartbeat.service
[Install]
WantedBy=timers.target
'''
    (root/'.deploy/resumelens-monitor.service').write_text(monitor)
    (root/'.deploy/resumelens-vm-heartbeat.service').write_text(heartbeat)
    (root/'.deploy/resumelens-vm-heartbeat.timer').write_text(timer)

if __name__ == '__main__':
    configure(Path(sys.argv[1]).resolve())
