# 统一 16 节点真实评测语义审查

审查对象：`20261113T165837.530729Z-ba85ad15e074`。

依据为该运行原样归档的 [cases.jsonl](../evaluations/rounds/round26/initial/cases.jsonl) 与 [results.jsonl](../evaluations/rounds/round26/initial/results.jsonl)；下文行号同时对应两份 JSONL 的同一案例，证据取自结果的 `raw_output`。另遵循 `prompts/AGENTS.md` 的事实忠实、避免无依据推断和元说明要求。未读取提示词实现，未重新调用模型或生成媒体。

结构检查确为 16/16 通过，但不能据此判定语义全部通过。本次发现一处需修复的身份补造，另有两处轻微元说明。未发现明确的提前揭示、错误资产绑定或闭门 POV 矛盾。

后续状态（最终复审前的进度记录）：主代理已报告角色修复后的独立真实 LLM 测试通过，许青性别未知得到保留；两处元说明已安排修复，当时最终全 16 节点真实复测进行中。下文保留初次评测的问题及证据；本审查者随后完成的最终复审结果见文末 Final 段，不以修复结果覆盖历史记录。

## 需修复的问题

**P2：asset.character 无依据指定许青为男性。** [results 第 1 行](../evaluations/rounds/round26/initial/results.jsonl#L1)，`默认的许青`、`穿白衬衫的许青` 两项均写 `“The man is twenty-six”`，后续继续使用 he/his。对应原文仅以“我叫许青，二十六岁”起笔，提供体型、肤色、头发、疤痕和服装，没有明确许青性别。工装、衬衫等不能作为男性身份的依据。影响是生成角色的身份外观被额外固定，且错误跨造型复用。应使用不补性别的成人表述，保留已明确的年龄和外貌。此问题已单独通知主代理。

## 轻微改进项

- **novel_to_script，第 7 行：** 新增“旁白交代，乔宁知道里面是一枚旧纽扣”。原文已直接叙述这一知识差异，因此这里没有把陆川写成提前知情，也不能判为提前泄露原文未披露的内容；问题是额外指定了“旁白交代”的呈现机制。直接保留“乔宁知道……陆川不知道”即可，减少下游把叙述知识变成显式旁白的歧义。
- **context_summary，第 5 行：** chapter 的“原文以内心打算说明她想先问……”是可删除的元说明，可直接写“她打算先问……”。story 末尾的“除此之外的世界设定及后续发展，原文未说明”也未增加当前状态信息。两字段的事实和知情边界均正确，这些属于简洁性改进，不与身份补造同级。

## 逐节点核对

| 行 | 节点 / 案例 | 语义结论与证据 |
| ---: | --- | --- |
| 1 | asset.character / asset-character-source | 有上述 P2。其余可核对外貌基本有据：左眉骨旧疤、枯黄参差发尾、工装与白衬衫湿态、右肩红印均对应原文；肖像、正侧背和三个细节格完整。 |
| 2 | asset.prop / asset-prop-source | 未见明确错误。完整钥匙、断裂状态、菜刀和搪瓷缸均有据。断裂项仅取新鲜亮断面及相邻铜材，未编造哪半属于手柄、齿端或锁内部分；杯内铅笔来自原文。 |
| 3 | asset.scene / asset-scene-source | 未见重大状态混淆。默认桌上电脑、暴雨夜窗帘盖住电脑、次晨电脑移到裸床板并套塑料袋得到区分；院子水井、两个蓝桶、三只黄鸡有据。没有增加夜景灯或月光来解释可见性。 |
| 4 | asset_binding / binding-hidden-key | 精确为 `asset_ids=[box,zhou,key]`、`first_frame_asset_ids=[box]`。后来入画的周予和开盒后可见钥匙只进整段数组；始终画外的陈远排除，未把声音转成可见人物。 |
| 5 | context_summary / summary-reader-vs-character | 有上述轻微元说明。明确“叙述者……向读者揭示”而“林知……并不知道”；车内暗、看不清脸、仍在车外、尚未开口均保留，未把打算写成已问姓名。 |
| 6 | continuity / continuity-light-reset | 未见明确错误。“台灯确切摆放位置未提供”避免补定位；保留夜间、暗窗、窗边朝入口机位，明确视向变化不代表人物或光源移动，并声明“未提供实际生成视频，无法确认成片末帧”。 |
| 7 | novel_to_script / script-delayed-reveal | 有上述轻微“旁白交代”新增。核心顺序正确：“先别开”→收手→稍后开盒→“这才看清……缺角”；开盒前明确陆川不知道。 |
| 8 | rewrite / rewrite-unspoken-suspicion | 未见明确错误。仍为“怀疑”且“没有说出自己的怀疑”；“先放这里。”及说话人保留，阿远仍不知道。未借后文把怀疑变成证实。 |
| 9 | rewrite_batch / rewrite-batch-spoiler | 未见明确错误。编号顺序 `9,2,17` 保留；没认出字迹、未拆信、没说话；“心想等明天再问”仍是意图，没有提前确认母亲身份。 |
| 10 | scene_overview | 单行英文，111 词。约 45° 斜俯视，保留侧面和透视；保持窗位置、入口/床/桌世界关系及既有日光，不引入新光照细节。 |
| 11 | scene_reverse_view | 单行英文，107 词。入口墙成为背景、窗墙在新镜头之后，允许屏幕左右改变而物理位置固定；不是把原图简单镜像。未见随机移动窗或家具的指令。 |
| 12 | scene_topdown | 单行英文，117 词。垂直光轴、墙仅薄边、无透视收敛；参考顺序 1 主图/2 反打/3 斜俯视明确，床品和袋装设备等状态保留，窗光固定在场景空间。实际参考绑定未在本次材料中验证。 |
| 13 | shot_plan / shot-plan-two-modes | 两条均选 i2va；输入允许 i2va 且声明有首帧，没有要求两种模式必须各用一次，不能因案例名称含 two-modes 判错。理由围绕连续动作和无额外多图需求，未替原剧情增加事件。 |
| 14 | storyboard / single-contact-ref | 首帧闭门遮住室内，右手已搭把手、双脚在走廊；同一右手压下并推门，左铰链向内开，释放后才过门槛。Picture 1 与六 sections 齐全，无额外 refs、配乐或负向段。 |
| 15 | storyboard_batch / reverse-and-time-ref | 三条均有 Picture 1 与六 sections。反打时 Lina 仍靠窗、Noah 仍靠入口，只改变近远及桌子屏幕侧；窗在镜头后。夜景换成已明确引入的桌上台灯唯一照明，无日光残留。灯罩和暖色是轻微外观具体化，未造成光源搬家。 |
| 16 | storyboard_i2va_batch / first-person-empty-i2va | 首帧只呈现右手、门把、门面，未把关闭木门后的室内塞进视野；脸与头在自身视野之外。单次压下、回位、抬指释放，门保持关闭；固定首句及三段齐全，无额外 refs 或负向段。 |

## 英文生图词及验证边界

三类资产的 description 均以英文写作；其中出现的中文是 `Reuses base…` 后的原始资产名称，不能据此判定生图描述改成了中文。资产描述按空白分词为 42–395 词，未超过项目 450 词硬上限；角色描述较长主要包含规定视图骨架及一致性要求，没有发现与当前事实同级的长篇教学解释。三种场景变换均为单行英文，未新增文字标签、拼贴或建筑物。

本次仅审查每节点一个真实文本样本及其输入。未验证其他采样、运行时传参、参考图片本身或生成媒体中的实际遮挡、光照与接触效果；因此不能据此宣称整个提示词版本语义全通过或物理视频稳定。

## Final：修复后全 16 节点复审

最终运行 `20261113T171530.612307Z`，版本 `4b69280b41d64b25e9f4662477a094edc482c9d64f012a6568e0f7347b9e6f2e`。证据为归档的 [final cases.jsonl](../evaluations/rounds/round26/final/cases.jsonl)、[final results.jsonl](../evaluations/rounds/round26/final/results.jsonl) 和 [run.json](../evaluations/rounds/round26/final/run.json)。已独立阅读全部 16 份最终输出并对照输入，未只复核三个修改点。

本次样本中，原 P2 和两处轻微元说明均已修正，未发现新的需修复语义问题。此结论来自逐节点文本审查；与 `run.json` 所记结构首过 16/16 是两项不同证据。

### 原问题复核

- **许青性别补造已修正：** [final results 第 1 行](../evaluations/rounds/round26/final/results.jsonl#L1) 的默认和白衬衫两版均为 `“The person is twenty-six years old”`，以 the person、the eyes、the outfit 等表述承接，没有 man/woman/male/female 或 he/his/she/her。年龄、肤色、左右侧疤痕与肩部红印、发型和各套服装仍保留，没有用删除身份细节来规避性别问题。
- **额外旁白机制已移除：** [第 7 行](../evaluations/rounds/round26/final/results.jsonl#L7) 第一记录直接保留“盒子还合着。乔宁知道里面是一枚旧纽扣，陆川不知道”，不再出现“旁白交代”。开盒后才让陆川看清缺角的顺序未变。
- **summary 元说明已移除：** [第 5 行](../evaluations/rounds/round26/final/results.jsonl#L5) 两字段直接总结场景、知识差异和未执行意图；不再出现“原文以内心打算说明”“除此之外……原文未说明”。仍明确身份由叙述者向读者揭示、林知未知，并保留车外及尚未开口状态。“叙述者向读者揭示”是本案例本来就需要保留的知情关系，不能与多余元说明混为一谈。

### 全节点回归核对

以下行号均链接最终结果，输入同号对应：

| 行 | 节点 | 最终文本核对 |
| ---: | --- | --- |
| [1](../evaluations/rounds/round26/final/results.jsonl#L1) | asset.character | 许青不补性别；人物外貌、衣着变化、肖像/正侧背/三个细节格仍在。三项 description 为 314、365、307 词，未超 450 词硬上限。 |
| [2](../evaluations/rounds/round26/final/results.jsonl#L2) | asset.prop | 红绳钥匙及断面、菜刀磨损、三处掉瓷和杯内铅笔有据；断裂项仍只描述断面及相邻铜材，没有补造断片归属。 |
| [3](../evaluations/rounds/round26/final/results.jsonl#L3) | asset.scene | 房间基态、积水夜态、退水晨态及院子区分正确；电脑从桌面到裸床板袋内、被子移至院子等变化保留，未凭空增加夜景光源。 |
| [4](../evaluations/rounds/round26/final/results.jsonl#L4) | asset_binding | 仍精确为整段 `[box,zhou,key]`、首帧 `[box]`；画外陈远排除。 |
| [5](../evaluations/rounds/round26/final/results.jsonl#L5) | context_summary | 双字符串结构与读者/人物知情边界保持，未提前认出弟弟、开口或上车；多余元说明已去除。 |
| [6](../evaluations/rounds/round26/final/results.jsonl#L6) | continuity | 夜间、无日光、窗旁朝入口的机位及台灯位置未知均保持；明确这是文本状态，未声称观察真实视频末帧。 |
| [7](../evaluations/rounds/round26/final/results.jsonl#L7) | novel_to_script | 不再增加旁白呈现机制；“先别开”→收手→稍后开盒→看清缺角的链条完整。 |
| [8](../evaluations/rounds/round26/final/results.jsonl#L8) | rewrite | 仍为未说出口的怀疑，原话“先放这里。”和说话人不变；未变成后文已证实的错送事实。 |
| [9](../evaluations/rounds/round26/final/results.jsonl#L9) | rewrite_batch | 顺序 `9,2,17` 保持；未拆信、未认出字迹、未讲话，明天询问仍是意图。 |
| [10](../evaluations/rounds/round26/final/results.jsonl#L10) | scene_overview | 单行英文 108 词；45° 斜俯视保留透视侧面、世界布局及既有窗光。 |
| [11](../evaluations/rounds/round26/final/results.jsonl#L11) | scene_reverse_view | 单行英文 112 词；入口墙在背景、窗墙在镜头后，家具不随屏幕左右改变而搬动。 |
| [12](../evaluations/rounds/round26/final/results.jsonl#L12) | scene_topdown | 单行英文 115 词；垂直无透视收敛、薄墙边、参考顺序和物件状态保持，窗光锚在场景空间。 |
| [13](../evaluations/rounds/round26/final/results.jsonl#L13) | shot_plan | 输入允许已有首帧的 i2va，两条均选择 i2va 有合理依据；未把案例名误当成必须各选一种模式的要求。 |
| [14](../evaluations/rounds/round26/final/results.jsonl#L14) | storyboard | 闭门背后室内被明确称为 concealed/hidden，未要求首帧穿门可见；同一右手压把推门、释放后过槛，左手空闲；六段与 Picture 1 完整。 |
| [15](../evaluations/rounds/round26/final/results.jsonl#L15) | storyboard_batch | 三记录均有六段及 Picture 1；反打保留两人原位置和镜头后的窗，夜景明确桌上台灯唯一照明，无白天光延续。 |
| [16](../evaluations/rounds/round26/final/results.jsonl#L16) | storyboard_i2va_batch | 固定首句与三段完整；第一人称只看右手、门把、门面及门框，脸在自身视野外；单次压下后释放，门始终关闭。 |

定向完整性核对：final 与 initial 的 `cases.jsonl` SHA-256 相同，均为 `b3ca25868afbd8fb01512dbe11d34dcc912a6b8cbbc99aa24aecf73262b39168`；final cases/results 的实算哈希分别与 run.json 一致；16 条结果的版本均匹配上述最终版本。未生成任何新评测输出或修改产品文件。

本次复审未发现值得修复的新回归。残余盲区仍是每节点仅一个输入和一次最终采样、实际参考绑定与生成媒体未验证；“该组文本未见语义错误”不等同于所有输入均稳定，也不等同于画面或视频物理效果通过。
