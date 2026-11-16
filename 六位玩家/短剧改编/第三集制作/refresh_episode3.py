"""Refresh the unified episode 3 storyboard locally. Never calls image/video APIs."""
from pathlib import Path
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote

BASE = Path(__file__).resolve().parent
SKILL = Path.home() / '.codex/skills/novel-storyboard/scripts/novel-storyboard.mjs'

def atomic_write(path, text):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as f:
        temp=Path(f.name);f.write(text);f.flush();os.fsync(f.fileno())
    try:
        for attempt in range(6):
            try:
                os.replace(temp,path);return
            except PermissionError:
                if attempt==5:raise
                time.sleep(.05*(2**attempt))
    finally:
        if temp.exists():temp.unlink()

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    board=BASE/'第三集-storyboard.json'; script=BASE/'第三集-script.json';art=BASE/'第三集-art.json'
    common=['--script',script.name,'--outline',str(BASE.parent/'六位玩家-outline.json'),'--art',art.name,'--no-log']
    env={**os.environ,'PYTHONIOENCODING':'utf-8'}
    def call(command,extra):
        r=subprocess.run(['node',str(SKILL),command,board.name,*common,*extra],cwd=BASE,capture_output=True,text=True,encoding='utf-8',env=env)
        if r.returncode:raise RuntimeError((r.stderr or r.stdout).strip())
        return r.stdout
    validated=call('validate',[])
    rendered=call('render',['--html','--frames','整集投产包','--images','.'])
    data=json.loads(board.read_text(encoding='utf-8-sig'));keys=[];missing=[]
    for ep in data['episodes']:
        for seg in ep['segments']:
            for i in range(1,len(seg['cuts'])+1):
                name=f"{seg['id']}/f{i}.png";keys.append(name)
                if not (BASE/'整集投产包'/name).is_file():missing.append(name)
    sources=re.findall(r'<img\b[^>]*\bsrc=["\']([^"\']+)',rendered)
    frames=[x for x in sources if x.startswith('整集投产包/')]
    if any(re.match(r'^[A-Za-z]:',x) for x in sources):raise RuntimeError('渲染结果仍包含不安全盘符图片链接，已停止替换。')
    for src in frames:
        path=(BASE/unquote(src.split('?',1)[0])).resolve()
        if not path.is_relative_to(BASE.resolve()) or not path.is_file():raise RuntimeError('图片引用失效：'+src)
    if len(set(frames))!=len(keys)-len(missing):raise RuntimeError('图片计数与渲染链接不一致，请稍后重新刷新。')
    status=f'第三集分镜图：{len(keys)-len(missing)}/{len(keys)} 张已保存。'
    status+=(' 缺失：'+ '、'.join(x.removesuffix('.png') for x in missing)+'。') if missing else ' 无缺图。'
    status+=' 本次仅刷新本地报告，未生成或上传任何图片。'
    banner='<aside id="episode3-local-image-status" style="padding:16px;background:#fff3cd;color:#302500">'+html.escape(status)+'</aside>'
    rendered=re.sub(r'(<body\b[^>]*>)',lambda m:m[1]+banner,rendered,count=1)
    manifest=BASE/'整集投产包/manifest.json'
    if manifest.exists():
        rows=json.loads(manifest.read_text(encoding='utf-8-sig'))
        for row in rows:
            row['missing']=[f"{row['segment']}/f{i}.png" for i in range(1,row['cuts']+1) if not (manifest.parent/row['segment']/f'f{i}.png').is_file()]
        atomic_write(manifest,json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    atomic_write(BASE/'第三集-storyboard-report.html',rendered)
    print(validated.strip());print(status);print(BASE/'第三集-storyboard-report.html')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('刷新失败，未调用任何生成API：'+str(e),file=sys.stderr);raise SystemExit(1)
