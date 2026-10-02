"""Run on the VM with ~/resumelens/.venv/bin/python; outputs sanitized JSON."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import httpx

root = Path(sys.argv[1] if len(sys.argv)>1 else Path.home()/'resumelens')
sys.path.insert(0,str(root))
import app
from src.examples import EXAMPLES

stop = threading.Event()
metrics = {'peak_rss_mib':0,'min_available_mib':99999,'swap_in_delta':0,'swap_out_delta':0,'oom_kill_delta':0}
def counters():
 return dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
initial = counters()
def sample():
 while not stop.is_set():
  mem = dict((k.rstrip(':'),int(v.split()[0])) for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines()))
  metrics['min_available_mib']=min(metrics['min_available_mib'],mem['MemAvailable']/1024)
  rss = 0
  for unit in ['resumelens-app','resumelens-inference']:
   group=subprocess.check_output(['systemctl','show',unit,'-p','ControlGroup','--value'],text=True).strip()
   folder=Path('/sys/fs/cgroup')/group.lstrip('/')
   for procs in folder.rglob('cgroup.procs'):
    for pid in procs.read_text().split():
     try:
      for line in Path(f'/proc/{pid}/status').read_text().splitlines():
       if line.startswith('VmRSS:'):rss+=int(line.split()[1])/1024
     except FileNotFoundError: pass
  metrics['peak_rss_mib']=max(metrics['peak_rss_mib'],rss)
  stop.wait(.5)
thread=threading.Thread(target=sample,daemon=True);thread.start()
scenarios = list(EXAMPLES) + [[EXAMPLES[0][0], review, ''] for review in ['General Resume Review', 'Skills Analysis']]
last_timings = {}
original_post = httpx.Client.post
def capture_post(self, url, **kwargs):
 response = original_post(self, url, **kwargs)
 if url == '/v1/chat/completions' and response.is_success:
  last_timings.clear()
  last_timings.update(response.json().get('timings', {}))
 return response
httpx.Client.post = capture_post
results=[]
try:
 for model in [app.LOCAL_PRIMARY_MODEL,app.LOCAL_BACKUP_MODEL]:
  for resume,review,job in scenarios:
   start=time.perf_counter()
   try:
    response=app._run_local_model(model,app._validated_prompt(resume,review,job),2048,.3)
    result={'model':model,'review':review,'seconds':round(time.perf_counter()-start,2),'response':response,'timings':dict(last_timings)}
   except Exception as error:
    result={'model':model,'review':review,'error':app._safe_error(error)}
   results.append(result)
   print(json.dumps(result),flush=True)
  print(json.dumps({'loaded_models':httpx.get('http://127.0.0.1:8080/models').json()}),flush=True)
 for model in [app.REMOTE_PRIMARY_MODEL,app.REMOTE_BACKUP_MODEL]:
  start=time.perf_counter()
  try:
   response=app._run_remote_model(model,app.require_hf_token(),app._validated_prompt(*EXAMPLES[0]),128,.3)
   print(json.dumps({'remote_model':model,'seconds':round(time.perf_counter()-start,2),'response':response}),flush=True)
  except Exception as error:
   print(json.dumps({'remote_model':model,'error':app._safe_error(error)}),flush=True)
finally:
 stop.set();thread.join()
 final=counters()
 metrics['swap_in_delta']=int(final['pswpin'])-int(initial['pswpin'])
 metrics['swap_out_delta']=int(final['pswpout'])-int(initial['pswpout'])
 metrics['oom_kill_delta']=int(final.get('oom_kill',0))-int(initial.get('oom_kill',0))
 print(json.dumps({'resources':metrics,'local_pass':all('error' not in r for r in results)}),flush=True)
