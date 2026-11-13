# Changelog

## 0.2.0 — 2026-11-14

- Organize guides, verification records, and prompt evaluations into dedicated
  directories, standardize agent guidance, and move wheel verification to `scripts/`.
- Keep creative works and writing records under the Git-ignored local `novels/`
  directory, outside public source and Python distributions.
- Preserve recorded line endings for immutable releases, evaluation records,
  so Git checkout retains their stored checksums.
- Add a unified command-line entry point and local MCP integration for prompt
  maintenance, asset operations, pipeline execution, and ComfyUI tasks.
- Use established libraries for configuration validation, HTTP, process
  locks, database access, and schema migrations.
- Package prompt and workflow resources for an installed application and
  provide workspace initialization.
- Preserve versioned prompt snapshots, asset versions, and resumable remote
  task state across runs.
- Support per-shot I2VA and Ref2VA planning, inspection, and mode changes
  before affected media jobs have been submitted.
- Add MIT licensing, contribution guidance, and third-party notices.
- Give all 16 prompt nodes the same fixtures, shared-LLM evaluation, candidate
  staging and guarded publication workflow; retain raw outputs and semantic reviews.
- Apply a shared visual style to image and video prompts, with image-only
  negative prompts and style-aware asset-reference reuse.
- Generate scene views individually and assemble the four results locally;
  use a single-view object prompt while retaining the character sheet and its
  three detail cells.
- Support character face-reference photos and configurable square object images.
- Keep `cinematic_anime_cg` as the only built-in style and the default for
  the workspace, CLI, MCP and Python SDK, preserving the user's tuned text.
- Refine that preset to the user-selected text-only CG rendering, without
  automatically attaching a style image or fixing shot lighting globally.
- Refine storyboard prompts for continuous interaction, visible arm chains,
  natural release and threshold crossing, and prompt timing.
- Replace `cinematic_anime_cg` with a version targeted at the user's reference
  image: sculpted CG facial structure, resolved knit and plaid material detail,
  and a warm filmic grade with deep shadows and light halation, still binding no
  reference image at generation time.
- Consolidate prompt Python code under `prompts/runtime/prompt_project/`;
  keep local style references under `prompts/src/styles/` and runtime data
  under `runs/library/`, excluding private references from distributions.
- Compile every node to a flat UTF-8 `compiled/<node_id>.txt` file, enforcing
  byte consistency and rejecting unexpected artifacts while preserving release hashes.
- Configure multiple ComfyUI endpoints through environment variables, with
  persistent round-robin stage pools and pinned task and upload ownership.
- Distill directing and drama skills into existing nodes, preserving narrative
  knowledge boundaries, delayed disclosure and grounded camera instructions.

Limited real-LLM asset-prompt evaluations and Qwen GPU image comparisons have
been performed. The complete novel pipeline remains unverified end to end;
the updated scene-view pipeline and square object images have also been tested,
with scene geometry, state fidelity and object rendering issues still open.
