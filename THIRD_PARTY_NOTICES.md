# Third-party materials

The repository contains workflow configuration supplied or adapted for this
project. A ComfyUI graph is configuration: using it also requires compatible
ComfyUI software, custom nodes, and model weights from their respective
providers. Those components are not included as part of the project's MIT
license; obtain and use each under its own terms.

The Ref2VA input layout was adapted with reference to a public example from
[comfyui-minimax-h3-audio-T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8).
The target server's installed node version must still be checked before use.

Source texts, reference images, model outputs, audio, and other materials
provided by an operator remain subject to their respective rights and terms.
The project's license does not grant rights in those materials. Review the
provenance and terms of any material before redistributing it.

Python dependencies are installed separately and retain their own licenses.
See the dependency metadata in the installed environment for their notices.

## shuohao-skills

This project redistributes six skills from [shuohao-skills](https://github.com/eternityspring/shuohao-skills/tree/8af10e0ea1a550e4575666884f4deef780bf38ff), commit `8af10e0ea1a550e4575666884f4deef780bf38ff`. The included files retain their original content and are licensed under Apache-2.0. The upstream license and copyright notice are included in `src/pipeline/shuohao/vendor/LICENSE` and `src/pipeline/shuohao/vendor/NOTICE`.

## Breeze TTS 2

The independently implemented HTTP adapter follows the API in [Breeze TTS 2](https://github.com/breezeblue-ai/breeze-tts/tree/daa6cb2efe5471f09369229f322d20bb956cb3c3). The upstream inference code is Apache-2.0; the project states that model weights, derivative checkpoints and self-hosted outputs are subject to a separate research and non-commercial license. Consult the [upstream license information](https://github.com/breezeblue-ai/breeze-tts/blob/daa6cb2efe5471f09369229f322d20bb956cb3c3/README.md#license-and-responsible-use) and [model license](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE) for the applicable terms. No upstream TTS code or model weights are included here.
