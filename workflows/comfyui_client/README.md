# ComfyUI 远程客户端

一个独立的 HTTP 客户端，通过 ComfyUI 的 HTTP API 提交 API 格式工作流、等待执行、下载产物；同时提供 **profile 绑定层**，把 `prompt / seed / width / height / length` 和参考图/参考音频写进具体工作流节点，避免手改几百行 JSON。

它不依赖 ComfyUI 的自定义节点代码，只依赖官方接口：`/system_stats`、`/prompt`、`/history/<id>`、`/queue`、`/interrupt`、`/upload/image`、`/view`、`/object_info`。

模块结构：

| 文件 | 职责 |
| --- | --- |
| [transport.py](transport.py) | `ComfyUIClient`：HTTP 请求、轮询、上传、下载、取消 |
| [bindings.py](bindings.py) | `build_graph` / `bind_graph` / `api_graph`：profile 解析与节点赋值 |
| [validation.py](validation.py) | `prompt_id`、远端文件名、本地文件名与路径校验 |
| [errors.py](errors.py) | `ComfyUIError` 及子类、`BindingError` |
| [workflow_bindings.json](../workflow_bindings.json) | 内置 `ref2va` / `i2va` 两个 profile 的绑定声明 |

典型用法是配合 [h3_client](../h3_client/README.md)：`h3-compile` 编译出的 values JSON 直接传给本客户端的 `run --profile ... --values ...`，两个环节必须使用同一个 profile 和同一台图片服务器（`remote_name` 是在那台服务器的 input 目录里的相对名字）。

## 安装与配置

在仓库根目录执行：

```powershell
.venv\Scripts\python -m pip install -e .
```

CLI 命令行名是 `comfyui-remote`（也可从 `workflows` 目录用 `python -m comfyui_client`）。**CLI 不读取 `.env`**，只读进程环境变量：

```dotenv
COMFYUI_URL=http://127.0.0.1:8188
COMFYUI_API_KEY=
```

`--base-url` 优先于 `COMFYUI_URL`，`COMFYUI_BASE_URL` 作为兼容回退。无鉴权服务器留空 `COMFYUI_API_KEY`；有鉴权时客户端以 `Authorization: Bearer <key>` 发送。密钥只从参数或环境读取，不会写进运行记录。

## CLI

```powershell
comfyui-remote --base-url http://your-comfyui:8188 check
comfyui-remote queue
comfyui-remote upload runs\shot\woman.png --subfolder codex
comfyui-remote run  --profile ref2va --values runs\shot.values.json --output-dir runs\shot
comfyui-remote wait 01f2c3a4-... --output-dir runs\shot
comfyui-remote cancel 01f2c3a4-...
```

连接参数（`--base-url` / `--api-key` / `--request-timeout` / `--poll-interval`）放在子命令前后都可以。`--job-timeout`（别名 `--timeout`）只控制等待任务完成的秒数，与单次 HTTP 超时无关，默认 3600。

| 子命令 | 作用 | 主要参数 |
| --- | --- | --- |
| `check` | 探测服务与队列可用性（`/system_stats`，失败回退 `/prompt`） | — |
| `queue` | 打印运行中与排队中的任务 | — |
| `upload` | 上传图片/音频等到 input 存储，回显 `remote_name` | `path`、`--subfolder`、`--overwrite` |
| `submit` | 只提交任务，不等待 | `--graph` 或 `--profile`、`--values`、`--output-dir`、`--client-id` |
| `run` | 提交 + 等待 + 下载 | 同上，另加 `--job-timeout`、`--no-run-subdir` |
| `wait` | 等待已存在的 `prompt_id` 并下载 | `prompt_id`、`--output-dir`、`--job-timeout`、`--no-run-subdir` |
| `cancel` | 从队列删除，运行中则请求中断 | `prompt_id` |

工作流来源二选一：

- `--graph ready.json`：已经是 API 格式、可直接提交的工作流；此模式不接受 `--values`。
- `--profile ref2va --values shot.values.json`：按 profile 绑定后生成图。`--bindings-file` 可换一份绑定声明，`--workflows-dir` 指定 profile 引用的工作流 JSON 所在目录（默认与绑定文件同目录）。

产物落盘规则：

