"""Compare pinned CPU models on the deployment VM; temporarily pauses inference.

Run with ~/resumelens/.venv/bin/python. Uses synthetic examples only and writes
.deployment comparison results without credentials. Models may finish before
the 2,048-token ceiling. Restores the inference service after testing.
"""
import json, os, socket, subprocess, sys, threading, time
from pathlib import Path
import httpx
from huggingface_hub import hf_hub_download
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path.home()/'resumelens'
with socket.socket() as check:
 check.bind(('127.0.0.1',8081))
subprocess.run(['systemctl','is-active','--quiet','resumelens-inference'],check=True)
sys.path.insert(0,str(root))
import app
from src.examples import EXAMPLES
app.LOCAL_BASE_URL='http://127.0.0.1:8081'
models=[('Qwen/Qwen3-0.6B-GGUF','Qwen3-0.6B-Q8_0.gguf','23749fefcc72300e3a2ad315e1317431b06b590a'),('LiquidAI/LFM2.5-1.2B-Instruct-GGUF','LFM2.5-1.2B-Instruct-QAD-Q4_0.gguf','8ed288026e23958ad9dfa92d53ed773a8eee7125'),('HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF','smollm2-1.7b-instruct-q4_k_m.gguf','2d4a76a30b4af41ecd395c35725ac11688d4cfe4')]
report=open(root/'.deploy/comparison-2048.jsonl','w')
def emit(value):
 line=json.dumps(value);report.write(line+'\n');report.flush();print(line,flush=True)
for i,(repo,name,revision) in enumerate(models):
 start=time.perf_counter()
 path=hf_hub_download(repo_id=repo,filename=name,revision=revision,local_dir=root/'models',token=app.require_hf_token())
 models[i]=(repo,path,revision)
 emit({'download':repo,'revision':revision,'filename':name,'bytes':Path(path).stat().st_size,'seconds':round(time.perf_counter()-start,2)})
for repo,path,_ in models:
 start=time.perf_counter()
 with open(path,'rb',buffering=0) as model_file:
  os.posix_fadvise(model_file.fileno(),0,0,os.POSIX_FADV_SEQUENTIAL)
  while model_file.read(8*1024*1024):pass
 emit({'model':repo,'cache_warm_seconds':round(time.perf_counter()-start,2)})
scenarios=list(EXAMPLES)+[[EXAMPLES[0][0],kind,''] for kind in ['General Resume Review','Skills Analysis']]
original_post=httpx.Client.post
last={}
def capture(self,url,**kw):
 response=original_post(self,url,**kw)
 if url=='/v1/chat/completions' and response.is_success:
  data=response.json();last.update({'timings':data.get('timings',{}),'usage':data.get('usage',{}),'finish_reason':data['choices'][0].get('finish_reason')})
 return response
httpx.Client.post=capture
subprocess.run(['sudo','systemctl','stop','resumelens-inference'],check=True)
try:
 for repo,path,revision in models:
  start=time.perf_counter()
  log=open(root/'.deploy'/('comparison-'+repo.split('/')[0]+'.log'),'w')
  process=subprocess.Popen([str(root/'.runtime/bin/llama-server'),'--model',path,'--alias',repo,'--host','127.0.0.1','--port','8081','--threads','2','--threads-batch','2','--ctx-size','4096','--parallel','1','--batch-size','128','--ubatch-size','64','--n-gpu-layers','0','--jinja','--chat-template-kwargs','{"enable_thinking": false}','--no-context-shift','--cache-ram','0'],stdout=log,stderr=log)
  done=threading.Event();metrics={'peak_service_rss_mib':0,'min_available_mib':99999}
  def counters():return dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
  initial=counters()
  def sample():
   while not done.is_set():
    mem={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
    metrics['min_available_mib']=min(metrics['min_available_mib'],mem['MemAvailable']/1024)
    ids=[str(process.pid),subprocess.check_output(['systemctl','show','resumelens-app','-p','MainPID','--value'],text=True).strip()]
    rss=0
    for pid in ids:
     try:
      for line in Path('/proc/'+pid+'/status').read_text().splitlines():
       if line.startswith('VmRSS:'):rss+=int(line.split()[1])/1024
     except FileNotFoundError:pass
    metrics['peak_service_rss_mib']=max(metrics['peak_service_rss_mib'],rss)
    done.wait(.25)
  thread=threading.Thread(target=sample,daemon=True);thread.start()
  try:
   for _ in range(900):
    if process.poll() is not None:raise RuntimeError('server exited; inspect sanitized model log')
    try:
     if httpx.get('http://127.0.0.1:8081/health').status_code==200:break
    except httpx.HTTPError:pass
    time.sleep(1)
   else:raise RuntimeError('model load timed out')
   emit({'model':repo,'load_seconds':round(time.perf_counter()-start,2)})
   for resume,kind,job in scenarios:
    last.clear();start=time.perf_counter()
    try:
     response=app._run_local_model(repo,app._validated_prompt(resume,kind,job),2048,.3)
     emit({'model':repo,'review':kind,'max_tokens':2048,'seconds':round(time.perf_counter()-start,2),'response':response,**last})
    except Exception as error:emit({'model':repo,'review':kind,'seconds':round(time.perf_counter()-start,2),'error':app._safe_error(error)})
  except Exception as error:emit({'model':repo,'error':app._safe_error(error)})
  finally:
   done.set();thread.join();final=counters()
   for key in ['pswpin','pswpout','oom_kill']:metrics[key+'_delta']=int(final[key])-int(initial[key])
   emit({'model':repo,'resources':metrics})
   process.terminate()
   try:process.wait(timeout=20)
   except subprocess.TimeoutExpired:process.kill();process.wait()
   log.close()
finally:
 subprocess.run(['sudo','systemctl','start','resumelens-inference'],check=True)
 report.close()
