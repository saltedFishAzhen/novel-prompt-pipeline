# Director Client

输入剧情和参考图，用视觉LLM输出**导演概述和分镜表**。系统提示词在 [prompts.py](prompts.py)，采用“关系导演·潜台词分镜设计器”的导演方法、工作流程和十项自检，按固定表格格式输出。

导演直接用英文写概述和镜头描述，表头保持中文。台词、歌词及画面文字保留原语言与标点，后续H3复用英文描述并添加格式标记。

分工为：`director_client`创作 → `shot_client`本地解析/校验/导出 → `h3_client`编译。导演和H3两个阶段调用LLM，表格缺项或矛盾由导演修订。

## 配置与使用

```powershell
.venv\Scripts\python -m pip install -e .
.venv\Scripts\director-plan --input runs\source.request.json --output-dir runs\director
.venv\Scripts\shot-plan --input runs\director\shot.request.json --output-dir runs\shot
.venv\Scripts\h3-compile --input runs\shot\h3.request.json --output runs\shot\h3.prompt.txt --prompt-only
```

CLI自动读取当前目录`.env`（可用`--env-file`），复用`H3_LLM_BASE_URL / API_KEY / MODEL / REASONING_EFFORT`，默认思考强度为`low`。专用配置使用`DIRECTOR_LLM_*`；优先级为显式参数、`DIRECTOR_LLM_*`、`SHOT_LLM_*`、`H3_LLM_*`、`OPENAI_*`。Python API先调用`dotenv.load_dotenv()`。

必需输入为`type / source_text / reference_images / duration_seconds`，详见 [example.request.json](example.request.json)。可指定图片用途、原文约束、镜数与转场偏好。图片路径相对于请求文件；i2va恰好一张实际首帧，ref2va为1–9张普通引用。每次导演请求发送实际图片data URL和固定编号。

输出：

- `director.md`：导演意图、潜台词、冲突公式、关系、本场功能和13列分镜表。
- `director.report.json`：模型、思考强度、用时、用量及是否需要澄清。
- `shot.request.json`：把原输入元数据和`director.md`的文件引用交给本地shot工具。

状态`generated`表示分镜表已生成，接下来交给shot_client校验。需要补充信息时CLI返回2，并在director.md中列出问题。

## Python API

```python
from dotenv import load_dotenv
from director_client import DirectorClient, ReferenceImage
from shot_client import ShotClient
from h3_client import H3Client

load_dotenv()
images = [ReferenceImage("girl.png", name="女孩"), ReferenceImage("bottle.png", name="瓶子")]
source = "女孩抬起左臂示意瓶子，说：“今天不打开。”，然后放下左臂。右臂不动。"
with DirectorClient(reasoning_effort="low") as director:
    draft = director.direct("ref2va", source, images, 8, preferences={"shot_count": 3})
result = ShotClient().process(
    draft.table_text,
    type="ref2va",
    source_text=source,
    reference_images=images,
    duration_seconds=8,
    preferences={"shot_count": 3},
)
if result.status != "ready":
    raise ValueError(result.report)
request = result.to_h3_request()
with H3Client(reasoning_effort="low") as compiler:
    compiled = compiler.compile(request["type"], request["storyboard"], images)
print(compiled.prompt)
```
