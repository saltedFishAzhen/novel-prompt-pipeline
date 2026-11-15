# H3 分镜格式编译客户端

用 **OpenAI Python SDK 的 Chat Completions** 调用支持视觉的 LLM，将已经合格的自然语言分镜转换成 MiniMax H3 提示词。模型同时接收原分镜和参考图。系统提示词明确禁止增加剧情、动作、镜头、声音、台词或为凑词数扩写；描述转为英文，台词、歌词、画面文字保留原文。

英文描述逐项保留原分镜的物体、数量、大小、形状、颜色、材质、位置和时间。参考图定义写明引用对象、用途与排除项；图中细节由图片引用承载，文字只转换原分镜明确写出的内容。

导演已输出英文镜头描述时，H3复用原句，只添加字段、镜头标记、主体引用和对白标签。

Shot导出的带时间对白会核对原话、顺序和完整起止区间；缺失结束时间的编译结果报错。英文表格中的潜台词、表演意图和导演备注随镜头保留为执行说明。

格式依据 MiniMaxAI 官方指南（2026-11-15 核对）：

- [基础格式 / I2VA](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)：首帧声明和三个字段。
- [全参考格式 / REF2VA](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)：六个字段。

系统提示词在 [prompts.py](prompts.py)。客户端检查字段完整性、图片编号、切点、镜头标记、响应结束状态和JSON格式。分镜未指定声音时保留“未指定”。编译结果可与原分镜逐项对照。

客户端只对表示形式做确定性规范化，不修改正文：已知字段标题的大小写（`Detailed_description:` 转为 `detailed_description:`）、CRLF 换行、以及 `<Picture 01>` 这类补零编号。未知字段名仍会拒绝。含对白的结果必须具有说话者编号和完整的 `<d>[Language] 原文</d>` 标签。

对白正文须包含实际台词，说话者编号从S1按首次发声顺序使用；`<Subject N> (Sx)` 组合保持主体与声音编号稳定。CLI 编译失败时，将可识别的旧 values/prompt 文件改名为 `<文件名>.stale`。REF2VA 无音频输入时移除模板占位音频加载器。

多镜头分镜由上游明确切点、转场类型/时长及切前切后的动作状态。编译后第一镜使用 `[Shot 1]`，后续镜头使用 `[Shot N] At MM:SS.mmm, ...` 且另起一行；正文回指先前镜头使用普通文字 `Shot 1`。跨切镜的一句对白按原切点分段，保留同一说话者及原文；两段 `<d>` 内的连接处各使用一个 `<scenetrans>` 单标记，结束截断可使用 `<cutoff>`。这两个控制标记按单标签使用。编译器保留原切点和过渡动作。

## 安装与配置

在仓库根目录执行：

```powershell
.venv\Scripts\python -m pip install -e .
```

在根目录 `.env` 填入以下配置；CLI 自动读取当前目录的 `.env`，也可使用 `--env-file`。Python API 直接读取进程环境，需要时自行先调用 `dotenv.load_dotenv()`。

```dotenv
H3_LLM_BASE_URL=https://your-host/v1
H3_LLM_API_KEY=your-key
H3_LLM_MODEL=your-vision-model
H3_LLM_REASONING_EFFORT=low
COMFYUI_URL=http://your-comfyui:8188
COMFYUI_API_KEY=
```

也接受 `OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL`，专用 `H3_LLM_*` 配置优先。无鉴权的本地 LLM 服务可将 API key 设为 `unused`。模型必须支持 Chat Completions 中的 `image_url` 和 base64 data URL；这里的模型名是**编译用 LLM**。密钥只从环境读取，不写入编译结果。

编译默认发送 `reasoning_effort="low"`。可通过 `H3_LLM_REASONING_EFFORT`、CLI 的 `--reasoning-effort` 或 Python 构造参数 `reasoning_effort` 设置为 `low / medium / high`；显式参数优先于环境配置。实际思考行为由兼容服务及所选模型决定。

## 输入

复制 [example.request.json](example.request.json) 到自己的工作目录，修改图片路径和分镜。图片支持 PNG、JPEG、WebP；相对路径相对于请求 JSON 所在目录。

三个主要参数是 `type`、`storyboard`、`reference_images`：

- `i2va`：恰好一张图，该图就是首帧。
- `ref2va`：1–9 张有序参考图，依次映射为 `<Picture 1>` 至 `<Picture N>`。可在图对象的 `name` 写分镜中的别名；建议分镜直接使用 `<Picture N>`，减少歧义。`<Subject N>` 表示语义主体，与图片槽位编号无关。参考图是否用作关键帧或构图锚点，由原分镜决定，不自动将第一张图当作开场画面。

