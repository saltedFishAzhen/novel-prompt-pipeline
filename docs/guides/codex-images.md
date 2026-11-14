# Codex 图片任务

默认图片后端为 `codex`。管线准备提示词、参考图和尺寸意图，并在需要图片时返回 `awaiting_image`；Codex 使用当前会话的 `imagegen` 技能生成或编辑图片。Python 不直接调用会话工具，也不要求为该任务配置 Images API。

资产事实描述、资产板和首帧生图提示词以及内置画风使用中文。

先运行到 `prompts`，然后使用 `--resume --stop-after assets --render` 准备资产图，或使用 `--resume --stop-after frames --render` 推进分镜图。使用 `novel-pipeline images list` 和 `images show` 查看任务。每个 cut 需要一张分镜图；视频提交前须接受全部分镜图。

同一批中无依赖的任务可以分别交给宿主处理。明确以 `Reuses base item` 指定基准的物品变体，等待对应基准图验收后进入后续批次；不把其他物品或场景无依据地当作它的参考。Codex 读取每项任务的提示词、参考图顺序及各图职责；对本地参考图先查看，再按任务内容调用内置图片工具。管线负责准备与回收任务，不会在 Python 中自动调用 Codex 会话工具。宽高是画幅与尺寸意图，实际返回尺寸由图片工具决定；结果记录实际宽高，不会为了匹配意图而拉伸原图。

首帧文字应独立写清本切出镜者的站坐、左右手、物件位置和动作起点。执行说明只约束本切出镜者，不额外提及其他角色；单人镜头的视线对象可以留在画外。参考板用于身份与造型，本镜的起始状态优先于参考板示例，例如空杯不能复制参考图的水位。核对特写是否真正收紧、人物是否误落座，以及金额和证件文字是否正确。

局部问题先查看最新候选，再以其作为编辑目标，只修正错误并保留其余内容；实际调用记录应包括新增编辑目标的路径与哈希。失败候选保留版本，只有目视复核通过的结果才完成任务。

生成后查看原图。资产板须核对主体、状态及版面，特别是场景和物品板的主格边界；程序不能自动判断画面是否符合版式。确认合格后，将文件放进工作区并记录观察：

```powershell
novel-pipeline images complete runs/chapter-001 '<task_id>' --file runs/chapter-001/incoming/board.png --notes '已查看：主格与细节位置、对象状态符合任务' --accept
novel-pipeline run --input C:/novels/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --resume --stop-after frames --render
```

完成操作要求显式 `--accept` 与非空观察说明，表示操作者已经查看并接受该图，不是自动视觉检查。不合格图片保留在操作者提供的位置，任务继续等待；同一任务已接受的结果不可被不同内容覆盖。重复导入相同内容是幂等的。

场景或物品板若未按预期比例排版，可在**首次验收时**用 `--main-region LEFT TOP RIGHT BOTTOM` 指定人工观察到的主格像素边界。坐标以图片按 EXIF 方向正常显示后的左上角为 `(0, 0)`，左、上包含在区域内，右、下不包含在区域内，四个数必须为整数且构成位于实际图片尺寸内的非空矩形。比如下方坐标只用于示意，应按具体图片的实际边界替换：

```powershell
novel-pipeline images complete runs/chapter-001 '<task_id>' --file runs/chapter-001/incoming/board.png --notes '已检查主格及参考一致性' --main-region 20 30 1100 620 --accept
```

验收结果保存观察的边界和显示尺寸；原图保持不变，续跑时从该区域裁出下游参考。未传入时沿用左上约 72% 宽、70% 高的默认裁切范围，仍应确认主格确实与此范围匹配。已接受任务不能事后更换或补填不同边界，应新建运行；重复导入同一图时可以省略已记录的边界。人物板和分镜首帧不使用此参数。

任务位于 `runs/<运行>/image_tasks/<任务ID>/`：`request.json` 和 `prompt.txt` 固定请求，`references/` 固定有序参考图；接受后 `result.json` 与 `results/` 保存复核记录和原图。任务 ID 绑定请求内容，续跑会核验提示词、参考图与结果哈希；改变输入应创建新运行。实际敏感素材留在被 Git 忽略的运行目录。

完整资产板先进入资产库。人物板作为人物参考；场景和物品板另外从主格裁出下游参考，保留完整板和原始尺寸。由实际图片决定的裁切会受生成版面偏差影响，故完成前必须目视检查。下游分镜首帧也是 Codex 图片任务；`stop_after="frames"` 在本轮首帧回收后停止，不提交视频。省略该阶段约束继续完整流程时，视频任务才交给配置的 ComfyUI 服务器。

MCP 提供 `image_plan_get`、`image_tasks_list`、`image_task_get`、`image_task_complete`；后者须传 `accepted=true` 和 `notes`，可选 `main_region` 整数数组按相同坐标约定指定主格。MCP 的运行目录及完成图片均为工作区内相对路径；本地 CLI 可使用有访问权限的显式路径。详见 [MCP 接口](mcp.md)。

图片与视频参考数量、状态及任务身份详见[管线与资产库](pipeline.md)。
