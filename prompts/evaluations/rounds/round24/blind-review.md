# Round 24 匿名对照审查

审查范围仅为 `blind-input.json` 中的原始输入、节点名及匿名 X/Y 完整输出。未读取提示词源码、其他报告、映射或 Git 差异；未请求 LLM/GPU，也未检查生成图片或视频。下述结论只评价文稿，不证明实际成片的空间、接触或光照稳定。

## 逐组判定

| 案例 | 判定 | 差异等级 | 主要依据 |
| --- | --- | --- | --- |
| single-contact-ref | 相当 | 无明显差异 | 两者均保留右手接触、左铰链、门内开、先释放再过门槛的动作链。 |
| reverse-and-time-ref | Y 更好 | 重大契约缺失 | X 三个视频记录均无 `<Picture 1>`；Y 三个记录均定义并使用首帧。人物及昼夜光源的主干连续性两者均合理。 |
| first-person-empty-i2va | Y 更好 | 重大可见性矛盾 | X 一边写关闭木门，一边写门外可见 Workshop 室内；Y 将窗和家具留在向下视野之外。 |
| continuity-light-reset | X 更好，小幅 | 小幅无据细节 | Y 把未明确摆放位置的 table lamp 写成“桌上亮着的台灯”；X 保留位置未知。 |
| scene_reverse_view | 相当 | 无明显差异 | 两者均把入口墙置于背景、窗墙留在新机位后方，并保持世界空间布置。 |
| scene_overview | 相当 | 无明显差异 | 两者均为约 45° 斜俯视，保留侧面、原布局及光照，没有明确空间冲突。 |
| scene_topdown | 相当 | 无明显差异 | 两者均规定垂直向下、墙体仅边缘、三图参考顺序及物品布置保留；各有不同明确化，但无证据证明一方更优。 |

## 按重要性排序的问题

### 1. X 的第一人称近景同时要求闭门与穿门可见室内

