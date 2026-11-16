# ComfyUI 远程客户端

消费 `novel-storyboard` 导出的 H3 投产包：逐段上传参考图、把提示词与时长写进 `h3-ref2va` 工作流、提交给远端 ComfyUI、等待执行并下载视频。只使用 ComfyUI 官方 HTTP 接口（`/system_stats`、`/prompt`、`/history/<id>`、`/queue`、`/interrupt`、`/upload/image`、`/view`），不依赖任何自定义节点代码。

| 文件 | 职责 |
| --- | --- |
| [storyboard.py](storyboard.py) | `load_storyboard_bundle`：读取投产包、校验段/图片/提示词、选出画幅 |
| [bindings.py](bindings.py) | `build_graph`：按 `workflow_bindings.json` 把值写进工作流节点 |
| [transport.py](transport.py) | `ComfyUIClient`：HTTP 请求、轮询、上传、下载、取消 |
| [validation.py](validation.py) | `prompt_id`、远端文件名、本地文件名校验 |
| [errors.py](errors.py) | `ComfyUIError` 及子类、`BindingError` |
| [../workflow_bindings.json](../workflow_bindings.json) | 节点绑定声明（prompt / seed / length / 画幅 / 9 个参考图槽位） |
| [../h3-ref2va-api.json](../h3-ref2va-api.json) | 提交给 ComfyUI 的 API 格式工作流 |

## 安装

在仓库根目录执行：

```powershell
.venv\Scripts\python -m pip install -e .
```

命令行名是 `comfyui-remote`，也可以从 `workflows` 目录用 `python -m comfyui_client`。连接参数只从命令行或进程环境变量读取：

```dotenv
COMFYUI_URL=http://127.0.0.1:8188
COMFYUI_API_KEY=
```

`--base-url` 优先于 `COMFYUI_URL`；无鉴权服务器留空 `COMFYUI_API_KEY`，有鉴权时以 `Authorization: Bearer <key>` 发送。密钥不会写进运行记录。

## 运行投产包

```powershell
# 只做本地校验：段名、时长、提示词里的 <Picture N>、图片尺寸与画幅
comfyui-remote storyboard --manifest "路径\H3投产包\manifest.json" --dry-run

# 逐段上传、生成、下载
comfyui-remote storyboard --manifest "路径\H3投产包\manifest.json" --output-dir runs\episode

# 逐段只提交，不等待
comfyui-remote storyboard --manifest "路径\H3投产包\manifest.json" --submit-only --output-dir runs\episode
```

每段使用 manifest 的 `seconds`（秒）、`prompt.md` 分隔线 `---` 之后的原始提示词、以及 `pictures` 的顺序。图片先上传到 `storyboard/<batch>/<段名>/`，本次运行的批次目录彼此隔离。

工作流只接受每一段的 Picture 数量所对应的参考图槽位（1–9 张，见 `workflow_bindings.json` 的 `reference_slots`），多余的槽位连同加载节点一起剪掉，因此服务器不需要预置占位图片；工作流自带的三个空白音频占位节点（`107`/`108`/`109`）在 `prune` 中声明，每次提交前移除。

