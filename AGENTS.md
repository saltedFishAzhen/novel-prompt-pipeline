# Repository guidance

## Layout

- `src/`: Python runtime packages (`pipeline`, `llm`, `comfyui_client`,
  and bundled-resource helpers).
- `prompts/`: editable prompt project; read `prompts/AGENTS.md` before changing it.
  `src/` holds authoritative prompt sources; `runtime/prompt_project/` contains
  the Python build, evaluation, validation and release implementation.
  `compiled/` and `releases/` are generated resources.
  `evaluations/` holds historical cases and reports; `tests/fixtures/` contains
  the distributable validation fixtures.
- `workflows/`: ComfyUI API graphs and binding declarations.
- `configs/`: distributable configuration example and ignored local settings.
- `tests/`: automated product tests; `scripts/`: repository verification utilities.
- `docs/guides/`, `docs/architecture/`, and `docs/verification/`: usage,
  structure, and evidence respectively. Keep links relative to their owning file.
- `novels/`: local creative works and writing records; preserve locked content
  and keep archived versions intact. Ignore this directory in Git and exclude
  it from public repositories and Python distributions.
- `runs/` and `.tmp/`: local asset library, execution records and scratch work.
  `prompts/src/styles/` holds private local style-reference material. Keep
  these local files out of commits and packages.

## Editing and validation

Keep shared LLM access in `src/llm/`, remote execution in `src/comfyui_client/`,
and orchestration and asset management in `src/pipeline/`. CLI and MCP share the
application service. Do not duplicate inference or workflow logic in scripts.

Preserve prompt release hashes and existing-run snapshots. Do not edit released
or compiled prompt content by hand. Update resource packaging, initialization,
tests, CI and documentation together when changing a distributed path.
Keep Git's byte-preservation attributes for releases and evaluation evidence;
line-ending conversion can invalidate stored hashes.

Use the workspace virtual environment. Check relevant tests and run the required
repository checks before delivery:

```text
python -m ruff check src prompts/runtime tests scripts
python -m ruff format --check src prompts/runtime tests scripts
novel-pipeline prompts check
python -m pytest
```

For packaging changes, build the sdist and wheel, run `python -m twine check dist/*`,
and run `python scripts/wheel_smoke.py --wheel-dir dist`. Distinguish offline
checks from real LLM/image/video evaluation; document observed limits honestly.

Never commit `.env`, local configuration, private assets, credentials, caches or
generated media. Preserve unrelated user content. Native subagents may own
independent work with explicit file boundaries; do not overwrite another agent's
edits. The primary agent remains responsible for integration and verification.
