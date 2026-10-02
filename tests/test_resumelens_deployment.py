import importlib.util
from pathlib import Path
import pytest
from dotenv import dotenv_values

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/configure_resumelens.py'
spec=importlib.util.spec_from_file_location('configure',SCRIPT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@pytest.fixture
def root(tmp_path,monkeypatch):
 (tmp_path/'.deploy').mkdir()
 (tmp_path/'.deploy/token').write_text('hf_test123')
 monkeypatch.setattr(module,'hf_hub_download',lambda **kwargs: None)
 return tmp_path

def test_configuration_preserves_settings_and_protects_secret(root):
 (root/'.env').write_text('INFERENCE_TIMEOUT=42\nCUSTOM_SETTING=yes\n')
 module.configure(root)
 settings=dotenv_values(root/'.env')
 assert settings['INFERENCE_TIMEOUT']=='42'
 assert settings['CUSTOM_SETTING']=='yes'
 assert settings['HF_TOKEN']=='hf_test123'
 assert (root/'.env').stat().st_mode & 0o777 == 0o600
 assert not (root/'.deploy/token').exists()
 assert '--models-max 1' in (root/'.deploy/resumelens-inference.service').read_text()
 assert 'HF_TOKEN' not in (root/'.deploy/resumelens-inference.service').read_text()
 assert '.venv/bin/python' in (root/'.deploy/resumelens-app.service').read_text()
 (root/'.deploy/token').write_text('hf_rotated123')
 module.configure(root)
 assert dotenv_values(root/'.env')['HF_TOKEN']=='hf_rotated123'

def test_invalid_secret(root):
 (root/'.deploy/token').write_text('$(do-not-execute)')
 with pytest.raises(ValueError,match='Invalid credential'):module.configure(root)
 assert not (root/'.env').exists()

def test_model_revisions_are_pinned(root,monkeypatch):
 calls=[]
 monkeypatch.setattr(module,'hf_hub_download',lambda **kwargs:calls.append(kwargs))
 module.configure(root)
 assert len(calls)==2
 assert all(len(c['revision'])==40 for c in calls)
 assert [call['repo_id'] for call in calls]==[
  'Qwen/Qwen3-0.6B-GGUF',
  'LiquidAI/LFM2.5-1.2B-Instruct-GGUF',
 ]
 assert calls[0]['filename']=='Qwen3-0.6B-Q8_0.gguf'
 assert calls[1]['filename']=='LFM2.5-1.2B-Instruct-QAD-Q4_0.gguf'
 preset=(root/'.deploy/models.ini').read_text()
 assert '[Qwen/Qwen3-0.6B-GGUF]' in preset
 assert '[LiquidAI/LFM2.5-1.2B-Instruct-GGUF]' in preset
 assert 'prism-ml/Ternary-Bonsai-1.7B-gguf' not in preset

def test_unrelated_listener_aborts_before_install(tmp_path):
    import os,subprocess
    stubs=tmp_path/'bin';stubs.mkdir()
    for name,body in {'ss':'echo LISTEN', 'systemctl':'echo 0', 'curl':'exit 99'}.items():
        p=stubs/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o755)
    env=dict(os.environ,PATH=str(stubs)+':'+os.environ['PATH'])
    result=subprocess.run(['bash',str(SCRIPT.with_name('provision_resumelens.sh')),str(tmp_path)],env=env,capture_output=True,text=True)
    assert result.returncode==1
    assert 'occupied by an unrelated process' in result.stderr
    assert not (tmp_path/'.runtime').exists()

def test_source_branch_check_precedes_network(tmp_path):
    import os,subprocess
    stubs=tmp_path/'bin';stubs.mkdir()
    p=stubs/'git';p.write_text('#!/bin/sh\necho main\n');p.chmod(0o755)
    env=dict(os.environ,PATH=str(stubs)+':'+os.environ['PATH'],APP_SOURCE=str(tmp_path))
    result=subprocess.run(['bash',str(SCRIPT.with_name('deploy_resumelens.sh'))],env=env,capture_output=True,text=True)
    assert result.returncode==1
    assert 'Source must be on local-deploy-main' in result.stderr

@pytest.mark.parametrize('jump', [None, ''])
def test_deployer_uses_jump_host_and_preserves_host_checks(tmp_path, jump):
    import json, os, subprocess, sys
    source = tmp_path/'app'; source.mkdir(); (source/'src').mkdir()
    for name in ['app.py', 'pyproject.toml', 'uv.lock', '.python-version', 'README.md']:
        (source/name).write_text('test\n')
    token = tmp_path/'token'; token.write_text('hf_test123')
    stubs = tmp_path/'bin'; stubs.mkdir()
    git = stubs/'git'; git.write_text('#!/bin/sh\necho local-deploy-main\n'); git.chmod(0o755)
    log = tmp_path/'ssh.jsonl'
    ssh = stubs/'ssh'
    ssh.write_text('#!'+sys.executable+'\n'+'''import json, os, sys
with open(os.environ['SSH_TEST_LOG'],'a') as log:
    log.write(json.dumps(sys.argv[1:])+'\\n')
command = sys.argv[-1]
if command.startswith('printf'):
    print('/home/test/resumelens')
elif 'cat >' in command or 'tar -xzf' in command:
    sys.stdin.buffer.read()
''')
    ssh.chmod(0o755)
    env = dict(os.environ, PATH=str(stubs)+':'+os.environ['PATH'], APP_SOURCE=str(source), TOKEN_FILE=str(token), SSH_TEST_LOG=str(log))
    env.pop('SSH_JUMP', None)
    if jump is not None: env['SSH_JUMP'] = jump
    result = subprocess.run(['bash', str(SCRIPT.with_name('deploy_resumelens.sh'))], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) >= 6
    for args in calls:
        assert 'StrictHostKeyChecking=yes' in args
        assert 'BatchMode=yes' in args
        if jump is None:
            assert args[args.index('-J')+1] == 'akrett@turing.wpi.edu'
        else:
            assert '-J' not in args
    assert 'hf_test123' not in result.stdout+result.stderr+log.read_text()