画幅由 Picture 1 的尺寸决定：客户端在 `1:1`、`2:3`、`3:2`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9` 中选出最接近的一项，写进工作流的 `ResolutionSelector`（节点 `29`），只影响首阶段；latent upscaler 的 `scale_by` 与其余分辨率设置保持工作流默认值。同段其余图片与 Picture 1 的比例偏差超过 3% 时在上传前报错。

## 其它子命令

```powershell
comfyui-remote check
comfyui-remote queue
comfyui-remote wait 01f2c3a4-... --output-dir runs\episode
comfyui-remote cancel 01f2c3a4-...
```

| 子命令 | 作用 | 主要参数 |
| --- | --- | --- |
| `storyboard` | 校验或运行整个投产包 | `--manifest`、`--output-dir`、`--job-timeout`、`--submit-only`、`--dry-run` |
| `check` | 探测 `/system_stats` | — |
| `queue` | 打印运行中与排队中的任务 | — |
| `wait` | 等待已提交的 `prompt_id` 并下载产物 | `prompt_id`、`--output-dir`、`--job-timeout` |
| `cancel` | 从队列删除，运行中则请求中断 | `prompt_id` |

连接参数（`--base-url` / `--api-key` / `--request-timeout` / `--poll-interval`）放在子命令前后都可以。`--job-timeout` 只控制单段等待完成的秒数，与单次 HTTP 超时无关，默认 3600。

产物与记录：

- 段产物写入 `--output-dir\<段名>\`，`wait` 写入 `--output-dir\<prompt_id>\`。
- 每段写一条 `--output-dir\comfy-<prompt_id>.json`：提交后是 `status: queued`，完成后补齐文件清单；超时或失败时该记录仍保留，可用 `wait` 继续取回结果。
- 已存在的同名产物不会被覆盖，而是加 `-1`、`-2` 后缀；文件名前缀是节点 ID。

退出码：成功 `0`，失败 `1`（错误写 stderr；参数解析失败由 argparse 返回 `2`）。等待超时会打印 `prompt_id` 以及「任务仍在队列，可加大 `--job-timeout` 或 `wait`」的提示。

## 投产包契约

`manifest.json` 是一个非空数组，每项：

| 字段 | 说明 |
| --- | --- |
| `segment` | 段名，只允许 `A-Z a-z 0-9 _ -`，且在数组内唯一 |
| `seconds` | 该段时长（秒，正数） |
| `prompt` | `prompt.md` 的相对路径，相对包目录或其父目录 |
| `pictures` | 参考图相对路径，按 Picture 1..N 顺序 |

所有路径必须留在包目录内。`prompt.md` 中 `<Picture N>` 的集合必须正好是 `1..len(pictures)`；图片只接受 PNG/JPEG/WebP，且与 Picture 1 画幅一致。

## 错误与重试

| 异常 | 含义 |
| --- | --- |
| `ComfyUIError` | HTTP/协议错误，含 `endpoint`、`status`、`prompt_id`、`payload` |
| `ComfyUITimeoutError` | 等待预算耗尽；带 `wait_seconds` / `budget_seconds`，任务仍在服务器上 |
| `WorkflowExecutionError` | 任务已被接受但执行失败（`status_str` 为 `error`/`failed`） |
| `BindingError`（`ValueError` 子类） | 值无法写进工作流（未知字段、类型不符、槽位超限、画幅不支持等） |

- 提交从不自动重试，避免网络抖动把同一任务排队两次。
- 下载产物只在网络错误或 5xx 时重试（`download_retries`，默认 2 次）；本地拒收（超大小、空文件）立即失败。
- `wait` 遇到临时错误继续轮询，遇到 4xx 立即抛出。

## 安全

- 不跟随重定向；服务端错误正文最多读 4 KB 并剔除控制字符，`api_key` 在错误信息与 payload 中替换为 `[REDACTED]`。
- `prompt_id`、上传子目录与远端文件名按同一套规则校验（拒绝绝对路径、`..`、盘符、`?`/`#`、控制字符），`/view` 的 `type` 只接受 `output`/`input`/`temp`。
- 下载先写同目录临时文件再原子替换，空文件视为失败；单文件默认上限 4 GiB（`max_output_bytes`）。
- 本地文件名经 `safe_filename` 去分隔符、去零宽/双向覆盖字符并限长。
- `POST /interrupt` 是全局接口：`cancel()` 会先查队列，但任务若在两步之间结束，被打断的可能是下一个任务。

## 测试

```powershell
.venv\Scripts\python -m pytest tests\test_comfyui_client.py tests\test_storyboard_client.py -q
```

测试使用 `httpx.MockTransport`，不连接真实服务器。需要真实环境验证时先跑 `comfyui-remote check`，再用一段小规模投产包试跑。


## 动态多服务器调度（balancer.py）

运行命令不变。新版调度器启动后默认每 2 秒只读取项目根目录 `.env` 的 `COMFYUI_URLS`，不做服务器健康探测，也不会启动/关闭远程服务：

```dotenv
COMFYUI_URLS=["https://server-a.example", "https://server-b.example"]
```

- 增加地址并保存：空闲的新节点加入领取队列，每节点最多一个本批在途任务。
- 移除地址并保存：下次读到配置后不再给该节点新任务；已提交的任务继续用原节点等待、下载。已上传但确定尚未提交的任务安全回到待分配队列。
- 改为 `COMFYUI_URLS=[]`：暂停领取新任务，有在途任务继续收尾；有待分配任务时保持运行等待再次添加节点。
- 配置暂时不合法或文件暂不可读：暂停新任务，保留在途；修正并保存后自动恢复。建议编辑完整后一次保存。日志仅显示状态与服务器数量，不打印地址或密钥。
- `--config-poll-seconds 2` 可调整轮询本地配置的间隔。无待办或可继续任务时调度器会正常结束；结束后修改配置不会自动重启进程。

SQLite 原子领取、跨批服务器锁、批次锁及输入指纹继续有效。提交结果不确定的任务不会迁移或重复提交；一个节点遇到上传/提交/等待/下载错误后本次运行不自动重试。重启同一输出目录时，已知 `prompt_id` 可继续取回，未知提交仍保持阻塞。不要删除账本或更换输出目录以绕开保护。

节点在同次运行移除时已有连接保留到任务完成；程序重启后若节点已从配置移除，该节点的未完成账本仍保留，重新加入原地址后才能继续取回。服务器排序改变不会把历史任务映射到新的第一台。API key 更新只用于新建的工作连接，已在途的连接不被中途替换。

已有旧版 Python 进程不会热加载此次代码改动，下一次正常启动新版调度器后才具备配置热更新。不要为升级强停正在生成的任务。

离线验证：`python -m unittest discover -s tests -p "test_comfyui*.py"`。新增动态测试用模拟客户端覆盖运行时增删、上传中移除、空/坏配置、未知状态、等待失败不重试及停止后在途收尾，不连接真实服务器。