**参考图编号：** `ref2va` 的 1–9 张图按顺序写入 `reference_images`，依次对应工作流的 `ref_image_0` 至 `ref_image_8`，也就是提示词里的 `<Picture 1>` 至 `<Picture 9>`。第 1 张图不做特殊处理，不自动当作开场画面；是否作为关键帧或构图锚点完全由原分镜决定（[T8 节点源码](https://github.com/T8mars/comfyui-minimax-h3-audio-T8/blob/main/h3_t8/conditioning.py#L429-L466) 中这 9 个槽位语义一致）。`i2va` 只有一个媒体字段 `first_frame`，那才是真正的首帧。

每张图片可写为本地路径字符串，或 `{"path": "woman.png", "name": "女主", "remote_name": "codex/woman.png"}`。`remote_name` 是已上传到目标 ComfyUI input 目录的相对文件名，不是本地路径。单张参考图上限 32 MB。可选 `values` 透传 `seed / width / height / length` 等原客户端支持的生成设置，不能覆盖编译后的提示词或媒体字段；`length` 的单位沿用原工作流（秒），允许小数。

`length` 决定切点校验：多镜头分镜必须显式给出 `length`，否则编译器会拒绝输出——工作流默认时长只有 3 秒，沿用默认值会静默截断后续镜头。单镜头分镜可省略 `length`，此时沿用工作流默认值。

## 编译并衔接 ComfyUI

只检查 H3 提示词时，使用 `--prompt-only`。图片只发送给 LLM，无需填写 `remote_name`、配置 ComfyUI 或上传文件；输出是 UTF-8 纯文本：

```powershell
.venv\Scripts\h3-compile --input runs\shot.request.json --output runs\shot.h3.txt --prompt-only
```

输出回显包含 `profile / prompt_file / mode`。此模式与 `--upload` 互斥；它不会生成可直接传给 `comfyui_client --values` 的 JSON。需要运行 ComfyUI 时使用下面的默认模式。

图片已经上传时，填写 `remote_name` 后执行：

```powershell
.venv\Scripts\h3-compile --input runs\shot.request.json --output runs\shot.values.json
```

图片尚未上传时，省略 `remote_name` 并加 `--upload`。该选项只上传图片，不提交视频任务：

```powershell
.venv\Scripts\h3-compile --input runs\shot.request.json --output runs\shot.values.json --upload
```

输出文件就是 `comfyui_client --values` 需要的对象：都有 `prompt`，`i2va` 还带 `first_frame`，`ref2va` 带按顺序排列的 1–9 个 `reference_images`。编译类型通过 CLI 的 `profile` 回显，不混入 values 文件，因此无需手工提取或修改字段。

检查输出后使用同一个 profile 和同一台图片服务器：

```powershell
.venv\Scripts\comfyui-remote --base-url http://your-comfyui:8188 run --profile ref2va --values runs\shot.values.json --output-dir runs\shot
```

输出默认落在 `runs\shot\<prompt_id>\`，因此同一个输出目录里多次生成不会互相覆盖；已存在的同名文件会加 `-1`、`-2` 后缀而不是被重写。需要扁平目录时加 `--no-run-subdir`，运行记录仍写到 `runs\shot\comfy-<prompt_id>.json`。连接参数（`--base-url` / `--api-key` / `--request-timeout` / `--poll-interval`）放在子命令前后都可以；profile 默认读取包内的 `workflow_bindings.json`，需要自定义副本时用 `--bindings-file` 与 `--workflows-dir`。

原 `comfyui_client` CLI 不自动加载 `.env`，因此上例显式传服务器地址。使用有鉴权的服务器时，先将 `COMFYUI_API_KEY` 设为进程环境变量。也可使用下方 Python API，在同一个进程中加载配置并完成调用。

从源码调用模块时，可在 `workflows` 目录执行 `python -m h3_client ...`、`python -m comfyui_client ...`；安装后可从任意目录调用模块或控制台命令。

## Python API

```python
import os
from dotenv import load_dotenv
from h3_client import H3Client, ReferenceImage
from comfyui_client import ComfyUIClient, build_graph

load_dotenv()
with H3Client() as llm:
    result = llm.compile(
        "i2va",
        "单镜头，固定机位。图1中的女子用左手推门，跨过门槛后松手。无对白，无配乐。",
        [ReferenceImage("opening.png", name="图1")],
    )
print(result.prompt)  # 此时尚未上传图片、生成视频

with ComfyUIClient(os.environ["COMFYUI_URL"], api_key=os.getenv("COMFYUI_API_KEY")) as comfy:
    values = result.to_values(comfy, values={"seed": 42, "length": 5})
    graph = build_graph(result.profile, values)
    # 测试视频时再执行：comfy.run(graph, "runs/shot")
```

若每张图已指定 `remote_name`，直接 `values = result.to_values()`，无需连接 ComfyUI。可注入 `sdk_client=OpenAI(...)` 复用自定义 SDK 客户端，其生命周期由调用方负责。

所有失败路径都抛 `H3Error`（`ValueError` 的子类）：LLM 请求/应答问题、提示词不合规、`remote_name` 非法、图片不可读或超过上限、生成设置无法绑定到 profile、图片上传失败。`H3Compilation` 也可以手工构造，构造时会重新校验 profile、图片数量和提示词，不必经过 `H3Client`。
