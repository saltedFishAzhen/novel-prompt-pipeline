# OpenAI 图片客户端

参照相邻 `comfyui_client` 的 Python 模块、命令行、环境变量、运行记录组织。直接使用官方 OpenAI Python SDK 的 Images API，**不是 ComfyUI 节点工作流，也不需要节点式界面**。

## 安装和配置

在仓库根目录执行（SDK由你自己安装和配置）：

```powershell
.venv\Scripts\python -m pip install -e ".[images]"
```

安装后使用 `openai-images`；也可在 `workflows` 目录执行 `..\.venv\Scripts\python -m openai_images`。依赖为官方 `openai>=2.54,<3`；现有开发环境SDK为2.54.0。

进程环境变量：
- `OPENAI_API_KEY`：真实执行时必需。自行在本地设置，不要发到聊天、提交Git或写进提示词。
- `OPENAI_IMAGE_MODEL`：可选，默认 `gpt-image-2.6-sunburst`；命令行 `--model` 优先。

`.env.example` 仅为变量说明，工具**不会自动读取 `.env`**。API地址固定为 `https://api.openai.com/v1`；忽略 `OPENAI_BASE_URL` 和系统HTTP代理环境变量，不接受第三方中转。需要网络代理的环境须另外明确设计，不能假定现有代理能生效。

```powershell
# 仅检查本地SDK/环境变量是否存在，不验证密钥有效性、不连接API
openai-images check

# 默认本地校验，不产生费用，不需要密钥或SDK
openai-images generate --prompt "白底人物正面全身参考图，单人，服装与发型清晰" --size 1024x1536

# 参考图编辑/新视角：UTF-8提示词，重复--image可传多张参考图
openai-images edit --prompt-file "prompts\character.txt" --image "refs\anchor.png" --size 1536x864 --output-dir "runs\character"
```

确认提示词、参考图及费用后，**自己给同一条命令加 `--execute`** 才会发送一次可计费请求。`--dry-run` 可显式写出，与 `--execute` 互斥。执行edit会把指定参考图传到OpenAI；不要使用未获授权分享的素材。

## 参数和行为

| 参数 | 默认/用途 |
|---|---|
| `generate` / `edit` | 文生图 / 参考图编辑 |
| `--prompt` / `--prompt-file` | 二选一；文件按UTF-8（支持BOM）读取 |
| `--image` | edit必需，重复传多图；PNG/JPEG/WebP，本工具每图小于50 MiB，最多16张 |
| `--model` | 覆盖环境模型；账户是否有该模型权限仍由服务端决定 |
| `--size` | auto；2.x支持符合API约束的WIDTHxHEIGHT，例如1536x864为16:9 |
| `--quality` | auto；low/medium/high，2.5/2.6另支持xhigh/max |
| `--output-format` | png，也支持jpeg/webp |
| `--n` | 1，最多10张；数量会影响费用 |
| `--request-timeout` | 300秒，单次请求等待上限 |
| `--output-dir` | runs/openai-images，相对当前工作目录 |

每次真实执行预先创建独立 `image-随机串` 目录，包含 `run.json` 及 `image-01.png` 等新文件。不会覆盖参考图或以前产物。记录保存参数、提示词摘要哈希、输出大小/哈希和状态，不保存密钥、完整提示词或参考图路径。失败记录保留；可能有已写出的部分产物。

SDK自动重试关闭；网络超时不代表服务端未执行或未计费，**不要盲目重新加execute**。本工具不提供ComfyUI的queue/wait/cancel，也无法在断线后恢复一个同步Images请求。退出码：成功/本地校验0，业务或读取错误1，命令行参数错误2。

## 人物参考图使用建议

先用一张正面全身图作身份锚点，再以该锚点作为edit参考分别生成侧面、背面或大头照；每次提示明确保留脸、发型、服装和年龄。多张参考图不是人物一致性的保证，输出仍需人工筛选。本工具不自动改角色设定、不读项目原图、不批量启动付费任务。

## 验证及边界

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m ruff check workflows tests
```

测试使用本地模拟，不会调用真实API。没有进行真实付费生图，因此账户权限、余额、网络可达性和实际图片质量未验证。check只检查配置存在，dry-run只验证本地参数和图片签名，不是完整图片解码或服务端许可验证。

当前只做静态PNG/JPEG/WebP；未实现mask/markup、透明背景选项、SVG/GIF、流式输出或Responses API多轮编辑。图片费用按API计费规则，工具不承诺固定单价。

## 官方依据（2026-11-16查阅）

- [开发者快速开始](https://developers.openai.com/api/docs/quickstart)
- [图片生成与参考图编辑指南](https://developers.openai.com/api/docs/guides/image-generation)
- [官方Python SDK](https://github.com/openai/openai-python)

以官方最新文档和账户实际模型权限为准。安装依赖、配置环境变量与真实付费执行都由用户控制。


## 批量命令

统一生图入口与失败重试见 [BATCH.md](BATCH.md)。默认50并发，支持依赖、断点恢复、跨进程锁及明确限流的有限自动重试；不确定结果不会重复提交。
