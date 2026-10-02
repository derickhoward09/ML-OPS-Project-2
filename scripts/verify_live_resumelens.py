"""Live integration checks; briefly stops the backend and restarts app services."""
import json,sys,time,subprocess,threading
from pathlib import Path
import httpx
from gradio_client import Client
root=Path(sys.argv[1]) if len(sys.argv)>1 else Path.home()/'resumelens'
sys.path.insert(0,str(root))
from src.examples import EXAMPLES
PRIMARY='Qwen/Qwen3-0.6B-GGUF'
BACKUP='LiquidAI/LFM2.5-1.2B-Instruct-GGUF'
stats={'peak_service_rss_mib':0,'min_available_mib':99999,'max_simultaneous_processing':0}
stop=threading.Event()

def vmstat():return dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
initial=vmstat()
def monitor():
 while not stop.is_set():
  pids=set()
  for unit in ['resumelens-app','resumelens-inference']:
   group=subprocess.check_output(['systemctl','show',unit,'-p','ControlGroup','--value'],text=True).strip()
   if group:
    for file in (Path('/sys/fs/cgroup')/group.lstrip('/')).rglob('cgroup.procs'):
     try:pids.update(file.read_text().split())
     except FileNotFoundError:pass
  rss=0
  for pid in pids:
   try:
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
     if line.startswith('VmRSS:'):rss+=int(line.split()[1])/1024
   except FileNotFoundError:pass
  stats['peak_service_rss_mib']=max(stats['peak_service_rss_mib'],rss)
  for line in Path('/proc/meminfo').read_text().splitlines():
   if line.startswith('MemAvailable:'):stats['min_available_mib']=min(stats['min_available_mib'],int(line.split()[1])/1024)
  stop.wait(.5)
thread=threading.Thread(target=monitor,daemon=True);thread.start()

def residency():
 return {item['id']:item['status']['value'] for item in httpx.get('http://127.0.0.1:8080/models').json()['data']}
def health():
 for _ in range(60):
  try:
   httpx.get('http://127.0.0.1:8080/health',timeout=2).raise_for_status()
   httpx.get('http://127.0.0.1:7860/',timeout=2).raise_for_status();return
  except httpx.HTTPError:time.sleep(1)
 raise RuntimeError('Services did not recover')
try:
 result=httpx.post('http://127.0.0.1:8080/v1/chat/completions',json={'model':PRIMARY,'messages':[{'role':'user','content':'Say ready.'}],'max_tokens':8,'temperature':0,'chat_template_kwargs':{'enable_thinking':False}},timeout=300)
 result.raise_for_status()
 print(json.dumps({'qwen_router_smoke':result.json()['choices'][0]['message']['content'],'residency':residency()}),flush=True)
 client=Client('http://127.0.0.1:7860',verbose=False)
 args=(EXAMPLES[0][0],EXAMPLES[0][1],'',512,.3,False,'Local',PRIMARY)
 jobs=[client.submit(*args,api_name='/analyze_resume'),client.submit(EXAMPLES[0][0],'Skills Analysis','',512,.3,False,'Local',PRIMARY,api_name='/analyze_resume')]
 deadline=time.monotonic()+180
 while not all(job.done() for job in jobs):
  stats['max_simultaneous_processing']=max(stats['max_simultaneous_processing'],sum(job.status().code.name in ('PROCESSING','ITERATING','PROGRESS') for job in jobs))
  if time.monotonic()>deadline:raise RuntimeError('Live UI jobs timed out')
  time.sleep(.25)
 outputs=[job.result() for job in jobs]
 assert all(output[0].strip() and 'Local CPU' in output[1] for output in outputs)
 assert stats['max_simultaneous_processing']<=1
 states=residency()
 assert states[BACKUP]=='unloaded'
 assert states[PRIMARY]=='loaded'
 print(json.dumps({'ui_primary_qwen':True,'queue_serialized':True,'residency':states,'information':[x[1] for x in outputs]}),flush=True)
 backup=client.predict(EXAMPLES[0][0],'Skills Analysis','',512,.3,False,'Local',BACKUP,api_name='/analyze_resume')
 assert backup[0].strip() and BACKUP in backup[1]
 states=residency()
 assert states[PRIMARY]=='unloaded' and states[BACKUP]=='loaded'
 print(json.dumps({'ui_manual_backup_liquid':True,'residency':states,'information':backup[1]}),flush=True)
 subprocess.run(['sudo','systemctl','stop','resumelens-inference'],check=True)
 try:
  fallback=client.predict(EXAMPLES[0][0],'Bullet Point Strength','',256,.3,True,'Local',PRIMARY,api_name='/analyze_resume')
  assert fallback[0].strip() and 'Switched from Local to Remote' in fallback[1]
  print(json.dumps({'live_remote_failover':True,'information':fallback[1]}),flush=True)
 finally:subprocess.run(['sudo','systemctl','restart','resumelens-inference'],check=True)
 old_pid=subprocess.check_output(['systemctl','show','resumelens-app','-p','MainPID','--value'],text=True).strip()
 subprocess.run(['sudo','systemctl','restart','resumelens-app'],check=True)
 health()
 new_pid=subprocess.check_output(['systemctl','show','resumelens-app','-p','MainPID','--value'],text=True).strip()
 assert old_pid!=new_pid
 recovered=Client('http://127.0.0.1:7860',verbose=False).predict(*args,api_name='/analyze_resume')
 assert recovered[0].strip() and 'Local CPU' in recovered[1]
 print(json.dumps({'restart_recovery':True,'information':recovered[1]}),flush=True)
finally:
 stop.set();thread.join()
 final=vmstat()
 for key in ['pswpin','pswpout','oom_kill']:stats[key+'_delta']=int(final.get(key,0))-int(initial.get(key,0))
 print(json.dumps({'resources':stats}),flush=True)
