# Repository guidance

## Layout

- `src/`: Python runtime packages (`pipeline`, `tts`, `comfyui_client`,
  and bundled-resource helpers). The default text backend hands frozen tasks to host-session Codex subagents; Python validates and resumes their submitted JSON. `pipeline/images/` owns Codex image task
  handoff, immutable inputs, result import and asset-board rendering.
- `workflows/`: ComfyUI API graphs and binding declarations.
- `configs/`: distributable configuration example and ignored local settings.
- `tests/`: automated product tests; `scripts/`: repository verification utilities.
- `docs/guides/`, `docs/architecture/`, and `docs/verification/`: usage,
  structure, and evidence respectively. Keep links relative to their owning file.
- `novels/`: local creative works and writing records; preserve locked content
  and keep archived versions intact. Ignore this directory in Git and exclude
  it from public repositories and Python distributions.
- `runs/` and `.tmp/`: local asset library, execution records and scratch work.
  Keep local files out of commits and packages.

## Editing and validation

Keep remote execution in `src/comfyui_client/`,
independent Breeze TTS access in `src/tts/`, and orchestration and asset management
in `src/pipeline/`. For Codex image tasks,
follow `docs/guides/codex-images.md` and use the host imagegen skill; accept
only visually inspected results, and keep their references and hashes. CLI and MCP share the
application service. Do not duplicate inference or workflow logic in scripts.

Preserve fixed resource hashes and existing-run snapshots. Update resource packaging, initialization,
tests, CI and documentation together when changing a distributed path.
Keep Git's byte-preservation attributes for releases and evaluation evidence;
line-ending conversion can invalidate stored hashes.

Use the workspace virtual environment. Check relevant tests and run the required
repository checks before delivery:

```text
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m pytest
```

For packaging changes, build the sdist and wheel, run `python -m twine check dist/*`,
and run `python scripts/wheel_smoke.py --wheel-dir dist`. Distinguish offline
checks from real text/image/video evaluation; document observed limits honestly.

Never commit `.env`, local configuration, private assets, credentials, caches or
generated media. Preserve unrelated user content. Native subagents may own
independent work with explicit file boundaries; do not overwrite another agent's
edits. The primary agent remains responsible for integration and verification.

Assign creative production documents (outlines, character/art descriptions,
scripts, staging and storyboard prompts) to `writer` subagents. Use `implementer`
for product code and `reviewer` for independent checks. The host dispatches text
tasks; the Python pipeline validates and imports their JSON outputs.