证据：[blind-input.json 第 52 行](blind-input.json#L52)，`first-person-empty-i2va`，X 的首帧写 `“closed wooden Workshop door”`，随后又写 `“the Workshop interior is visible beyond the door”`，并指定可见远墙窗及其右侧桌子。视频正文也把这些室内物件列为持续可见的 `“fixed spatial anchors”`。

原始输入要求视线向下看右手与门把，门始终关闭，且不能切镜或移机。未提供透明门或其他穿透视线的条件。X 把不可同时满足的背景可见性带进了首帧和整段动作，可能要求生成器透视木门或改变门状态。应仅保留手、门把、接触和门面，已知但不可见的室内布置不应硬塞进画面。

Y 的对应证据为第 53 行：`“the window and furniture lie outside the downward framing”`、`“The door stays fully closed throughout”`。两者都明确排除了 Lina 自己的脸；本组优劣来自闭门与视野的一致性，不是 Y 更长。

### 2. X 的 Ref2VA 批量输出全部漏掉 Picture 1

证据：[blind-input.json 第 34 行](blind-input.json#L34)，`reverse-and-time-ref`，X 的三个 `subject_definitions` 直接从 `<Subject 1>` 开始；完整三个视频记录中 `<Picture 1>` 的出现次数均为 0。三个记录均保留了六个规定 section，因此问题是首帧引用缺失，不是 section 数量不足。

`reference_capacity=0` 表示没有额外参考图，不能据此省略当前记录的首帧。X 使用 `“matching the opening composition”` 等普通文字，未履行 Picture 1 的明确引用契约。修复方向是在每个记录的 subject definitions、保留分析及动作正文中按实际首帧建立 Picture 1 关系。

Y 的对应证据为第 35 行，每个记录均有 `“<Picture 1> provides the opening composition reference”` 及正文 `“Using <Picture 1>”`；没有增加 Picture 2 或额外音频参考。

### 3. Y 的 continuity 摘要补出了未确定的台灯摆放位置

证据：[blind-input.json 第 65 行](blind-input.json#L65)，`continuity-light-reset`，Y 写 `“桌上亮着的台灯照亮Lina的脸”`。本案例输入仅说 `“the newly stated table lamp lights her face”`，以及 Lina 在桌旁、看向台灯；table lamp 是灯的类别，并未明确说它摆在那张桌上。

这是小幅无据细节，不足以称为重大光源搬家。若摘要作为下批状态传递，它会把一种合理猜测升级为已确定位置。应保留“亮着的台灯”，并将具体摆放位置标为未提供。X 第 64 行的 `“台灯具体摆放位置未提供”` 更忠实于输入。

## 各组其余核对

### single-contact-ref：相当

X：`“The closed door conceals the workshop interior”`、`“Keeping the same right-hand contact”`、`“she releases the lever naturally”`，随后跨门槛。Y 同样先压把手、以原右手推门、门足够开后释放，再穿过 `“clear right side of the opening”`。左铰链与向内开门不冲突。

两者首帧均为动作开始前右手已搭门把、双脚在走廊；均将镜头放在右后侧，明确要求接触和门槛可读。X 明写肩肘腕手，Y 的完整身体和右侧取景也覆盖主要接触。文字没有提供足够差异来断言哪份实际遮挡更少。两者均有六 section 和 Picture 1，未添加额外 refs、负向视频段、对白或配乐。

### reverse-and-time-ref：除契约缺失外，主干相当

两者反打均保持 Lina 在靠窗位置、Noah 在靠入口位置；仅随反打变成 Lina 离镜头较近。X：`“The window is behind the camera and outside the frame”`；Y 同义表述。桌子随视点从右侧变到左侧，未写成物理搬动。反打中的犹豫限于眼神、眉眼和面部张力，没有另加走位或手势。

两者夜景均把窗变暗，并把新引入的桌上台灯作为唯一光源；未把白天日光带入夜景。X 另写灯的 `“shade and warm pool of light”`，其中灯罩和暖色是原输入没有指定的小幅外观细节，优先级低于 Picture 1 缺失。Y 夜景 summary 的 `“lamp beneath a dark window”` 可改成仅说台灯位于既定桌上，以免读者误把它理解为窗下定位；同一记录其余正文仍明确桌在窗右侧墙边，故这里仅列措辞改进，不判定已发生光源搬家。

### first-person-empty-i2va：格式主干均通过

两者都使用固定首句 `“For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.”`，并包含 `integrated_multimodal_description`、`overall_soundscape`、`non_diegetic_music` 三段。均为一次右手压下后释放、门不打开、固定 POV，没有自身脸部入镜，也未虚构额外 refs 或添加负向段。Y 还明确 `“releases contact with her hand remaining nearby”`；X 的 `“without ... leaving the contact area”` 稍含混，但不足以独立判为另一个重大动作错误。

### continuity-light-reset：证据边界均保留

两者均可解析为只有 `summary` 字段的 JSON。均将昼转夜和窗边反打作为明确输入更新，没有因连续性强行恢复白天，也没有把镜头变化说成人物或光源移动。X：`“以上仅为文案所述状态”`；Y：`“以上状态依据分镜及视频提示词正文”`，均明确未提供生成视频、实际末帧无法确认。除上述灯位置外，没有足够证据区分优劣。

### scene_reverse_view：相当

X：`“the original window wall is behind the new camera”`；Y：`“the window wall is behind the new camera”`。两者均维持物品世界位置，并允许视点相关左右改变；既有窗光得到保留。Y 额外明确不新增光源和阴影细节，X 已要求保留 window daylight、不要新增物件；未见 X 实际引入新灯或让窗口搬家，不能仅因 Y 多写限制就认定它更好。

### scene_overview：相当

两者都有 `“look down at approximately 45 degrees”` 和保留 perspective/side surfaces，避免与垂直俯视混淆。X 明确 `“Keep the window in its established location”`；Y 保留 `“entrance-to-window orientation and bed-left, table-right relationships”`，后文也要求保留 world-space layout。两者没有把床和桌的原始左右改成未锚定的新视点坐标。屋顶移除都仅为呈现用剖切，不是新增毁坏情节。

### scene_topdown：相当

两者均要求光轴与地面严格垂直，没有地平线、消失点或透视收敛，墙只作为薄边。X 明确 `“when facing the window from the entrance”` 来锚定床左桌右；Y 要求 `“Preserve their common layout”` 并 `“Reproject screen-relative directions”`。Y 进一步明确窗光固定在场景中，X 重点保留物件和当前状态；未见任何一份实际改写光源位置或窗户数量，故不凭显式限制的多寡拉开评分。

## 长度、语言和检查边界

以下按空白分词，都是单行：

| 节点 | X 词数 | Y 词数 | 要求 |
| --- | ---: | ---: | --- |
| scene_reverse_view | 105 | 107 | 70–120 |
| scene_overview | 111 | 99 | 70–120 |
| scene_topdown | 126 | 122 | 80–130 |

六份均在目标范围内。它们使用直接的镜头变换指令，部分否定项较密，但未发展成解释教程，也无额外 LOCK/检查清单段；没有语言流畅度方面足以改变组内判定的差异。分镜输出中的 section 是规定契约，不能仅因其结构化而扣分。

本审查未获得或检查实际参考图片、生成首帧、视频和音频，因此不判断真实画面中的遮挡、光线投射、动作时序或物理稳定性。场景节点所写 image 1/2/3 的实际绑定也不在本次匿名文本材料中，未据此猜测引用链是否正确。
