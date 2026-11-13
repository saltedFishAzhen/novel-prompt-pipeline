# 动漫 CG 画风验证

当前唯一内置画风 `cinematic_anime_cg` 是新工作区、CLI、MCP 和 Python SDK 的默认值。它通过文字提供前置、后置和图片负向词，不自动挂载画风参考图；视频只使用前置和后置词。画风文本可在配置中覆盖，修改后需要新建运行。

## 已有生图记录

用户选定的英文镜头纯文字结果保存在本机 `runs/style-evaluations/semi-realistic-anime-cg/restrained_romance_cg_v5-target-v5-en/43_ComfyUI_00036.png`。该次 Qwen 生图为 768×1344、种子 27101，`reference_images=[]`。选定图片和实际提示词、参数的私有记录位于 `prompts/src/styles/cinematic-anime-cg/`，不随包分发，也不自动挂载为参考图；内置画风文字在 `src/pipeline/styles.py`。

后续按用户对参考图的要求调整了脸部细节、材质、对比和暖色胶片分级。最后一次单变量结果为 `runs/style-evaluations/semi-realistic-anime-cg/reference_match_v3/43_ComfyUI_00040.png`，沿用相同镜头文本、尺寸、种子和空参考图列表；运行记录保存了实际提示词和工作流参数。该次画面暗部更实、逆光和暖色调更明显，但背景散景、人物刘海和构图仍受镜头文本与分辨率限制。

当前内置文本是该轮调优后的版本，不等同于最初选定图片的提示词。此前证据来自单张校园黄昏镜头，不能证明冷光、室内、其他人物或视频中的效果稳定；具体成图仍需审查。

当前默认画风另完成一次不挂参考图的人物生图，结果保存在本地 `runs/style-evaluations/current-default/outputs/43_ComfyUI_00045.png`。该次运行证明工作流完成出图，单次结果不足以证明画风稳定性或相对此前结果有所提升。
