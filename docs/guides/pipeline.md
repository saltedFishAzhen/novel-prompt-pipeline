# 管线与资产库

默认生产流程基于固定版本的 shuohao 六阶段：`source → outline.json → cast.json / art.json / script.json → staging.json → storyboard.json → 报告和导出`。角色、美术和剧本是同级产物，执行时按各阶段实际依赖安排。宿主当前会话的 Codex 原生子代理生成创作 JSON；Python 负责校验、缓存、报告、资产和媒体，不能自行创建会话子代理。角色和美术可按依赖并行，剧本和分镜按集批次处理。

`staging.json` 根据美术文字信息推算，站位提示为草稿，不读取场景图片。分镜由 segments 和 cuts 组成，每个剧本 beat 必须按顺序恰好认领一次。随附的上游 Node 脚本执行 seed、质量门、报告和导出；Node.js 18 或以上为运行前提。上游 H3 源格式和原始校验保留，多 cut 到 Picture 顺序的转换由本项目视频工作流适配。

初始化后配置所需工作流和服务地址，提供适合模型上下文长度的原文。运行命令如下：

```powershell
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json --resume --stop-after frames --render
```

运行会冻结每项文本任务的 prompt、seed、upstream 和 episode_range，并在等待时返回 `awaiting_text` 与 `pending_text_tasks`。宿主按依赖批量派发原生子代理，子代理只将 JSON 草稿写入工作区；宿主导入结果后续跑：

```powershell
novel-pipeline text list RUN
novel-pipeline text show RUN ID
novel-pipeline text complete RUN ID --file FILE --agent-id ID
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json --resume
```

`text complete` 可选 `--notes`。提交后的 `submitted` 仅表示草稿已导入，并非验收通过；续跑时仍须通过原有 Node/schema 质量门和有界修复才能推进。重复处理后续待办，直到到达请求的停止点。

`--stop-after` 支持 `outline`、`characters`、`art`、`script`、`staging`、`prompts`、`assets`、`frames`。默认停在 `prompts`；不指定 `--render` 不生成媒体。带 `--render` 且不指定停止阶段表示明确请求完整媒体流程，所有分镜图验收后才会提交视频。Codex 图片任务须人工查看、接受并续跑，见[图片任务指南](codex-images.md)。阶段支持续跑，但输入、配置等固定文件指纹不符时拒绝继续。

新配置的 `production` 默认值为 `workers: 3`、`max_repairs: 2`、`episodes_per_batch: 3`、`aspect_ratio: "9:16"`、`minutes_per_episode: 3.0`。`production.episodes` 可选，填写正整数以指定集数。资源、服务配置及实际输出请以初始化模板和命令帮助为准。阶段指令与来源见[阶段指令](production-rules.md)。

唯一画风为 `cinematic-anime-cg`。资产和分镜图片提示词使用中文，H3 视频提示词使用英文，对白保留原文。人物板为 16:9，左约 34% 半身、右侧三视图及三个细节；场景和物品采用 16:9 L 型板，物品白底且无人和手。I2VA 单 cut 使用原生首帧；Ref2VA 按 cut 的 Picture 1 至 N 顺序引用，最多九张，不传参考音和负向提示词。画面需人工验收，格式校验不能证明视觉质量。

真实小说、图片和视频端到端验收尚未完成。Breeze 与音频后期的现有接口对 script 的适配尚待确认，见[角色声音](character-voices.md)和[音频后期](../architecture/audio-postproduction.md)。
