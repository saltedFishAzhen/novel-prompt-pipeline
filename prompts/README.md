# 提示词维护、评测与发布

这里统一维护整条小说制作管线的提示词。Python 构建、发布与评测代码位于 [`runtime/prompt_project/`](runtime/prompt_project/)，导入名仍为 `prompt_project`；使用前在工作区根目录执行 `python -m pip install -e .`。下表路径相对于本文件所在的 `prompts/` 目录。

## 目录

| 路径 | 用途 |
| --- | --- |
| `src/common.json` | 三类资产提取的共享规则 |
| `src/modes/` | 角色、场景、物品的差异规则 |
| `src/nodes/*.md` | 剧本、改写、分镜及辅助推理节点的权威源 |
| `src/nodes/index.json` | 节点 ID、必需变量与输出协议 |
| `src/styles/` | 私有本地画风参考资料，不随包分发或自动挂载 |
| `runtime/prompt_project/` | Python 构建、校验、评测与发布实现 |
| `compiled/<node_id>.txt` | 16 个节点的平铺文本构建产物，禁止手改 |
| `releases/<sha256>/` | 不可变的发布版，含提示词文件及 manifest |
| `current.json` | 最新发布版指针，发布完成后原子切换 |
| `evaluations/suites/`、`tests/fixtures/` | 全节点评测用例与协议夹具 |
| `evaluations/rounds/` | 独立实测记录、模型输出和问题报告 |
| `tools/` | 三类资产的兼容编译、注入和校验工具 |
| `docs/frameworks/`、`docs/` | 资产规则摘要、校验边界与修订记录 |

内置画风文字定义位于工作区根目录 [`src/pipeline/styles.py`](../src/pipeline/styles.py)；本地画风资料不改变画风配置，也不强制作为生图参考图。

资产源码使用 JSON，其他节点使用 Markdown；构建后每个节点统一生成 `compiled/<node_id>.txt`，例如 `compiled/asset.character.txt`、`compiled/novel_to_script.txt`。文件为 UTF-8、LF 换行且无 BOM 的提示词正文；不改变节点各自的变量与输出协议。历史 release 继续保留其原有目录及清单路径，运行快照和内容哈希不因此改变。

下列节点源位于 `src/nodes/`：

| 节点 ID | 节点源 |
| --- | --- |
| `novel_to_script` | `novel_to_script.md` |
| `rewrite` | `rewrite.md` |
| `rewrite_batch` | `rewrite_batch.md` |
| `storyboard` | `storyboard.md` |
| `storyboard_batch` | `storyboard_batch.md` |

其余节点为 `context_summary`、`asset_binding`、`shot_plan`、`storyboard_i2va_batch`、`scene_reverse_view`、`scene_overview`、`scene_topdown`、`continuity`，以及由结构化源编译的 `asset.character`、`asset.scene`、`asset.prop`，共 16 个节点。单条与批量模式是不同调用入口，不应连续执行两遍相同职责。

`shot_plan` 为每个分镜选择 `i2va` 或 `ref2va`；人工指定优先。I2VA 通过视频工作流的原生 `first_frame` 输入开始，`storyboard_i2va_batch` 使用首帧指令和三个 I2VA 正文字段。Ref2VA 使用既有六段节点，首帧意图图作为参考输入，支持附加图像和音频参考；它不提供 I2VA 那样的原生首帧输入。两类节点都以实际运行约束为准，生成后仍须检查画面效果。

模式提示词格式参照 [MiniMax I2VA 指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)和 [Ref2VA 指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)。

## 训练与发布

这里的训练是提示词迭代、独立执行和效果评测，不涉及模型权重微调。结构检查通过只能说明协议可解析；原文忠实性、骨架复用和实际画面效果仍需人工或独立模型复核。

1. 在 `src/` 修改源文件，保留节点输入输出契约。
2. 构建并检查，使用 `stage` 创建不切换当前指针的候选版本。
3. 使用默认全节点用例或自备 JSONL，对候选版本执行 LLM 评测，阅读原始输出及结构结果。
4. 将问题、改动和验证写入 `docs/changelog.md`，实测产物放入对应轮次目录。
5. 发布选定版本。新管线运行自动读取 `current.json`；旧运行继续使用自己的快照。

在工作区根目录执行：

```powershell
novel-pipeline prompts build
novel-pipeline prompts check
novel-pipeline prompts stage
novel-pipeline prompts --config configs/pipeline.local.json evaluate --candidate --require-coverage
novel-pipeline prompts publish --evaluation path/to/run.json
```

将 `path/to/run.json` 替换为本次评测生成的报告路径。默认套件为 [`evaluations/suites/default.jsonl`](evaluations/suites/default.jsonl)；自备 JSONL 每行包含用例标识 `id`、节点名 `node` 和按节点 manifest 定义的 `variables`。评测与正式管线共用配置文件中的 `llm` 配置及同一 LLM 调用层；空值从工作区 `.env` 或环境变量补充。评测将用例、原始输出和结构检查结果保存在运行目录中。

默认发布必须提供覆盖全 16 节点且通过门禁的匹配候选评测报告。无 LLM 的离线合同检查发布必须显式运行 `novel-pipeline prompts publish --offline`；它不代表真实模型评测通过。评测不会自动修改提示词或晋升版本，也不会把结构通过率解释为画面质量提升。

发布 API 为 `PromptRelease.load("prompts")`；使用 `.render(node_id, variables)` 渲染、`.text(node_id)` 读取文本、`.version` 记录版本。指定 `version` 可加载历史发布；`PromptRelease.from_directory(run_snapshot)` 加载固定快照。

`render` 严格检查缺失和多余变量，只替换模板自身的占位符一次，不重新解释原文中的花括号。加载发布时同时验证 manifest 的内容哈希和每个文件哈希。

仓库中的 `releases/` 保留当前版本和必要的评测对照版本，并非完整的版本历史；已移除的旧版本可从 Git 历史查找。运行时创建的提示词快照继续固定该次运行所用内容。

## 三类资产规则与旧工具

三类资产规则保留命名、非空 aliases、共享骨架、变体状态和 450 词硬上限。角色为半身肖像、正侧背三视图及三格细节条；场景的初始提示词描述一张 16:9 单视角全景，物品描述一张 1:1 单视角画面，必要时按原文证据选择局部。场景管线随后以主图为参考分别生成反向平视和斜俯视，再以前三张为参考生成垂直俯视，并在本地拼成四宫格成品。每次场景生图仍是单视角；场景和物品不要求模型输出拼版或细节格。目标词数分别在各模式源文件中配置，稀疏输入可更短，不为凑数编造。

修改时遵循 [AGENTS.md](AGENTS.md)，并通过独立执行和两层校验验证，不通过放宽校验器掩盖提示词问题。统一画风由管线在准备媒体任务时叠加，提示词源负责原文事实和各类画面的构图契约，见[画风配置](../docs/guides/pipeline.md#全局画风)。

```powershell
python prompts/tools/cli.py build
python prompts/tools/cli.py check
python prompts/tools/cli.py selftest
python prompts/tools/cli.py inject scene prompts/tests/fixtures/scene_src.txt
python prompts/tools/cli.py validate scene prompts/tests/fixtures/scene_ok.json prompts/tests/fixtures/scene_src.txt
```

`tools/cli.py build` 构建三类资产；完整节点构建和发布使用 `novel-pipeline prompts`。当前尚需人工确认的问题见 [docs/known-issues.md](docs/known-issues.md)，校验器的语义限制详见 [docs/validation-rules.md](docs/validation-rules.md)。
