# 小说视觉资产与分镜管线

[English](README.md)

这个 Python 项目把用户提供的小说转换为角色、场景、物品资产和分镜提示词，并可调用远端 ComfyUI 生成资产参考图、分镜首帧和视频。LLM 负责文字理解与镜头推理，程序负责校验、资产管理、工作流绑定和运行记录。

使用时需要自备小说文本、兼容 OpenAI SDK 的 LLM 服务，以及安装了所需模型和自定义节点的 ComfyUI。可通过环境变量配置多台服务器及轮询，详见 [ComfyUI 使用说明](docs/guides/comfyui.md)。仓库不包含模型权重。

## 安装与初始化

需要 Python 3.11 或更高版本。在源码目录创建并激活虚拟环境后安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
novel-pipeline init ./my-workspace
Set-Location ./my-workspace
```

Linux/macOS 激活命令为 `source .venv/bin/activate`。初始化会在指定工作区创建可维护的提示词、工作流资源、`configs/pipeline.local.json` 和 `.env.example`。复制 `.env.example` 为 `.env`，填写你自己的模型地址和密钥；在管线配置中为不同作品设置不同的 `project_id`。

也可以安装本地构建的 wheel 后初始化工作区，无须保留源码目录。本项目没有假设软件包已经发布到公开包仓库。

## 运行

在初始化后的工作区，准备自己的 UTF-8 输入文件，配置 LLM 后执行：

```powershell
novel-pipeline prompts check
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --dry-run
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --resume --render
```

将 `C:/novels/chapter.txt` 替换为你自己的文件。`--dry-run` 只检查本地配置和资源；普通运行生成资产提取、剧本、改写及分镜结果；`--resume --render` 继续该运行并调用配置的 ComfyUI。单次输入应适合所选模型的上下文和输出长度。

新运行自动读取最新发布的提示词。每次运行保留提示词快照、资产版本和参考文件哈希；续跑时会核对输入、配置和工作流。它们发生变化时，请使用新的运行目录。

## 接口与目录

统一的 `novel-pipeline` CLI 覆盖初始化、提示词、资产、数据库迁移、管线、ComfyUI 和本地 stdio MCP 服务。执行 `novel-pipeline --help` 及各子命令的 `--help` 可查看参数。各节点提示词的构建、评测和发布见[提示词指南](docs/guides/prompt-project.md)；MCP 与 CLI 共用业务服务，见 [MCP 使用说明](docs/guides/mcp.md)。

| 路径 | 职责 |
| --- | --- |
| `src/llm/` | 推理和提示词评测共用的 LLM 配置与 OpenAI SDK 适配 |
| `src/pipeline/` | 推理编排、输出校验、资产管理与数据库迁移 |
| `prompts/runtime/prompt_project/` | 提示词构建、评测和发布 |
| `src/comfyui_client/` | ComfyUI HTTP 客户端和工作流绑定 |
| `prompts/` | 提示词源、编译产物、发布版本和历史训练记录 |
| `workflows/` | API 工作流和输入绑定配置 |
| `configs/` | 管线配置模板 |
| `tests/`、`scripts/` | 自动测试和开发验证脚本 |
| `docs/` | [文档导航](docs/README.md)、指南和验证记录 |
| `novels/` | 可选的本地小说、写作资料及历史版本，由 Git 忽略，不公开发布 |

管线推理与提示词评测共用 `llm` 配置中的模型、服务地址、API 模式、密钥环境变量名、超时、重试和附加参数。实际服务是否兼容、模型能否稳定完成任务，需要接入后验证。提示词“训练”指迭代与评测，不涉及模型权重微调。

## 资产和媒体

SQLite 保存按项目区分的资产元数据、版本、来源和使用记录，参考图片以文件形式托管。续跑固定已选资产版本与参考图。详见 [管线与资产库](docs/guides/pipeline.md)。

角色参考图保留半身肖像、正侧背三视图与三格细节条。场景先生成单张主视图，再以主图为参考分别生成反向平视图和斜俯视全景，最后以前三张为参考生成垂直俯视图；每次 Qwen 只生成一个视角，由 Pillow 在本地拼成四宫格场景参考图。物品使用单视角图，原文不足以确定断裂片段时可取有据的局部。场景和物品的单次生图不再要求模型生成拼版或细节格。

管线唯一内置画风为 `cinematic_anime_cg`，工作区、CLI、MCP 和 Python SDK 均默认使用它。该画风根据参考图调优了脸部结构、材质细节与暖色胶片分级；前后词用于图片和视频，负向词只用于图片。资产参考图按画风区分缓存，续跑固定所选参考文件；画风预设不自动绑定用户参考图。配置方式见[全局画风](docs/guides/pipeline.md#全局画风)。

物品默认为 1024×1024 的 1:1 方图，可通过 `media.prop_size` 调整；旧提示词快照保留原有尺寸。使用 `novel-pipeline assets --config configs/pipeline.local.json link-face <character_asset_id> portrait.jpg` 为角色绑定人像脸部参考，也可通过 `media.character_face_references` 按名称或别名配置。脸部相似度仍需人工核验，详见[管线与资产库](docs/guides/pipeline.md)。

管线可以逐分镜选择 I2VA 或 Ref2VA，并可在受影响的媒体任务提交前检查和修改选择。Qwen 最多提供 10 张图片参考。I2VA 使用专用首帧输入；Ref2VA 将分镜图片作为参考，另支持最多 8 张图片和 3 路可选音频参考，不能保证参考图与视频第一帧完全相同。请根据实际 ComfyUI 服务器核对节点和模型，见 [ComfyUI 使用说明](docs/guides/comfyui.md)。

## 开发与许可

开发安装与检查见 [CONTRIBUTING.md](CONTRIBUTING.md)。自动测试使用模拟服务，不能证明真实 LLM 的推理质量或实际生成效果。项目已进行有限的真实 LLM 资产评测和 Qwen GPU 对照，但完整小说管线尚未端到端实测；范围及待验证项目见[验证记录](docs/verification/verification.md)。

项目采用 [MIT 许可证](LICENSE)。模型、第三方节点和用户提供的素材有各自的许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
