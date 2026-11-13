# 管线与资产库

管线负责提示词节点推理、项目资产库与媒体任务编排。在初始化的工作区中，使用统一的 `novel-pipeline` CLI。以下命令假设已激活虚拟环境。

## 推理节点

1. `context_summary`：提炼原文支持的故事与章节情节。
2. `asset.character`、`asset.scene`、`asset.prop`：从原文提取三类资产，执行结构及内容校验，登记资产库。
3. `novel_to_script`：拆成单行叙事记录，由程序分配稳定的 `panel_index`。
4. `rewrite_batch`：按原编号和顺序润色，不增删拆并。
5. `asset_binding`：选择该镜使用的具体造型资产 ID，同时标明首帧实际出现的子集。
6. `shot_plan` 与对应的分镜节点：选择每镜的 I2VA 或 Ref2VA 模式，生成相应的首帧图片与视频提示词，验证编号、精确名称、实际参考输入、时长和切点。
7. `continuity`：保存本批文本计划的结束状态，交给下一批；它不代表已观察到成片末帧。

管线的中间表示是 JSON：剧本/改写记录为 `{panel_index,text}`，分镜记录还包含图片、视频提示词、时长、资产 ID、首帧资产 ID，以及 `generation_mode`、模式来源和原因。各提示词的原生输出协议分别解析，接线由程序完成。

`prompt_version=null` 使用 `prompts/current.json`。每个运行将发布目录复制成 `prompts/` 快照，续跑验证文件哈希，避免训练新版本影响进行中的任务。每个节点通过验证后立即保存，失败时保留原始输出并停止，校验失败最多按 `validation_retries` 重试，不静默丢镜。

## 提示词维护与评测

提示词源在工作区 `prompts/src/`。构建、检查和发布使用统一 CLI；发布后，新运行读取最新版本，已有运行继续使用自己的快照。

```powershell
novel-pipeline prompts --config configs/pipeline.local.json build
novel-pipeline prompts --config configs/pipeline.local.json check
novel-pipeline prompts --config configs/pipeline.local.json stage
novel-pipeline prompts --config configs/pipeline.local.json evaluate --candidate --require-coverage
novel-pipeline prompts --config configs/pipeline.local.json publish --evaluation path/to/run.json
```

将 `path/to/run.json` 替换为评测生成的报告路径。默认套件覆盖全 16 节点；自备 JSONL 每行用 `node` 指定节点、用 `variables` 提供节点要求的输入，可用 `id` 标识用例。默认发布必须提交覆盖全节点且通过门禁的匹配候选评测报告。仅运行离线合同检查时，须显式指定 `publish --offline`，不能把它视为真实 LLM 评测。评测与 `run` 共用 `llm` 配置；结构检查不能单独证明内容忠于原文或生成画面准确。详见[提示词指南](prompt-project.md)。

## 配置

初始化工作区后，在 `configs/pipeline.local.json` 中设置自己的 `project_id`。路径相对于配置文件所在目录解析，模型与地址可使用 `.env` 提供。管线推理和提示词评测共用 `llm` 配置：模型、服务地址、API 模式、密钥环境变量名、超时、重试和附加请求参数。`llm.extra` 可配置接入服务支持的请求参数，例如输出 token 上限；管线不会强制所有模型使用相同的 temperature 或 reasoning 参数。

`media.image_profile` 默认为 `qwen`；`media.video_profile` 默认为 `ref2va`，`media.i2va_profile` 默认为 `i2va`。顶层 `generation_mode` 默认为 `auto`：由 LLM 按镜头内容选择 `i2va` 或 `ref2va`，也可在新运行的配置中统一指定其中一种。自定义工作流可使用 `bindings_file`、`workflows_dir` 及 profile 名称。两种视频模式使用各自的提示词协议。

