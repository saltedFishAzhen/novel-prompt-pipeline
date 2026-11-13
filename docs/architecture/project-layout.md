# 项目目录结构

- `src/`：Python 管线应用、共享 LLM 接口、ComfyUI 客户端及资源辅助代码。
- `prompts/`：`src/` 是提示词权威源码，其下 `styles/` 存放私有本地画风参考资料，不随包分发；实际内置画风词由 `src/pipeline/styles.py` 管理。`runtime/prompt_project/` 是提示词项目的 Python 实现（导入名为 `prompt_project`）；`compiled/`、`releases/` 与 `current.json` 保存编译产物、发布版本和当前版本；`evaluations/` 保存用例和实测资料，`docs/` 与其中的 `frameworks/` 保存说明，`tests/fixtures/` 和 `tools/` 保存校验夹具与兼容工具。运行使用发布版本并固定快照。
- `workflows/`：ComfyUI API 工作流及绑定配置。
- `configs/`：初始化工作区使用的配置模板。
- `tests/`：自动化测试；`scripts/`：开发与验证脚本。
- `novels/`：本地小说、写作计划、锁稿记录和作品历史版本；由 Git 忽略，不公开发布，也不包含在 Python 安装包中。
- `docs/guides/`：操作指南；`docs/verification/`：验证范围与结果；`docs/architecture/`：项目结构说明。
- `runs/`、`.tmp/`：本地资产库、运行记录或临时文件，可能包含私有素材，不作为发布资源。默认 SQLite 资产库和 ComfyUI 归属记录位于 `runs/library/`。

提示词源码、编译产物和已发布版本分别管理；修改源码后应通过提示词项目构建、检查及发布，避免直接改写既有运行快照。模型权重和用户素材由使用者自行管理。

提示词资产源码为 JSON，其他节点源码为 Markdown；`prompts/compiled/<node_id>.txt` 平铺保存每个节点的 UTF-8、LF、无 BOM 正文。节点输出协议可以不同；既有 release 路径和运行快照不随编译目录规范调整。

仓库的提示词发布目录保留当前版本和必要的评测对照版本；已移除的旧版本可从 Git 历史查找，目录本身不承诺包含全部历史版本。
