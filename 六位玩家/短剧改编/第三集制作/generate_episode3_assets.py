from pathlib import Path
import json,sys,os,hashlib,threading,concurrent.futures,argparse,tempfile,time
ROOT=Path(r'D:\work\articles');BASE=ROOT/'六位玩家/短剧改编/第三集制作';sys.path.insert(0,str(ROOT/'workflows'))
from openai_images import ImageRequest,OpenAIImagesClient
def atomic_save(path,payload,lock):
 with lock:
  with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,prefix=path.name+'.',suffix='.tmp',delete=False) as f:
   json.dump(payload,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno());tmp=Path(f.name)
  for k in range(8):
   try:os.replace(tmp,path);return
   except PermissionError:
    if k==7:raise
    time.sleep(.15*(k+1))
ap=argparse.ArgumentParser();ap.add_argument('--phase',choices=['base','bathroom'],required=True);ap.add_argument('--execute',action='store_true');args=ap.parse_args();phase=args.phase
specs=json.loads((BASE/'第三集-新景任务.json').read_text(encoding='utf-8'));selected=[x for x in specs if bool(x['dependsOn'])==(phase=='bathroom')];jobs=[]
for x in selected:
 refs=[Path(p) for p in x['sourceReferences']];assert all(p.is_file() for p in refs),'Source references missing: finish base phase first'
 target=(BASE/x['outputFile']).resolve();assert target.is_relative_to(BASE.resolve())
 prompt=x['prompt']+' Single continuous photorealistic film environment master view, no collage, no sheet, no people. Negative: '+x['negativePrompt']
 req=ImageRequest(prompt,operation='edit',images=tuple(refs),model='gpt-image-2.6-sunburst',size='1536x864',quality='medium',output_format='png',n=1);req.validate();jobs.append((x['assetId'],1,target,req))
assert len(jobs)==(3 if phase=='base' else 1) and len({str(x[2]) for x in jobs})==len(jobs)
if '--execute' not in sys.argv:
 print(json.dumps({'scope':'episode3 assets '+phase,'calls':len(jobs),'concurrency':50,'automaticRetry':False,'existingTargets':sum(x[2].exists() for x in jobs)}));sys.exit()
assert not any(j[2].exists() for j in jobs),'Existing frames; refuse batch'
run=BASE/'整集生成记录'/('assets-'+phase);run.mkdir(parents=True,exist_ok=False)
for raw in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
 raw=raw.strip()
 if raw.startswith('export '):raw=raw[7:].lstrip()
 if not raw or raw.startswith('#') or '=' not in raw:continue
 k,v=raw.split('=',1)
 if k.strip()!='OPENAI_API_KEY':continue
 v=v.strip()
 if len(v)>=2 and v[0] in {'"',"'"} and v[-1]==v[0]:v=v[1:-1]
 elif ' #' in v:v=v.split(' #',1)[0].rstrip()
 os.environ['OPENAI_API_KEY']=v
lock=threading.RLock();failed=threading.Event();record={'scope':'episode3 assets '+phase,'automaticRetry':False,'jobs':[{'segment':s,'cut':i,'status':'prepared','target':str(t)} for s,i,t,q in jobs]};manifest=run/'manifest.json';atomic_save(manifest,record,lock)
def work(index):
 s,i,t,q=jobs[index];j=record['jobs'][index]
 if failed.is_set():return False
 with lock:j['status']='before_call';atomic_save(manifest,record,lock)
 try:
  result=OpenAIImagesClient(request_timeout=600).run(q,run/f'{s}-f{i}',execute=True)
  with lock:j.update(status='api_completed',runDir=result['run_dir']);atomic_save(manifest,record,lock)
  out=Path(result['run_dir'])/result['outputs'][0]['file']
  from PIL import Image
  with Image.open(out) as im:im.load();assert im.size==(1536,864)
  data=out.read_bytes();t.parent.mkdir(parents=True,exist_ok=True)
  with t.open('xb') as f:f.write(data)
  with lock:j.update(status='saved_pending_user_review',sha256=hashlib.sha256(data).hexdigest());atomic_save(manifest,record,lock)
  print(s,i,'saved',flush=True);return True
 except Exception:
  failed.set()
  with lock:j['status']='failed_or_uncertain_no_retry';atomic_save(manifest,record,lock)
  print(s,i,'failed_or_uncertain_no_retry',flush=True);return False
with concurrent.futures.ThreadPoolExecutor(max_workers=min(50,len(jobs))) as pool:results=list(pool.map(work,range(len(jobs))))
print(json.dumps({'saved':sum(x['status']=='saved_pending_user_review' for x in record['jobs']),'total':len(jobs)}),flush=True)
sys.exit(0 if all(results) else 1)
