# Novel Prompt Pipeline

[中文文档](README.zh-CN.md)

A Python pipeline for turning a supplied novel into versioned visual-asset and
storyboard prompts. It can generate asset reference images, storyboard first
frames, and videos through one or more configured ComfyUI servers. The LLM handles textual
reasoning; the application validates outputs, manages assets, binds workflow
inputs, and records execution state.

This repository contains no model weights. Supply your own source text, an
OpenAI-compatible LLM service, and a ComfyUI installation with the required
models and custom nodes.

## Install and initialize

Python 3.11 or newer is required. From a source checkout, create a virtual
environment and install the project:

```console
python -m venv .venv
```

Activate the environment before installing. On Windows
PowerShell use `.venv\Scripts\Activate.ps1`; on Linux/macOS use
`source .venv/bin/activate`, then run:

```console
python -m pip install .
novel-pipeline init ./my-workspace
cd ./my-workspace
```

`init` creates a workspace containing editable
prompt and workflow resources, `configs/pipeline.local.json`, and
`.env.example`. Copy `.env.example` to `.env` and supply your own credentials
and service addresses; keep `.env` private. Set a distinct `project_id` for
each work in the pipeline configuration.

A built wheel can also be installed locally; it includes the resources needed
to initialize a workspace. No package-registry publication is assumed.

## Run

From the initialized workspace, after supplying an input file and configuring
the LLM:

```console
novel-pipeline prompts check
novel-pipeline run --input /path/to/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --dry-run
novel-pipeline run --input /path/to/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json
novel-pipeline run --input /path/to/chapter.txt --output runs/chapter-001 --config configs/pipeline.local.json --resume --render
```

`--dry-run` inspects local configuration and prompt/workflow resources without
calling an LLM or GPU. The first normal run extracts assets and writes script,
rewrite, and storyboard results. `--resume --render` resumes that run and
submits media jobs to the configured ComfyUI servers. Provide a chapter or other
input that fits the chosen LLM's context and output limits.

The prompt project's published version is used automatically for new runs.
Each run retains its prompt snapshot, selected asset versions, and reference
file hashes. Resuming a run checks its source, configuration, and workflow
identity. A changed input or configuration requires a new run directory.

## Interfaces and structure

`novel-pipeline` is the common CLI for workspace initialization, prompts,
assets, database migrations, runs, ComfyUI, and a local stdio MCP server.
Run `novel-pipeline --help` and a subcommand's `--help` for the supported
arguments. The MCP interface shares the application's business services; see
[the MCP guide](docs/guides/mcp.md).

| Path | Responsibility |
| --- | --- |
| `src/llm/` | Shared LLM configuration and OpenAI SDK adapter for inference and prompt evaluation |
| `src/pipeline/` | Pipeline execution, validation, asset management, and database migrations |
| `prompts/runtime/prompt_project/` | Prompt building, evaluation, and release operations |
| `src/comfyui_client/` | ComfyUI HTTP client and workflow binding |
| `prompts/` | Prompt sources, compiled artifacts, release manifests, and training history |
| `workflows/` | API graphs and input binding profiles |
| `configs/` | Configuration template |
| `tests/` and `scripts/` | Automated tests and development checks |
| `docs/` | [Documentation index](docs/README.md), guides, and verification records |
| `novels/` | Optional local creative works and writing records; ignored by Git and excluded from publication |

LLM inference and prompt evaluation share the same `llm` configuration:
model, base URL, API mode, key environment-variable name, timeout, retries,
and extra request parameters. Model compatibility and reasoning quality must
be checked with the service you choose. Prompt “training” here means
iteration and evaluation; it does not fine-tune model weights.
See the [prompt project guide](docs/guides/prompt-project.md) for the shared
node lifecycle and evaluation, and the [ComfyUI guide](docs/guides/comfyui.md)
for multi-server environment configuration and routing.

## Assets and media

SQLite stores project-scoped asset metadata, versions, provenance, and usage;
managed reference files are stored alongside the database. The application
reuses selected asset versions and reference images during a resumed run.
See [pipeline and asset management](docs/guides/pipeline.md).

Character references use a portrait, three views, and three detail cells.
The scene pipeline generates a single main view, then a reverse eye-level view
and an oblique overview using the main image as reference. A vertical top-down
view uses those three images as references; Pillow assembles the four results
into a 2×2 scene reference. Each Qwen call produces one view. Object references
remain single-view, with a source-supported close-up when a fragment is
otherwise ambiguous. The default object image is 1:1 at 1024 x 1024;
`media.prop_size` configures its size, while older prompt snapshots retain
their original dimensions. Scene and object prompts no longer require detail grids.

Bind a portrait as a character face reference with
`novel-pipeline assets --config configs/pipeline.local.json link-face <character_asset_id> portrait.jpg`,
or configure `media.character_face_references` by character name or alias.
Review face fidelity in generated images; see [pipeline and asset management](docs/guides/pipeline.md)
for configuration and caching details.

The pipeline also applies a project-wide visual style when preparing media.
The sole built-in preset, `cinematic_anime_cg`, is the default for workspaces,
CLI, MCP and the Python SDK. It is tuned to the user's reference image for face
structure, material detail and a warm filmic grade. It does not automatically
bind a user reference image.
Its prefix and suffix apply to images and video; negative prompts
apply only to images. Style-specific asset references are cached separately,
and resumed runs keep their selected reference files. See the
[style configuration](docs/guides/pipeline.md#全局画风).

The pipeline can choose I2VA or Ref2VA per shot and lets you review or change
that choice before affected media tasks are submitted. Qwen supports up to 10
image references. I2VA uses a dedicated first-frame input; Ref2VA uses the
storyboard image as a reference, plus up to eight additional image references
and three optional audio references. Ref2VA does not guarantee that its
reference becomes the exact first video frame. Validate the actual custom nodes
and models on your target server. See the [ComfyUI guide](docs/guides/comfyui.md).

## Development and verification

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks. Automated tests use
mock services; they do not establish the quality of an actual LLM response or
generated media. Real-LLM asset evaluations and a limited Qwen GPU comparison
have been performed; they do not establish end-to-end results for the full
novel pipeline. See [verification](docs/verification/verification.md) for the exact scope
and outstanding checks.

The project is available under the [MIT License](LICENSE). Models, custom
nodes, and user-provided materials have their own terms; see
[third-party notices](THIRD_PARTY_NOTICES.md).
