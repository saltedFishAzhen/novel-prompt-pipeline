# 六位玩家101分镜交接

## 状态与范围
仅第一集第一场101，已完成分镜文字、结构与逐切挂图清单。8段26切，84.2秒/本场目标79.5秒，1个场景光照批次；不是完整第一集180秒。没有生成分镜图、视频或配音。

## 正式依据及文件
- 正式全剧剧本：D:\work\articles\六位玩家\短剧改编\六位玩家-script.json
- 源SHA256：5855325613c83bfa92c0d7537ba61ee69998118c161256a7fdd63f96c22adf83
- 单场派生剧本：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-script.json
- 大纲：D:\work\articles\六位玩家\短剧改编\六位玩家-outline.json
- 美术：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-art.json
- 站位：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-staging.json
- 分镜JSON：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-storyboard.json
- 正式分镜表：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-storyboard.md
- 可阅报告：D:\work\articles\六位玩家\短剧改编\101制作\storyboard-report.html
- 逐切参考：D:\work\articles\六位玩家\短剧改编\101制作\101-逐切参考清单.json 及同名.md
- 制作资产：D:\work\articles\六位玩家\短剧改编\101制作\101-制作清单.json，引用最新C03形象，不覆盖人物源资产。
- 真实空景：D:\work\articles\六位玩家\短剧改编\101制作\images\学校食堂门口-sheet.png，HTML以相对路径显示，分发时保留images目录。
- H3提示词投产包：D:\work\articles\六位玩家\短剧改编\101制作\H3投产包\manifest.json；8个段目录各有prompt.md。26张f*.png均缺失，manifest明确标missing，不是可直接提交的完整图包。

## 已定事项
六人本就在开场，原空数组是错误元数据，正式源已纠正为C01—C06全在场。派生场与最新正式源逐字段相同；首拍“六人走出食堂”为场内移动，无新增enter。26拍完整、19句逐字保留；最后第26拍只主角离开，其他五人留在屋檐。首段blocking逐字取最终站位；0秒不是空镜，不用一人先入方案。
分镜固定16:9；午后阴雨真实S09空景；无群演、无滑板、无新增拿物事件。两部手机分别属于森巴与x坤，背包属杰哥。普通随身物不强加叙事道具ID。H3英文正文，中文台词原样；Seedance由报告程序生成。正文不指定画风，调用方按已确认资产风格附加。

## 实际校验
已执行 novel-storyboard seed、validate、render --md/--html（均带正式派生script、大纲、美术，render带--images）、export。正式validate：1集8段26镜全部通过，84.2/79.5秒，1批次。19门体系中可选第18门shot-recipe未挂载而跳过，其余启用门通过，包括逐切人物连续性。另机械断言26拍各一次且顺序不变、19条H3台词逐字逐序一致、派生场等于最新正式源、26切所有现有资产路径存在。明细：D:\work\articles\六位玩家\短剧改编\101制作\101-分镜验收.json。
站位由站位线程重验最终六人版本通过。报告生成不等于图片验收：26张实际分镜图未生成，人数、相貌、方位须下游逐切实看；不可删人名单掩盖生成遗漏。

复跑（从本目录执行，Node脚本路径为本机技能）：
```powershell
node "C:\Users\肖家正\.codex\skills\novel-storyboard\scripts\novel-storyboard.mjs" validate "六位玩家-101-storyboard.json" --script "六位玩家-101-script.json" --outline "..\六位玩家-outline.json" --art "六位玩家-101-art.json"
```

## 下一步
只制作101的26张分镜图，按逐切清单挂真实空景与每个画内人物身份图，用户另行授权付费生成；逐切验收后补齐H3包再做视频，不扩全剧。

```text
先读取 D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-storyboard-handoff.md 及正式 JSON；以文件为准；保留已确认事项；输入缺失或校验未通过时先定位问题。范围仅101第一集第一场，开场六人本就在场，26拍19句不改。读取101-逐切参考清单.json，只在获得生成授权后制作26张16:9分镜图，未生成时不宣称已出图。逐切核对每个人身份和方位，补齐投产包缺图，再交视频流程。
```
