"""Build episode 3's unified offline image-job manifest. Does not call APIs."""
from pathlib import Path
import json,hashlib
BASE=Path(__file__).resolve().parent
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def build():
 assets=read(BASE/'第三集-新景任务.json');board=read(BASE/'第三集-storyboard.json');envmap=read(BASE/'第三集-逐切环境引用.json');cast=read(BASE/'第三集-资产引用.json')['characters'];actors={x['id']:x for x in cast}
 jobs=[];prompts={};generated={x['assetId']:x['outputFile'] for x in assets}
 def add(id,prompt,refs,target,deps):
  promptfile='生图提示词/'+id+'.txt';prompts[promptfile]=prompt+'\n';jobs.append({'id':id,'promptFile':promptfile,'references':refs,'outputFile':target,'dependsOn':deps,'model':'gpt-image-2.6-sunburst','size':'1536x864','quality':'medium'})
 for x in assets:
  refs=[]
  for s in x['sourceReferences']:
   p=Path(s).resolve()
   refs.append(p.relative_to(BASE.resolve()).as_posix() if p.is_relative_to(BASE.resolve()) else str(p))
  add('asset-'+x['assetId'],x['prompt']+' Single continuous photorealistic master view, NOT a collage or a model sheet. No people or human reflections. Negative: '+x['negativePrompt'],refs,x['outputFile'],['asset-'+k for k in x['dependsOn']])
 for ep in board['episodes']:
  assert ep['ep']==3
  for seg in ep['segments']:
   for i,c in enumerate(seg['cuts'],1):
    key=f"{seg['id']}/f{i}";env=envmap[key];refs=[env['path']];names=[]
    for cid in c['characters']:
     a=actors[cid];p=Path(a['reference']['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==a['reference']['sha256'];refs.append(str(p));names.append((cid,a['name']))
    prompt='生成一张横屏16:9写实电影分镜关键帧。单画面、非拼贴、非角色设定表。参考图1锁定指定实际空间，仅按本镜机位裁切；不要照搬空景全景而抹掉应在场的同伴。\n'
    for n,(cid,name) in enumerate(names,2):prompt+=f'参考图{n}是{cid}（{name}），保留此最终设定图的脸、发型、衣服与年龄，只画一个实体，不复制多视图。\n'
    prompt+='画内角色：'+','.join(c['characters'])+'。\n关键帧画面：'+c['frame']+'\n机位：'+c['cameraPosition']+'\n景别及焦距：'+c['size']+'；'+c['lens']+'\n构图：'+c['composition']+'\n视线：'+c['eyeline']+'\n焦点：'+c['focus']+'\n镜头动作仅供定位起始瞬间，不提前完成动作：'+c['shot']+'\n仍在场但画外：'+json.dumps(c['offscreen'],ensure_ascii=False)+'\n'
    notes={1:'顾岚乔本人不在画内，衣架白大褂是挂起的道具，不变成人。回档后身体与衣服完好。',2:'药瓶、通知、器械只按当前切状态出现，不能提前取出或换位置。卫生间按实际参考图：门在画面左，洗手盆/镜在远端墙中央，暗格在盆右侧的同一远端墙；不将暗格移至侧墙，不交换门与盆。疑问只是主角猜测。冲突仅非血腥悬疑画面，不渲染伤口、内脏、喷血；不画医学操作细节。',3:'只主角实体。杰哥与x坤只有电话声音，不出现在街边、不新增电话另一端人像。回档后衣服身体恢复无血。',4:'六人仍在食堂外会合处，不凭一句先坐添加并不存在的椅子或坐下动作。x坤只展示公开履历，不变成黑客。',5:'五名室友停在门外楼梯间，不能摆入客厅；按此切环境选择真实机位，顾岚乔下楼沿已给通路，不穿墙。'}
    prompt+=notes[seg['sceneIndex']]+'\n角色画外不等于离场。严格尊重门墙和可见范围，不把另一侧人物所在地画空。无额外群演、字幕、水印、姓名标签。胸牌/病历只给稳定简洁版式，不编造医院名或个人医疗数据，可读文字另行后期处理。'
    deps=['asset-'+env['assetId']] if env['assetId'] in generated else []
    add(f"{seg['id']}-f{i}",prompt,refs,'整集投产包/'+key+'.png',deps)
 assert len(jobs)==66 and len({j['id'] for j in jobs})==66 and len({j['outputFile'] for j in jobs})==66
 known={j['id'] for j in jobs};assert all(d in known for j in jobs for d in j['dependsOn'])
 for rel,text in prompts.items():
  p=BASE/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
 out={'version':1,'scope':'第三集整集：4个新场景依赖任务及62个关键帧；人物全部复用','jobs':jobs}
 (BASE/'第三集-生图任务.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 for label, selected in [('场景',jobs[:4]),('分镜', [{**j,'dependsOn':[]} for j in jobs[4:]])]:
  (BASE/f'第三集-{label}生图任务.json').write_text(json.dumps({'version':1,'jobs':selected},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'jobs':66,'assets':4,'frames':62,'networkRequested':False},ensure_ascii=False))
if __name__=='__main__':build()