ComfyUI 可通过 `COMFYUI_URLS` 配置多台，并为图片、Ref2VA 和 I2VA 指定各自候选服务器池；默认对新任务按池轮询。具体环境变量、各服务器独立鉴权和单台固定配置见[远端 ComfyUI 客户端](comfyui.md#连接和命令)。同一任务的上传、提交、轮询和下载固定在选定服务器；跨服务器使用已下载的图片时会重新上传，上传缓存按服务器隔离。续跑使用原服务器，地址或路由变化时应新建运行。服务器池内的节点和模型兼容性需自行确认。

资产图片默认生成 1024×576 横图；分镜首帧默认 576×1024 竖图，与当前 Ref2VA 工作流的 9:16 一致。分别通过 `asset_width/asset_height`、`frame_width/frame_height` 配置。更换分镜画幅时也应修改视频工作流的 ResolutionSelector 比例；视频分辨率保留工作流自己的显存与两阶段放大设置。

三个音频参考均可选，例如在 `media.reference_audios` 中配置：

```json
[
  {"path":"../voices/actor-a.wav","usage":"主角的音色参考；不要复制原音频对白","remote":false},
  {"path":"reference/room-tone.wav","usage":"室内环境声参考","remote":true,"server":"gpu_b"}
]
```

本地路径会上传到选定的 ComfyUI input 目录；`remote=true` 使用远端已存在的相对文件名。多服务器 Ref2VA 候选池有多台时，远端音频须用 `server` 指明所在服务器，所有远端音频必须位于同一台，视频任务会固定到该服务器；单服务器或仅有一个候选服务器时可省略。Ref2VA 中的顺序对应 `<Audio 1>` 至 `<Audio 3>`，I2VA 不使用这些额外音频参考。未配置音频时，生成结果不能引用音频素材。远端节点与模型是否支持相应音频用途，以实际服务为准。

## 逐分镜选择视频模式

自动选择后，可以检查每镜的模式、来源与原因。确认需要调整时，在受影响媒体任务提交之前修改：

```powershell
novel-pipeline shots list runs/chapter-001
novel-pipeline shots show runs/chapter-001 0
novel-pipeline shots set-mode runs/chapter-001 0 i2va
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --resume
```

将 `0` 换成实际分镜编号，模式可以是 `i2va` 或 `ref2va`。切换会使所在批次及后续批次的相关分镜和连续性提示词需要重新推理；以上命令不会自动重建已提交媒体。如果这些分镜已有图片或视频任务提交，修改会被拒绝，应创建新运行。旧版不含混合模式提示词快照的运行也需新建，才能切换到 I2VA。

I2VA 使用分镜图片的专用首帧输入，不使用额外视频图片或音频参考。Ref2VA 将分镜图片作为第一张参考图，可增加最多 8 张资产图片和 3 路音频参考；它不保证参考图与生成视频第一帧完全相同。

## 全局画风

项目唯一内置画风是 `cinematic_anime_cg`，工作区、CLI、MCP 及 Python SDK 未指定画风时均使用它。该画风描述参考图定向调优的脸部结构、材质细节与暖色胶片分级；它使用文字，不自动绑定画风参考图。

管线生图节点要求英文视觉描述，同时保留资产原名及需要出现在画面中的原文文字；这不表示所有自定义输入都会被强制翻译。

`prefix` 和 `suffix` 会在提交媒体任务时分别加入画面描述前后，资产与分镜的原始推理结果不被修改；视频风格词放入画面描述段，保留 I2VA 首帧指令及音频段。可以通过在 `presets` 中定义同名预设来覆盖默认文本。

```json
{
  "style": {
    "preset": "cinematic_anime_cg",
    "presets": {
      "cinematic_anime_cg": {
        "prefix": "Custom visual treatment.",
        "suffix": "Keep the rendering consistent.",
        "negative": "watermark, blurry details"
      }
    },
    "image_negative": null
  }
}
```

`negative` 是该画风的图片负向词；`image_negative` 为可选覆盖：`null` 继承画风值，字符串替换它，`""` 显式关闭图片负向词。Qwen 图像工作流将负向词绑定到真实负向输入；自定义图片工作流须在其 profile 中提供真实的 `negative_prompt` 绑定，否则管线在上传或提交任何媒体任务之前报错。视频使用画风前后词，不使用负向提示词；当前 H3 I2VA 和 Ref2VA 工作流也没有负向输入。

运行会记录画风配置和实际解析后的词；改变画风文本后请创建新运行。早于画风层的运行没有当前画风身份，也须创建新运行。资产图按实际生图画风词及原始参考图哈希分别管理：有相同画风的结果时复用，否则将已有参考图用于保持身份并重新生图；原参考图改变后也会重新生成。参考图的内容仍可能影响结果，实际画面须复核。

## 资产管理

当前发布版包含场景多视角节点时，使用场景资产时先生成单张主视角，以主图为参考分别生成反向回望平视和全景俯视；随后按主图、反向图、全景图的顺序作为三张参考，生成单张垂直俯视图。各阶段单独提交并记录任务 ID，最终用本地程序将四张原图合成为 2×2 四宫格，顺序依次为主视角、反向回望、全景俯视、垂直俯视。四宫格作为管线中的场景参考，四张原图及引用关系保存在资产库和运行状态中。

三个视角描述节点只读取文字场景描述，不会观察已生成的图片；后续图像之间的一致性由 Qwen 工作流接收对应参考图实现，实际空间关系仍须查看生成结果。画风前后词与图片负向词用于每一张图，底层图像工作流至少需要三个参考图槽位；缺少槽位时会在提交任何媒体任务前报错。旧运行的提示词快照如不包含这些场景节点，会继续沿用原来的单图流程；要使用四视角流程需新建运行。场景四视角缓存与旧单图分开，并记录画风及原始参考图版本。

数据库与托管文件保存在 `asset_library` 指定位置。操作使用相同配置中的项目 ID：

```powershell
novel-pipeline assets --config configs/pipeline.local.json list
novel-pipeline assets --config configs/pipeline.local.json list --kind character --query 许青
novel-pipeline assets --config configs/pipeline.local.json show '<asset_id>'
novel-pipeline assets --config configs/pipeline.local.json versions '<asset_id>'
novel-pipeline assets --config configs/pipeline.local.json update '<asset_id>' --description-file updated-description.txt
novel-pipeline assets --config configs/pipeline.local.json link-image '<asset_id>' reference.png
novel-pipeline assets --config configs/pipeline.local.json link-face '<character_asset_id>' portrait.jpg
novel-pipeline assets --config configs/pipeline.local.json export assets-export.json
novel-pipeline assets --config configs/pipeline.local.json import assets-export.json
novel-pipeline assets --config configs/pipeline.local.json archive '<asset_id>'
novel-pipeline assets --config configs/pipeline.local.json restore '<asset_id>'
```

同一身份的不同服装、场景状态或物品状态保留独立资产。`aliases` 用于对应原文，不能作为造型唯一键。元数据更新建立新版本，已有版本保留；关联图片复制到库的托管目录并记录内容哈希，删除原始上传文件不会使托管图片失效。导出清单含版本、来源及文件信息，迁移到别的机器时需要同时迁移托管文件，并保证导入清单中的路径可访问。

默认 `reuse_existing_assets=true`，提取到同名资产时复用库中的当前版本。设为 false 可将本次提取登记为新的当前版本。归档资产不会因重新提取而自动恢复。

同一版本关联多张参考图时，新运行优先使用最近关联的一张；选定后记录文件哈希，续跑固定该图片。文件丢失或损坏时会报错，避免静默使用错误参考图。

### 角色脸部参考与物品尺寸

`link-face` 将 PNG、JPEG 或 WebP 人像复制到资产库，作为该角色当前版本的脸部参考。生成时，人像作为第一张参考图，提示词要求只借用面部身份；衣着、发型、姿势与背景仍由角色描述和画风决定。使用有足够参考槽的图像工作流；若同时使用既有资产图作为另一张参考图，需要两个槽。模型对脸部相似度和参考范围的遵循仍须人工核对。每个造型是独立资产，可分别绑定，也可在配置中按原文称谓共享同一照片：

```json
{
  "media": {
    "character_face_references": {"角色原文称谓": "../references/portrait.jpg"},
    "prop_size": 1024
  }
}
```

配置中的人物名按资产 `name` 或英文逗号分隔的原文 `aliases` 精确匹配，路径相对于配置文件目录。多个不同路径命中同一资产时会报错；配置优先于资产关联。照片会复制到资产库并记录哈希；一旦运行选定参考图或明确无参考，续跑沿用该选择。更换脸部参考后请新建运行，避免影响进行中的结果。MCP 只允许工作区内照片。

发布契约要求 1:1 的物品图使用 `prop_size × prop_size`，默认 1024×1024；可配置为其他正数且为 64 的倍数。旧提示词版本继续使用其原有资产尺寸，角色和场景仍使用 `asset_width`、`asset_height`。

## 数据库与迁移

资产目录使用 SQLite 数据库，数据库访问通过 SQLAlchemy，表结构由 Alembic 迁移管理。打开资产库会初始化新库，并检查或迁移已有库；兼容的旧版无版本资产库会在校验表结构后接管。迁移已有数据前，应停止相关运行并备份数据库和托管文件。

```powershell
novel-pipeline db --config configs/pipeline.local.json info
novel-pipeline db --config configs/pipeline.local.json migrate
```

SQLite 引擎版本取决于 Python 使用的底层运行库。资产库会根据该版本选择日志模式：已包含 [WAL 并发问题修复](https://www.sqlite.org/wal.html#walreset) 的版本可以使用 WAL；受影响的版本使用 DELETE 回滚日志。该选择不会升级 Python 或 SQLite。生产环境仍应使用包含修复的 SQLite 版本。

## 远端任务与恢复

`--render` 按选定资产版本复用或生成设定板，再生成每镜分镜图片，并按该镜的 I2VA 或 Ref2VA 模式上传素材、提交视频和下载输出。图片最多10参考；Ref2VA 的分镜参考图之外最多8图片和3音频。生成时长由解析结果绑定到工作流数值节点，末尾中文时长行不传给视频文本编码器。

```powershell
novel-pipeline inspect runs/chapter-001
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --resume --render
```

远端任务 ID 保存后，超时或下载中断可续查同一任务，不重复提交。若 POST 已发出但响应丢失，无法安全判断是否入队，状态会保留 `submitting` 并停止。先在远端队列查找已提交任务，再记录正确 ID：

```powershell
novel-pipeline attach-job --run runs/chapter-001 --key panel_0_video --prompt-id '<远端任务ID>'
```

随后续跑。程序不会调用全局 interrupt 终止别人的任务。运行目录使用跨进程文件锁。使用本机系统锁的环境中，持锁进程结束会释放锁；锁文件本身可能保留，不能仅凭文件存在判断任务是否运行。

模型因 token 上限截断时会停止并保留已完成节点，不会将残缺记录当作成功，也不会自动重拆批次。需要增加模型输出上限或减小 `batch_size` 时，请调整配置后使用新的运行目录。

默认忽略 `runs/`、`.tmp/`、`prompts/src/styles/` 和 `.env`，但模型输入和原始响应会保存在本地运行目录；请按素材敏感程度管理这些目录。默认 SQLite 资产库及 ComfyUI 归属记录位于 `runs/library/`。
