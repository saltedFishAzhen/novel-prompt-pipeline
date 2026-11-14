# Novel Prompt Pipeline

[中文文档](README.zh-CN.md)

Turn a novel into script, art, storyboard and video production materials using the six-stage shuohao workflow. Codex session subagents generate creative JSON, Codex image tasks produce asset boards and a frame for each cut, and ComfyUI workflows produce video. The default run stops at prompts; media requires `--render`.

Python 3.11+, Node.js 18+, and a Codex host session capable of dispatching native subagents are required for text generation. Video also requires a compatible ComfyUI installation and models.

```console
python -m venv .venv
```

Activate the environment (`.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on Linux/macOS), then:

```console
python -m pip install .
novel-pipeline init ./my-workspace
cd ./my-workspace
```

Edit `configs/pipeline.local.json` and configure service addresses as needed. Keep credentials and private media out of version control. Supply text that fits the model's context:

```console
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json
novel-pipeline run --input inputs/chapter.txt --output runs/chapter --config configs/pipeline.local.json --resume --stop-after frames --render
```

The run pauses at `awaiting_text` when text tasks need a host session. Dispatch the pending tasks to native subagents, import their JSON drafts, and resume until the quality gates pass. Python cannot spawn session subagents itself. See the [pipeline guide](docs/guides/pipeline.md) for stages, stop points, resume restrictions and video inputs. Review and accept each [Codex image task](docs/guides/codex-images.md). `--render` without a stop point requests the full media workflow; video submission waits until every storyboard frame is accepted. The workflow has not yet been validated end to end with a real novel, images and video, and audio postproduction compatibility with the script remains unverified.

See the [documentation index](docs/README.md) for more.