- `run` 默认写入 `--output-dir\<prompt_id>\`，同一输出目录多次生成互不覆盖；`--no-run-subdir` 改为平铺写入。
- 每次运行写一条记录 `--output-dir\comfy-<prompt_id>.json`（`submit` 为 `status: queued`，`run` / `wait` 为 `completed` + 文件清单）。
- 已存在的同名产物不会被覆盖，而是加 `-1`、`-2` 后缀；文件名前缀是节点 ID，便于追溯来源。

退出码：成功 `0`，失败 `1`（错误写 stderr；参数解析失败由 argparse 返回 `2`）。等待超时会额外打印 `prompt_id` 以及「任务仍在队列，可加大 `--job-timeout` 或 `cancel`」的提示。

## Profile 绑定

profile 在 [workflow_bindings.json](../workflow_bindings.json) 中声明：每个运行时字段对应哪些节点的哪个输入、媒体走 `first_frame` 还是参考槽位、槽位数量与清理规则、必填要求、能力声明与宽高比表。加载时会**立即校验 schema**（未知键、类型不符、能力声明与槽位数量矛盾都会报错），所以写错不会静默失效。

绑定文件按「包内副本优先、源码树回退」的顺序查找：装了 wheel 时用 `comfyui_client/_data/workflow_bindings.json`，没有包内副本（源码运行、editable 安装）时用仓库根目录的那份。所以装成 wheel 后改仓库根目录的 JSON 不会生效，需要 `--bindings-file` 显式指定。绑定文件故意不缓存，每次调用都会重新读取。

内置两个 profile：

| profile | 工作流 | 媒体槽位 | 说明 |
| --- | --- | --- | --- |
| `ref2va` | `h3-ref2va-api.json` | 9 个参考图槽位（`ref_image_0..8` = `<Picture 1..9>`）+ 3 个参考音频槽位 | 至少 1 张参考图；**不接受 `first_frame`**，9 张图语义相同 |
| `i2va` | `h3-i2va-api.json` | 仅 `first_frame`（真正的首帧） | 不支持参考图/参考音频 |

`values` 允许的字段：`prompt`（必填字符串）、`seed`、`width`、`height`、`length`、`first_frame`、`reference_images`、`reference_audios`。具体用哪个媒体字段由 profile 决定：`i2va` 用 `first_frame`，`ref2va` 用 `reference_images`，用错会直接报「不支持」。媒体字段是**相对 ComfyUI input 目录的远端文件名**，不是本地路径——本地文件请先用 `upload` 上传，或使用 `ComfyUIClient.upload_file()`。

绑定行为：

- `width` / `height` 必须是正整数。两者同时给出时走 `target_dimensions` 模式，并按 1.03 容差匹配工作流声明的宽高比（`1:1`、`16:9` 等），不匹配直接报错并提示支持的列表；只给一个时另一个取工作流默认值，若因此比例不符会给出「请同时给宽高」的提示。
- `length` 是正整数或小数（秒）；未声明该绑定的 profile 会拒绝该字段。
- 未提供的参考槽位会被**整段剪掉**（连加载节点一起移除），因此服务器不需要预置空白占位文件；如果被移除的节点仍被其它节点连线引用，绑定会直接报错而不是发出坏图。
- `requires_reference_images` 声明「至少一张参考图」：`ref2va` 一张都不给会本地报错，而不是发出缺少 `ref_image_0` 的图去让服务器失败。
- `negative_prompt` 等字段只有在 profile 里声明了绑定才能用；内置两个 profile 未声明，会报「不支持」。
- 提交前 `api_graph` 会清掉 `_` 开头的 UI 元数据与 `definitions` 子图块，并要求每个节点 ID 为数字、含 `class_type` 与 `inputs`——只支持扁平图。

## Python API

```python
import os
from comfyui_client import ComfyUIClient, build_graph

with ComfyUIClient(
    "http://your-comfyui:8188",
    api_key=os.getenv("COMFYUI_API_KEY"),
    timeout=30.0,        # 单次 HTTP 请求超时
    poll_interval=2.0,   # 轮询 /history 的间隔
    job_timeout=1800.0,  # 等待任务完成的总预算
) as comfy:
    graph = build_graph("ref2va", {"prompt": "...",
                                   "reference_images": ["codex/woman.png", "codex/door.png"],
                                   "seed": 42, "length": 5})
    result = comfy.run(graph, "runs/shot")     # 默认写到 runs/shot/<prompt_id>/
    print(result.prompt_id, result.files)
