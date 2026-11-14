# 项目目录结构

- `src/pipeline/`：生产阶段编排、文本任务、资产和图片任务。创作由宿主会话派发 Codex 原生子代理，Python 负责质量门与后续处理。
- `src/comfyui_client/`：远端 ComfyUI 调用；`src/tts/`：独立 Breeze TTS 接口。
- `workflows/`：ComfyUI API 图和绑定配置；`configs/`：配置模板。
- `tests/`、`scripts/`：自动化测试和仓库检查。
- `docs/`：指南、架构和验证记录。
- `novels/`、`runs/`、`.tmp/`：本地作品、运行记录和临时资料，不公开分发。

生产流程固定使用 shuohao 上游脚本与参考资料，详见[阶段指令](../guides/production-rules.md)和[管线指南](../guides/pipeline.md)。模型权重、凭据和用户素材由使用者管理。
