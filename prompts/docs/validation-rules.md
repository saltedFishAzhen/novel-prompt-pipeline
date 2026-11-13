# 校验规则与边界

节点输入变量和输出协议以 [节点注册表](../src/nodes/index.json)、资产模式源码及发布 manifest 为准。生产校验实现位于 [contracts.py](../runtime/prompt_project/contracts.py) 和 [validators/](../runtime/prompt_project/validators/)；运行入口见[提示词指南](../../docs/guides/prompt-project.md)。

节点的格式检查包括注册的 JSON 字段、记录分隔和必要约束。资产输出为包含 `name`、`aliases`、`description` 的记录；生产契约还要求名称唯一。资产校验对名称来源、长度、Reuses、状态段以及身份锚点等做规则或启发式检查；来源检查需要提供原文。角色的第一人称「我」有专门处理规则，不能套用场景或物品的命名限制。

场景和物品的资产描述分别要求单视角画面，不要求生成拼版或细节格；角色仍使用多视图与细节条。各模式目标词数见 [modes/](../src/modes/)，450 词为资产描述硬上限；稀疏原文无需扩写凑数。

结构通过只说明已实现的协议检查通过，不能证明事实忠实、共享骨架逐字一致，或生成画面、动作符合要求。名称来源检测和锚点比较均为启发式，可能漏报或误报；需结合原文、模型输出和实际媒体复核，参见[已知问题](known-issues.md)。