```

常用方法：

| 方法 | 说明 |
| --- | --- |
| `health()` / `queue()` / `object_info(node_class=None)` | 服务探测、队列、已安装节点 |
| `submit(graph, client_id=None) -> prompt_id` | 提交图；图被拒（`node_errors`）时尽力清理已入队任务并报错 |
| `history(prompt_id) -> dict \| None` | 未记录时返回 `None` |
| `wait(prompt_id, timeout=None) -> history` | 轮询到完成/失败/超时；超时抛 `ComfyUITimeoutError`，任务仍在服务器上 |
| `run(graph, output_dir, *, client_id=None, timeout=None, subdir=None)` | 提交 + 等待 + 下载，返回 `RunResult` |
| `download_outputs(history, output_dir, *, prompt_id=None, subdir=None)` | 下载 `images/videos/gifs/audio` 产物，返回 `OutputFile` 列表 |
| `upload_file(path, *, subfolder="", overwrite=False) -> remote_name` | 上传到 input 存储 |
| `cancel(prompt_id)` / `interrupt()` / `delete_from_queue(prompt_id)` | 取消与中断 |
| `close()` | 关闭自有连接池；注入的 `httpx.Client` 由调用方负责 |

图绑定函数：

- `build_graph(profile, values=None, *, bindings_file=None, workflows_dir=None)`：加载 profile 的工作流并赋值。
- `bind_graph(graph, profile, values=None, *, bindings_file=None)`：把值写进你手上的图，不修改原对象。
- `api_graph(graph)`：返回去掉 UI 元数据的深拷贝，供提交/自检使用。

`RunResult` 含 `prompt_id`、`history`、`outputs`，`files` 是本地路径列表，`to_dict()` 与 CLI 记录同构。`OutputFile` 保留服务端原始 `filename` / `subfolder` / `storage_type`（`output`/`input`/`temp`），`path` 是本地落盘文件，`relative_name` 是净化后的相对名。

## 错误与重试

| 异常 | 含义 |
| --- | --- |
| `ComfyUIError` | HTTP/协议错误，含 `endpoint`、`status`、`prompt_id`、`payload` |
| `ComfyUITimeoutError` | 等待预算耗尽；带 `wait_seconds` / `budget_seconds`，任务未丢失 |
| `WorkflowExecutionError` | 任务已被接受但执行失败（`status_str` 为 `error`/`failed`） |
| `BindingError`（`ValueError` 子类） | 值无法绑定到该 profile/图（未知字段、类型不符、槽位超限、宽高比不支持等） |

重试语义是刻意收窄的：

- **提交从不自动重试**，避免网络抖动导致同一任务被排队两次。
- 下载产物只在网络错误或 5xx 时重试（`download_retries`，默认 2 次，间隔递增退避）；本地拒收（超大小、空文件）立即失败。
- `wait` 遇到临时错误继续轮询，遇到 4xx 立即抛出，不掩盖真实原因。

## 安全与健壮性

- 不跟随重定向；服务端错误正文最多读 4 KB 并剔除控制字符，`api_key` 在错误信息与 payload 中被替换为 `[REDACTED]`。
- `prompt_id`、`subfolder` 与远端文件名都按同一套规则校验（拒绝绝对路径、`..`、盘符、`?`/`#`、控制字符），`/view` 的 `type` 只接受 `output`/`input`/`temp`。
- 下载先写同目录临时文件再原子替换，空文件视为失败；单文件默认上限 4 GiB（`max_output_bytes`，传 `None` 关闭）。
- 本地文件名经 `safe_filename` 去分隔符、去零宽/双向覆盖字符并限长，产物目录不会被远端名穿越。
- `POST /interrupt` 是**全局**的：`cancel()` 会先查队列，但若任务恰在此刻结束，被打断的可能是下一个任务；需要绝对精确时应自己查 `queue()`。

## 测试

```powershell
.venv\Scripts\python -m pytest tests\test_comfyui_client.py -q
```

测试使用 `httpx.MockTransport`，不连接真实服务器；需要真实环境验证时，先跑 `comfyui-remote check`，再用小尺寸、短时长参数试跑一次 `run`。
