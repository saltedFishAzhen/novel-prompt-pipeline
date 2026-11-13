# Third-party materials

The repository contains workflow configuration supplied or adapted for this
project. A ComfyUI graph is configuration: using it also requires compatible
ComfyUI software, custom nodes, and model weights from their respective
providers. Those components are not included as part of the project's MIT
license; obtain and use each under its own terms.

The image-reference inputs were checked against the documentation for the
[ComfyUI Qwen Image 2.1 text-encoding node](https://github.com/Comfy-Org/embedded-docs/blob/main/comfyui_embedded_docs/docs/TextEncodeQwenImage21/en.md).
The Ref2VA input layout was adapted with reference to a public example from
[comfyui-minimax-h3-audio-T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8).
The target server's installed node version must still be checked before use.

Source texts, reference images, model outputs, audio, and other materials
provided by an operator remain subject to their respective rights and terms.
The project's license does not grant rights in those materials. Review the
provenance and terms of any material before redistributing it.

Python dependencies are installed separately and retain their own licenses.
See the dependency metadata in the installed environment for their notices.

## Video direction research

The project's prompt design was informed by a review of
[skill-for-video](https://github.com/Yganna-c/skill-for-video-/tree/9d2524bbd39ccc6cacaea19280dd9da15d834570)
at commit `9d2524bbd39ccc6cacaea19280dd9da15d834570`. The reviewed source
contained no license file. The project does not redistribute its templates,
tables, or examples; its prompt instructions use independently written
expressions of general ideas about scene-space lighting, viewpoint, continuity,
and adapting shot instructions to the available inputs. The upstream skill's
specific confirmation flow, fixed lighting coordinates, style presets, and
model capability claims were not adopted. An operator seeking to reuse the
upstream material itself must determine the applicable permission separately.

## drama-skills

The project's prompt research reviewed [drama-skills](https://github.com/zenstory-ai/drama-skills/tree/8cf7404981b7095b498e061832bb3c9ffd6a42be) at commit `8cf7404981b7095b498e061832bb3c9ffd6a42be`. The upstream project is distributed under the MIT License. The project uses independently written instructions informed by concepts such as knowledge boundaries, reveal order, and separating off-screen audio from visible assets; it does not copy the upstream templates or prescribe its workflow.

MIT License

Copyright (c) 2026 drama-skills contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
