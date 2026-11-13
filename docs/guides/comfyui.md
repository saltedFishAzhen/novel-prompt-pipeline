# 远端 ComfyUI 客户端

工作区根目录的 [`src/comfyui_client/`](../../src/comfyui_client/) 提供远端 ComfyUI 的 Python 客户端。它使用 [`workflows/`](../../workflows/) 中的 API 工作流和 [`workflow_bindings.json`](../../workflows/workflow_bindings.json)，负责上传素材、提交任务、查询历史和下载结果。管线会调用该客户端；也可以单独使用。

| 工作流配置 | 首帧 | 额外图片参考 | 音频参考 |
| --- | ---: | ---: | ---: |
| `qwen`、`qwen-unlock` | 不需要 | 最多 10 张 | 0 |
| `ref2va` | 必需 | 最多 8 张 | 最多 3 个 |
| `i2va` | 必需 | 0 | 0 |

`i2va` 的分镜图片使用专用首帧输入。`ref2va` 将分镜图片作为第一张参考图，另外的 8 张图片用于资产参考；它是视频的参考，并不保证与生成视频的第一帧像素完全一致。生成提交图时，未配置的可选图片和音频输入会被移除。管线为两种模式分别使用对应的提示词协议，也可以逐镜选择模式，见[管线与资产库](pipeline.md#逐分镜选择视频模式)。

## 连接和命令

远端地址来自工作区 `.env` 中的 `COMFYUI_URL` 或同名环境变量；使用鉴权代理时可设置 `COMFYUI_API_KEY`。客户端使用 HTTPX，并提供上下文管理器来关闭连接池。

也可以只在 `.env` 中配置多台服务器；不需要修改管线 JSON。例如：

```dotenv
COMFYUI_URLS={"gpu_a":"http://127.0.0.1:8188","gpu_b":"http://127.0.0.1:8189"}
COMFYUI_GPU_A_API_KEY=
COMFYUI_GPU_B_API_KEY=
COMFYUI_BALANCE=round_robin
# 按实际安装的模型和节点分配服务器池；省略时使用全部服务器。
COMFYUI_IMAGE_SERVERS=gpu_a,gpu_b
COMFYUI_REF2VA_SERVERS=gpu_b
COMFYUI_I2VA_SERVERS=gpu_a,gpu_b
```

`COMFYUI_URLS` 也接受逗号分隔地址，按顺序命名为 `server1`、`server2` 等；带逗号的地址请使用 JSON 格式。名称可包含字母、数字、连字符和下划线，并以字母开头。多台服务器时，密钥默认按名称读取 `COMFYUI_<名称大写>_API_KEY`，连字符转下划线；不会自动将旧的全局 `COMFYUI_API_KEY` 发送给所有服务器。每台服务器的工作流、自定义节点和模型需要自行安装并保持与对应工作流兼容；轮询不会自动识别模型或显存。

不配置 `COMFYUI_URLS` 时继续使用单地址 `COMFYUI_URL` / `COMFYUI_API_KEY`。多服务器默认对每个阶段的候选池进行跨进程轮询；可通过 `COMFYUI_DEFAULT_SERVER=gpu_a` 固定未单独设置池的阶段。具体阶段的 `COMFYUI_*_SERVERS` 优先于该默认值。轮询游标保存在资产库同目录的 `comfyui-routing.json`，不含地址或密钥。

还可以在管线 JSON 中添加 `comfyui`，以引用单独的地址和密钥环境变量，并选择各阶段候选池：

```json
{
  "comfyui": {
    "servers": {
      "gpu_a": {"url_env": "COMFYUI_GPU_A_URL", "api_key_env": "COMFYUI_GPU_A_API_KEY"},
      "gpu_b": {"url_env": "COMFYUI_GPU_B_URL", "api_key_env": "COMFYUI_GPU_B_API_KEY"}
    },
    "image_servers": ["gpu_a", "gpu_b"],
    "ref2va_servers": ["gpu_b"],
    "i2va_servers": ["gpu_a", "gpu_b"],
    "balance": "round_robin"
  }
}
```

配置中的环境变量名不含密钥本身。若确有意让多台服务器共用同一凭据，可明确把它们的 `api_key_env` 设为 `COMFYUI_API_KEY`；空字符串表示不发鉴权头。显式 JSON 服务器和 `COMFYUI_URLS` 可共同使用；同名时显式配置的 `url_env` 优先。服务器名称映射到的密钥变量名必须互不冲突。

```powershell
$env:COMFYUI_URL = "http://comfyui.example:8188"
# 如需鉴权，另外设置 $env:COMFYUI_API_KEY。

# 查看本地绑定配置；加 --remote 才访问远端服务。
novel-pipeline comfy check --profile ref2va
novel-pipeline comfy check --profile ref2va --remote

# 上传文件，返回远端 input 目录中的名称。
novel-pipeline comfy upload .\first-frame.png

# 使用 inputs.json 中的 prompt 和远端素材名称提交，再用返回的 ID 查询。
novel-pipeline comfy submit --profile ref2va --values .\inputs.json --output-dir .\runs
novel-pipeline comfy wait '<prompt_id>' --output-dir .\runs --timeout 1800
```

多服务器下先查看配置和候选池：`novel-pipeline comfy servers`。上传后使用返回的 `server` 名称，在后续 `submit --server gpu_a` 和 `wait --server gpu_a` 中选择相同服务器。存在多个服务器且提交包含远端首帧、图片或音频名称时，必须显式指定服务器；若该文件由统一 CLI/MCP 上传过，程序还会拒绝已知属于另一服务器的名称。手动准备的远端文件由调用者确认存在于所选服务器；如果同名上传记录只属于其他服务器，请在目标服务器重新上传以登记归属。

每个任务提交记录保存服务器名称及地址哈希；资产库同目录的 `comfyui-job-locations.json` 保存任务 ID 哈希及归属，使 `wait` 在更换输出目录后也能确定唯一的服务器。请保留该文件以及运行记录；旧任务或外部任务没有归属记录时，多服务器下必须通过 `--server` 明确指定。修改服务器地址后，旧记录不会被默默改发到新地址。使用 `comfy check --profile qwen --remote` 检查多服务器池时会检查该池各服务器；也可加 `--server gpu_a` 只检查一台。

`inputs.json` 的字段对应 `WorkflowTemplate.prepare` 的参数。图片和音频引用应填写上传后得到的远端文件名，例如：

```json
{
  "prompt": "subject_definitions: ...",
  "first_frame": "codex/first-frame.png",
  "reference_images": ["codex/character.png"],
  "reference_audios": ["codex/voice.wav"],
  "length": 6,
  "seed": 0
}
```

独立提交会先写入 `submitting` 记录，成功后保存返回的 `prompt_id` 和所选服务器；之后可在同一服务器等待和下载，无须再次提交。如果提交响应丢失，记录标为 `unknown`，客户端不会自动重试或切换服务器；先检查记录指定的远端队列，避免重复运行。完整管线的恢复步骤见[管线与资产库](pipeline.md#远端任务与恢复)。

## Python 调用

```python
from comfyui_client import ComfyUIClient, get_default_template

template = get_default_template("ref2va")
with ComfyUIClient("http://comfyui.example:8188") as client:
    first_frame = client.upload_file("first-frame.png")
    asset_image = client.upload_file("character.png")
    voice = client.upload_file("voice.wav")

    graph = template.prepare(
        video_prompt,  # 已生成的 Ref2VA 视频提示词
        first_frame=first_frame,
        reference_images=[asset_image],
        reference_audios=[voice],
        length=6,
        seed=0,
    )
    prompt_id = client.submit(graph)
    # 在持久化 prompt_id 后再等待，便于中断后恢复。
    history = client.wait(prompt_id, timeout=1800)
    files = client.download_outputs(history, "runs/shot-001")
```

`template.capabilities` 可查看绑定容量；`template.graph` 和 `template.profile` 返回独立副本。客户端不自动重试任务提交，也不调用全局中断。远端 API 的路由包括 `/prompt`、`/history/{prompt_id}`、`/upload/image` 和 `/view`。

## 工作流映射

Qwen 的参考图连接到 `TextEncodeQwenImage21` 的 `images.image_1` 至 `images.image_10`。Ref2VA 的首帧和参考图输入分别连接到工作流的两次条件编码，音频使用三个音频输入。

若自定义工作流或调整容量，需要同时修改真实工作流节点和绑定配置。仅修改提示词里的 `<Picture N>` 或 `<Audio N>` 不会增加工作流输入。现有 Ref2VA 映射依据公开的 T8 节点示例；目标服务器的自定义节点版本可能不同，请接入后查询其 `/object_info` 并核对输入，再实际运行验证。

本地客户端测试使用模拟 HTTP 服务，不需要启动远端 ComfyUI：

```powershell
python -m pytest tests/test_comfyui.py tests/test_httpx_client.py
```
