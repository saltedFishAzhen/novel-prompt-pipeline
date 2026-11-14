# MCP 接口

MCP 通过 stdio 使用与 CLI 相同的应用服务。在已初始化和配置的工作区启动：

```console
novel-pipeline --workspace /absolute/path/to/my-workspace mcp --config configs/pipeline.local.json
```

客户端可通过工具发现读取名称和参数。`pipeline_run` 调用生产流程；`stop_after` 可选 `outline`、`characters`、`art`、`script`、`staging`、`prompts`、`assets`、`frames`，默认 `prompts`。`render=true` 且不指定停止阶段请求完整媒体流程；所有分镜图接受后才提交视频。运行和续跑约束见[管线指南](pipeline.md)。

需要文本创作时返回 `awaiting_text` 和 `pending_text_tasks`。宿主按冻结的任务及其依赖派发会话原生子代理，将工作区内 JSON 草稿导入后续跑；Python 服务不能自行派发子代理。提交状态 `submitted` 不代表验收，续跑仍执行质量门和有界修复。文本任务命令与流程见[管线指南](pipeline.md)。

使用 `run_status` 查看持久化状态，使用 `image_tasks_list`、`image_task_get` 查看图片任务。Codex 按任务的提示词和有序参考图生成图片，人工检查后以 `image_task_complete` 提交工作区内路径、观察 `notes` 和 `accepted=true`，然后续跑。实际版面可用 `main_region` 指定场景或物品板主格；详见[图片任务](codex-images.md)。

服务使用工作区的 `.env` 和配置；不要在工具参数中放密钥。运行记录可能包含原文和子代理输出。客户端断开不保证远端请求停止，续跑前先检查持久化状态和任务 ID。
