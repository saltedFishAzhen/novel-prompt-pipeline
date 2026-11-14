# Novel Prompt Pipeline

[English](README.md)

将小说原文通过 shuohao 六阶段转换为剧本、美术、分镜与视频生产资料。Codex 会话子代理生成创作 JSON，Codex 图片任务生成资产板和各 cut 分镜图，ComfyUI 工作流生成视频。默认停在文本提示词；不配置 `--render` 不生成媒体。

文本生成需要 Python 3.11+、Node.js 18+ 和能够派发原生子代理的 Codex 宿主会话；生成视频还需要自行部署兼容的 ComfyUI 和模型。安装和初始化：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
novel-pipeline init ./my-workspace
cd ./my-workspace
```

在 `configs/pipeline.local.json` 配置项目及所需服务。密钥和私人素材不要提交。提供适合模型上下文长度的原文：

```powershell
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json --resume --stop-after frames --render
```

运行遇到文本任务时会停在 `awaiting_text`；由宿主派发待处理任务、导入子代理的 JSON 草稿并续跑，直至通过质量门。Python 不能自行派发会话子代理。六阶段及停止点、续跑约束和视频输入见[管线指南](docs/guides/pipeline.md)；图片需通过 [Codex 图片任务](docs/guides/codex-images.md)逐项目视验收。`--render` 不指定停止点表示请求完整媒体流程；所有分镜图验收后才会提交视频。真实小说、图片和视频端到端验收尚未完成，真实配音与音画同步仍需素材验收。

更多说明见[文档导航](docs/README.md)。
