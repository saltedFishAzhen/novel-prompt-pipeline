# 统一提示词工程

角色、场景、物品与其他节点使用相同的构建、检查、评测和发布入口。本文路径与命令均相对于工作区根目录。当前共 16 个节点，提示词源码位于 `prompts/src/`，Python 实现位于 `prompts/runtime/prompt_project/`（导入名仍为 `prompt_project`）；资产提示词由公共规则与对应模式编译，其余节点由 `prompts/src/nodes/index.json` 注册。所有节点一起写入同一个不可变发布版本。

```powershell
novel-pipeline prompts list
novel-pipeline prompts build
novel-pipeline prompts check
```

资产源码是 JSON，其他节点源码是 Markdown。构建统一输出 `prompts/compiled/<node_id>.txt`，例如 `prompts/compiled/asset.character.txt` 和 `prompts/compiled/novel_to_script.txt`；每份是 UTF-8、LF 换行、无 BOM 的提示词正文。统一的是编译载体，各节点的变量和输出协议仍各自定义。不可变 release 和既有运行快照保留原来的文件布局及哈希。

`list` 给出每个节点的源码、编译路径、变量、输出协议、发布文件哈希及用例覆盖情况。`check` 同时检查编译同步、默认评测输入和正反例；不是只检查三类资产。新节点没有配齐用例时不能通过发布门禁。

## 用例与效果评测

- `prompts/tests/fixtures/node_cases.jsonl`：所有节点的离线正反例。正例必须通过生产校验器，负例必须被拒绝。
- `prompts/evaluations/suites/default.jsonl`：所有节点的真实 LLM 评测输入，不包含供模型照抄的参考答案。
- `prompts/evaluations/rounds/`：有必要保留的技能蒸馏对照、原始输出和独立审查。

评测与管线读取同一份 `llm` 配置和校验重试次数。默认不传用例文件即评测全部节点；也可以给自己的 JSONL，或按节点选择用例。

```powershell
novel-pipeline prompts --config configs/pipeline.local.json evaluate
novel-pipeline prompts --config configs/pipeline.local.json evaluate --candidate --node storyboard_batch
novel-pipeline prompts --config configs/pipeline.local.json evaluate path/to/cases.jsonl --require-coverage
```

结果默认保存在本地忽略目录 `runs/prompt-evaluations/`。每次运行保存输入、输出、版本、模型、请求 ID、每次校验尝试和每节点覆盖。初次通过数与重试后的通过数分别报告；没有评测的节点明确列为缺失。结构检查无法证明剧情忠实、镜头美感或实际媒体正确，效果判断仍需对照输入审查；有媒体时再检查成图或成片。

## 候选与发布

```powershell
novel-pipeline prompts stage
novel-pipeline prompts --config configs/pipeline.local.json evaluate --candidate
novel-pipeline prompts publish --evaluation runs/prompt-evaluations/<本次目录>/run.json
```

`stage` 构建并验证不可变候选，不改变 `prompts/current.json`。`evaluate --candidate` 直接评测当前源码对应的候选，也不激活它。带 `--evaluation` 的发布要求报告覆盖全部注册节点、输出通过生产校验，并核对候选版本及输入、结果、提示词哈希；报告缺失、失败、被修改或版本不匹配时拒绝激活。

默认发布必须提供评测报告；报告须包含当前默认评测集的全部原始用例，可以追加用例。门禁会重新核验每次输出、重试记录、首过数量和节点统计。无 LLM 的离线合同检查发布必须显式使用 `publish --offline`，它仍要求全节点正反例通过，但不代表完成真实 LLM 或内容评测。`--offline` 与 `--evaluation` 互斥。

激活后，新管线运行自动读取新版；已有运行继续使用自己的提示词快照。不要手工编辑 `prompts/compiled/`、发布文件或版本哈希。当前发布目录只保留仍需使用的版本和必要对照，其他历史通过 Git 查阅。

## 蒸馏其他 skill

先记录上游固定版本与许可，再把适合的方法改写到已有节点；只有现有节点确实无法承担时才注册新节点。同步补充输入用例与正反例，使用同一组输入做修改前后对照，保留失败和无明显增益的结果。不要因 skill 的格式不同就改动已有 H3 协议、资产名称匹配、参考槽位或用户已确定的画风。

`prompts/tools/` 中保留的三类资产工具只用于兼容和专项校验，不是另一套训练、评测或发布系统。

## MCP

MCP 使用同一组应用服务：`prompts_list`、`prompts_check`、`prompts_stage`、`prompts_evaluate`、`prompts_publish`。`prompts_evaluate` 的 `cases` 可省略；`candidate`、`nodes` 和 `require_coverage` 与 CLI 含义一致。`prompts_publish` 的 `evaluation` 接受本次报告路径；离线发布须显式传 `offline=true`。MCP 文件路径必须在当前工作区内。
