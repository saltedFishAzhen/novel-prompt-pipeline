# Shot Client

本地分镜表处理工具：读取导演产出的Markdown表格，校验时间线、台词和参考图，导出 `h3_client` 的输入。

工作流程：

```text
剧情＋参考图 → director_client（LLM分镜表）
            → shot_client（本地处理）
            → h3_client（LLM格式编译）
```

## 三步调用

```powershell
.venv\Scripts\director-plan --input runs\source.request.json --output-dir runs\director
.venv\Scripts\shot-plan --input runs\director\shot.request.json --output-dir runs\shot
.venv\Scripts\h3-compile --input runs\shot\h3.request.json --output runs\shot\h3.prompt.txt --prompt-only
```

导演和H3默认复用现有 `.env` 与 `low`。整个流程只有两次模型调用，不需要ComfyUI地址。安装或更新入口：`python -m pip install -e .`。

## 输入和表格

处理请求包含 `type / source_text / reference_images / duration_seconds`，以及二选一的 `storyboard_table` 文本或 `table_file` 路径。`director-plan` 会直接生成该请求；也可按 [example.request.json](example.request.json) 手工提供。

可选 `reference_roles / constraints / preferences / values` 保留原有参数。图片及表格的相对路径以请求JSON所在目录为基准，导出图片使用绝对路径。i2va恰好一张真实首帧且每行参考图都写图1，ref2va为1–9张普通参考图；图片顺序不改变。`values`只接受seed/width/height，宽高成对，总长度由duration_seconds生成。

固定13列：

导演使用英文填写描述，中文列名保持固定；台词正文保留原语言。Shot按原文保留单元格，时间可写为`0–2.6s`，参考图可写为`<Picture 1>`。

| 镜号 | 时间 | 参考图 | 景别 | 机位/角度 | 运镜 | 动作调度 | 情绪/潜台词 | 台词 | 台词与肢体的关系 | 道具/场景层次 | 衔接 | 导演备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

时间填写绝对秒范围（如0–2.6s），镜号从1连续编号。参考图写`<Picture 1>`、`<Picture 2>`。单元格内换行用`<br>`（导出时渲染为分句），字面竖线用`\|`。

转场写在“衔接”列：首镜用`Opening`，硬切用`Hard cut at 2.6s`；软转场写出类型、起点和时长，如`Cross-dissolve, start at 5s, duration 0.5s, end at 5.5s`。程序核对转场时间与镜头区间，并检查软转场是否获得原文或偏好授权。

台词格式：

```text
None
Girl (Mandarin):「今天不打开。」
2.8–4.1s Girl (Mandarin):「今天不打开。」
```

跨镜同一句可写：

```text
D1 1/2 2.8–3s Girl (Mandarin):「先看这里，」
D1 2/2 3–4.1s Girl (Mandarin):「今天不打开。」
```

程序校验同组、同人物、同语言、相邻镜头、时间接壤和原句逐字拼接。未指定台词子时间时不虚构发声时间。多人对白用分号或`<br>`分隔。

## 校验与输出

自动检查必需列、连续镜号、时间线缺口/重叠/超时、引用编号、原文对白及标点、重复发声、跨镜分段、转场和部分静态运镜矛盾。剧情、表演和空间连续性由导演审读。

输出四个文件：

- `shot.table.json`：表格行与解析出的技术字段，保留各列原文；仅供排查，后续环节不读取。
- `shot.report.json`：ready/invalid、问题及校验范围。问题的`path`标明是`table`还是`input`/`reference_images`/`reference_roles`/`preferences`/`constraints`。
- `storyboard.txt`：本地确定性生成的H3编译输入。
- `h3.request.json`：只有type/storyboard/reference_images/values四个字段。

导演概述保存在director.md中；潜台词、关系和导演备注在导出时标为执行参考。校验失败时输出问题，由导演修订表格后重新处理。

退出码：`0`通过，`2`只有表格需要修订，`1`请求、图片或输出目录不可用。校验失败时只保留`shot.report.json`，旧的表格、分镜和H3请求会被清除，避免后续环节读到过期产物。

## Python API

```python
from shot_client import ShotClient

result = ShotClient().process(
    table_text,
    type="ref2va",
    source_text=source_text,
    reference_images=images,
    duration_seconds=8,
    preferences={"shot_count": 3},
)
if result.status != "ready":
    raise ValueError(result.report)
request = result.to_h3_request(values={"seed": 42})
```

