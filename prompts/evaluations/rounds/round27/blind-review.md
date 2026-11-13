# Round 27：8 对匿名文本比较

仅依据 [blind-input.json](blind-input.json) 各案例自己的输入和 criteria 判断，未读映射、baseline/candidate 目录或源提示词，未调用 LLM/ComfyUI。审查者此前已审阅本轮默认 16 节点输出，因此本次不称为严格首次接触双盲；下列判断不猜测 X/Y 来源。

判定比较的是文本中视角变换的落实程度，不是字数或禁止项数量；判优也不意味着另一份有事实错误。未检查参考图片或成片，不推断图像生成稳定性。

| 案例 | 判定 | X 的证据与问题 | Y 的证据与问题 |
| --- | --- | --- | --- |
| reverse-opposed-entrance-window | **Y 略好** | 明确 `“entrance wall now forms the background”`、窗墙在新相机后，物件世界位置固定，核心正确；凳子及左右关系仅用 `“apparent left and right may change”` 概括。无明确补造。 | 把已知关系具体重投影为 `“bench ... right”`、`“cabinet ... left”`，窗边凳子成为近景，均可由输入和反打推出；重建表面而非镜像，不补新入口细节。无明确内容问题。 |
| overview-opposed-entrance-window | **Y 更好** | 45° 和侧面/透视明确，保留入口与窗相对关系；但没有直接要求地面、三件家具顶面同时可见，也未直接处理入口遮挡。不是事实矛盾，主要是目标表达较弱。 | `“substantial workshop floor, object tops and sides”`，并落实到 bench top、cabinet top、stool seat；越过入口遮挡、保留原门口，切屋顶仅为观看。顶/侧/地面和原家具靠墙关系都明确，未新增门窗或内容物。 |
| reverse-outdoor-path | **Y 更好** | 有 180° 反打及固定井/桥/桶/柳树，但新背景仍写抽象的 `“existing boundary behind the original camera”`，未明确桥侧机位和井侧背景。这里不把 boundary 夸读成新增室内墙。 | 直接从桥旁朝井侧看，`“well in the background”`、桥为近景、柳树在新相机后、桶仍在远处井旁，逐项符合输入。保留露天，没有屋顶、墙或新地标。 |
| overview-outdoor-path | **X 更好** | 明确可见 substantial ground、井上沿及占地、桥面、侧面和透视；桶/井与柳树/桥的关系正确，明确保持露天。`“any original foreground obstruction”` 是条件性处理，未指定实际存在的新障碍。 | 45°、侧面、空间关系均正确，无新增室内或屋顶；顶面与大面积地面主要依赖俯视角度隐含产生，未像 X 一样明确井沿、桥面等可见目标。 |
| reverse-undetermined-relations | **相当** | 明确反向看回原机位、看物件另一面；椅、圆桌、桌上书保留，近远关系由参考重投影，不强制全入画。没有补家具坐标、门窗或方向。 | 明确反向背景及固定世界布置，以 `“Preserve the established scene state”` 保留既有物件状态，没有指定未给定的左右、近远或建筑细节。X 更具物件针对性，但两者均满足本案，未见足以判胜负的事实差异。 |
| overview-undetermined-relations | **X 更好** | 明确 substantial floor、椅上表面、圆桌顶和其上的书，以及可见侧面；没有给家具虚构坐标。`“any original doorway or foreground obstruction”` 按条件性处理理解，不能当成已知有门；下游应仅对实际参考中的遮挡应用此句。 | 保留状态且不补布局，45° 与侧面正确；没有直接落实地面、家具顶面和桌上书的可见性。X 的优势来自这些已知物件的正向可见目标，不来自多写门口或屋顶措辞。 |
| reverse-light-anchor | **相当** | 入口侧新背景、原远墙在镜头后；窗的已知位置、下午光和椅影均保留，未重写阴影几何或加灯。 | 同样正确反打；椅在地毯上、窗在入口左侧物理墙面及已有光影保留，增加另一面可见性的描述。两者均满足本案，未见光源漂移或未知墙面补造。 |
| overview-light-anchor | **Y 更好** | 45°、侧面与已知窗光/椅影锚定正确，不补远墙细节；地面与椅顶面没有直接指定。 | 明确 substantial floor、椅上表面及地毯占地关系、可见侧面与透视，入口不遮挡；保留既有窗、下午光和斜椅影，不补远墙细节或新阴影。更完整落实本案可见范围。 |

本组为 **Y 更好 4 对、X 更好 2 对、相当 2 对**，其中第一对仅小幅判优。没有发现可据此认定的重大空间矛盾或明确新增门窗。较弱项多是使用通用变换/保留指令，没有把 criteria 要求的已知物件顶面、地面或新近远关系直接写清；不把这些表达差异当成已观察到的渲染失败。

报告至此冻结；解盲与发布由主代理处理。
