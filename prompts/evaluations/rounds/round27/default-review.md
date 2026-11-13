# Round 27 default：16 节点语义审查

依据仅为 [cases.jsonl](default/cases.jsonl) 与 [results.jsonl](default/results.jsonl) 的输入和 `raw_output`，逐项审查全部 16 份结果。结果版本统一为 `aeaf94c652ad8d1ae284326457bee89793a0d928b22958cc21b01a9e1765a74e`。未读源提示词，未调用外部 LLM 或 ComfyUI，未检查生成媒体；仅新增本报告。

未发现 P1/P2 问题。性别未知、揭示顺序、资产绑定及新反打/斜俯视的核心语义均保留；有两处 P3 无依据的空间具体化，不能将结构 16/16 直接表述为语义无问题。

## 发现

1. **P3：斜俯视额外限定散落衣物的位置。** [结果第 10 行](default/results.jsonl#L10) 写 `“garments on the floor between established footprints”`；[同号输入](default/cases.jsonl#L10) 只说 `“a few garments lie on the concrete floor”`，没有交代它们相对家具占地的位置。若参考中的衣物位于床下、桌下或墙边，该短语可能把它们改放到家具占地之间，与后文保持所有位置的要求冲突。此处 footprints 按物件占地理解，不判为新增脚印。建议保留“原位置地面上的衣物”，删除 between established footprints。未检查参考图片，不声称实际发生移位。
2. **P3：开门分镜把铰链侧推导成侧墙邻接。** [结果第 14 行](default/results.jsonl#L14) 写门打开后 `“beside the left interior wall”`；[同号输入](default/cases.jsonl#L14) 只给走廊视点左门框铰链及向内开门，没有给入口在墙上的横向位置、门框到侧墙的距离。入口居中时，左铰链门可向内开而不贴近左侧内墙，因此该邻接关系不能从铰链方向推出。建议改为“门在铰链侧保持开启、通道畅通”，不指定侧墙。右手接触、释放和过槛动作本身没有因此被写错，不列为重大接触矛盾。

## 逐项结论

下列行号同时对应输入与结果中的同一案例，链接指向结果。

| 行 | 节点 | 语义核对 |
| ---: | --- | --- |
| [1](default/results.jsonl#L1) | asset.character | 许青两版均为 `“The person is twenty-six years old”`，没有补男女性别或性别代词。疤痕左侧、肩部红印右侧、发色、工装及白衬衫湿态有据。肖像、正侧背及三个细节格完整。 |
| [2](default/results.jsonl#L2) | asset.prop | 铜钥匙、红绳、亮断面、菜刀缺口/铁丝及搪瓷缸三处掉瓷/铅笔均有据；断裂项仅取断面与邻接铜材，未猜哪半留锁或属于手柄。 |
| [3](default/results.jsonl#L3) | asset.scene | 默认/积水/退水状态清楚：电脑桌面→窗帘遮半→裸床板塑料袋内，被子浸水→院子晾晒；院子两个蓝桶、三只黄鸡正确。夜景未补造灯具或月光。 |
| [4](default/results.jsonl#L4) | asset_binding | 精确为整段 `[box,zhou,key]`、首帧 `[box]`；后入画周予和后显露钥匙不进首帧，始终画外的陈远不绑定为可见角色。 |
| [5](default/results.jsonl#L5) | context_summary | 两个字符串均区分读者已知弟弟身份、林知未知；车内暗、看不清脸、仍在车外、尚未开口保留。未加旁白机制或“原文未说明”等多余尾注。 |
| [6](default/results.jsonl#L6) | continuity | 夜间暗窗与已亮台灯重锚正确，灯的具体位置保留未知；窗边朝入口的机位变化未当成人物/光源移动。明确仅为文本状态，未声称观察实际末帧。 |
| [7](default/results.jsonl#L7) | novel_to_script | 乔宁知道、陆川不知道；“先别开”→收手→稍后开盒→看清缺角，顺序及原话保留。没有新增“旁白交代”。 |
| [8](default/results.jsonl#L8) | rewrite | 保留未说出口的怀疑及“先放这里。”；阿远仍不知情，没有借后文把怀疑升级为证实。 |
| [9](default/results.jsonl#L9) | rewrite_batch | 编号顺序 `9,2,17` 保持；未拆信、没认出字迹、未讲话，明天询问仍为意图，没有提前认出母亲。 |
| [10](default/results.jsonl#L10) | scene_overview | 核心方向正确：约 45°，明确 substantial concrete floor、object tops、sides、readable depth，不保留平视地平线；屋顶切除仅作呈现，未补门窗。保留入口/窗/床/桌关系与既有窗光。另有上述衣物位置 P3。 |
| [11](default/results.jsonl#L11) | scene_reverse_view | 入口侧边界成为新背景、窗在新相机后方；床改为右前景、桌改为左侧符合原视点左右关系。明确重建透视而非镜像/裁切/保留原镜头，物件世界位置固定；新露普通表面只续接既有材料，未补门窗、房间或家具位置。 |
| [12](default/results.jsonl#L12) | scene_topdown | 垂直光轴、无透视收敛、顶面占主导、墙仅薄边；参考顺序 1 主图/2 反打/3 斜俯视明确。重投影相对方向而非搬物，窗光固定在场景空间。 |
| [13](default/results.jsonl#L13) | shot_plan | 两条均用 i2va 符合输入已有首帧且无额外参考需求的条件；输入未要求两模式各用一次，不因案例名 two-modes 判错。 |
| [14](default/results.jsonl#L14) | storyboard | 首帧闭门遮住室内，右手已搭把手、双脚在走廊；同一右手压把推门、释放后入室，另一手自由，接触与门槛要求保留。Picture 1 和六段齐全；另有上述侧墙邻接 P3。 |
| [15](default/results.jsonl#L15) | storyboard_batch | 三记录均有 Picture 1 与六段。反打 Lina 仍靠窗、Noah 仍靠入口，桌只改变屏幕侧；窗在相机后，未搬光源。夜景明确新台灯唯一照明，暗窗无日光/月光。未增加走位或手势。 |
| [16](default/results.jsonl#L16) | storyboard_i2va_batch | 固定首句及三段齐全；第一人称只看右手、门把和门框，不显示自己的脸或穿闭门看到室内。单次按下、回位、释放，门始终关闭，镜头固定。 |

## 英文及验证边界

三类资产 description 为英文，复用指示后保留原始中文资产名，不属于把描述改成中文；按空白分词为 48–362 词。三个场景变换均为单行英文，overview/reverse/topdown 分别 108/113/118 词，表达直接，没有大段教程或新增文字标注。视频均未新增额外 refs、负向段、对白或配乐。

本审查只覆盖本组 16 个文本样本，不能证明其他采样、真实图片中的遮挡与几何、实际参考绑定或视频物理稳定。上述两处 P3 是文本额外设定，不是成片观察；是否作为本次发布的阻断项由主代理结合门禁范围决定。
