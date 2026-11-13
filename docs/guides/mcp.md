# MCP 接口

MCP 服务提供与统一 CLI 相同的工作区操作，使用官方 MCP Python SDK 的 stdio 传输。它由客户端启动，通过标准输入和输出通信。启动命令中的工作区应为已经初始化、并已配置 `.env` 和管线配置的目录：

```console
novel-pipeline --workspace /absolute/path/to/my-workspace mcp --config configs/pipeline.local.json
```

将工作区路径替换为自己的绝对路径。客户端需要能找到安装了本项目的 `novel-pipeline` 可执行文件；如果客户端的 PATH 与终端不同，使用该可执行文件的绝对路径。MCP 客户端的服务器配置格式各有差异，应将上面的命令和参数填入客户端对应的 stdio 配置中。

例如，支持 `mcpServers` 配置项的 Windows 客户端可以使用下列结构。两处路径都需要替换为实际安装和工作区路径；其他客户端可能使用不同的配置字段。

```json
{
  "mcpServers": {
    "novel-pipeline": {
      "command": "C:/path/to/.venv/Scripts/novel-pipeline.exe",
      "args": ["--workspace", "D:/path/to/my-workspace", "mcp", "--config", "configs/pipeline.local.json"]
    }
  }
}
```

服务读取工作区 `.env` 与选定的管线配置。LLM 推理和提示词评测使用同一组 `llm` 配置及密钥环境变量。不要把密钥写入工具参数或提示词。服务端运行时记录可能包含用户原文和模型输出，请按项目需要管理这些文件。

## 工具

客户端可以通过 MCP 的工具发现能力读取名称、参数和说明。当前接口包括：

| 类别 | 工具 |
| --- | --- |
| 工作区 | `workspace_info` |
| 提示词 | `prompts_list`、`prompts_check`、`prompts_stage`、`prompts_publish`、`prompts_evaluate` |
| 资产 | `assets_list`、`asset_get`、`asset_versions`、`asset_update`、`asset_link_image`、`asset_link_face`、`asset_archive`、`asset_restore`、`assets_import`、`assets_export` |
| 管线 | `pipeline_run`、`run_status`、`attach_job`、`shots_list`、`shot_get`、`shot_set_mode` |
| ComfyUI | `comfy_servers`、`comfy_check`、`comfy_upload`、`comfy_submit`、`comfy_wait` |
| 数据库 | `database_info`、`database_migrate` |

`pipeline_run` 会等待执行完成；使用 `dry_run=true` 可以只检查本地输入和绑定。正常推理、提示词评测和媒体生成会调用用户配置的服务。`pipeline_run` 的 `render=true` 会提交 ComfyUI 任务。

`prompts_stage` 创建不激活的候选，`prompts_evaluate` 可用 `candidate=true`、`require_coverage=true` 评测默认全节点套件。默认 `prompts_publish` 必须用 `evaluation` 提供覆盖全 16 节点且通过门禁的匹配候选报告；无 LLM 的离线合同检查发布须显式传 `offline=true`，不代表真实模型评测通过。详情见[提示词指南](prompt-project.md)。

工具参数中的文件和目录使用**相对于工作区的路径**；路径不得通过 `..`、绝对路径或越界符号链接访问工作区以外的文件。配置指向的数据库、提示词、工作流和本地音频文件也应在工作区内。统一 CLI 由本地用户直接调用时，可以按其文件权限使用工作区外的显式路径。

`asset_link_face` 为角色资产关联工作区内的人像照片；生成时作为面部身份参考。也可使用配置的角色名到照片路径映射，配置中的照片同样必须位于工作区内。详见[角色脸部参考](pipeline.md#角色脸部参考与物品尺寸)。

返回的文件路径同样使用工作区相对路径，可直接作为后续工具调用的路径参数；`workspace_info` 会另行报告工作区的绝对位置。

`shots_list` 和 `shot_get` 用于查看分镜的视频模式；`shot_set_mode` 可在受影响媒体任务提交前将某镜切换为 `i2va` 或 `ref2va`，随后使用 `pipeline_run` 的 `resume=true` 重新推理受影响的提示词。更多条件见[逐分镜选择视频模式](pipeline.md#逐分镜选择视频模式)。

为避免覆盖已有文件，MCP 的 `assets_export` 不会覆盖现存的导出文件；`comfy_wait` 将每个远端任务的下载结果写入所选目录下独立的任务子目录。

多服务器配置时，`comfy_servers` 列出名称、候选池和地址是否已配置，不返回地址或密钥。`comfy_check`、`comfy_upload`、`comfy_submit` 与 `comfy_wait` 接受可选 `server` 名称。上传后将返回的服务器名称用于含该素材的提交；多台服务器下提交远端首帧或参考文件须显式指定服务器。`comfy_wait` 可从同一目录的唯一提交记录确定服务器；裸 ID 没有唯一归属时须显式提供。服务器池配置和独立鉴权见[远端 ComfyUI 客户端](comfyui.md#连接和命令)。

## 中断与恢复

MCP 请求取消或客户端断开，不保证已提交的 LLM 或 ComfyUI 请求停止。先使用 `run_status` 检查该运行的持久化状态及远端任务 ID，再决定是否以 `pipeline_run` 的 `resume=true` 续跑。不要直接重复提交已记录 ID 的任务。

如果提交请求的响应丢失，且远端队列中可以确认任务 ID，可使用 `attach_job` 关联该 ID，再续跑。具体状态和 CLI 恢复命令见[管线与资产库](pipeline.md#远端任务与恢复)。
