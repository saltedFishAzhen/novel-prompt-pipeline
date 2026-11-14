# 角色声音与对白

`cast.json` 的 `voice` 字段描述角色声音，供声音设计和 Breeze 合成参考。声音描述应区分原文依据和设计选择；实际声线须试听确认。H3 视频不传角色参考音，参考音用于 Breeze 后期合成。

## Breeze TTS

项目提供独立的 Breeze TTS HTTP 接口，依据 [Breeze TTS 官方本地 API](https://github.com/breezeblue-ai/breeze-tts/blob/daa6cb2efe5471f09369229f322d20bb956cb3c3/README.md)实现。使用者须准备服务和模型权重，并核对实际部署的 API。真实 Breeze 服务与声线效果尚未完成端到端验证。

在工作区 `.env` 设置 `BREEZE_TTS_URL`；如鉴权代理需要 Bearer 密钥，可设置 `BREEZE_TTS_API_KEY`。不要将密钥写入请求或版本库。可用 `novel-pipeline tts --help` 查看检查与合成参数。

## 后期对白来源

后期以运行生成的 `dialogue-source.json` 为对白来源。它由 `dialogue_source(run)` 将剧本和分镜确定性投影为 segment 编号、文字及对白字符偏移，供逐句计划引用。`panel_index` 从 1 开始，表示 segment 编号；时长可为小数秒，例如 2.5 秒。逐句计划应逐字对齐来源并核对时间、角色和声线；合成与混音后的结果须人工试听和验收。

处理流程及验证限制见[音频后期方案](../architecture/audio-postproduction.md)。
