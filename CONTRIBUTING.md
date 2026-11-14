# Contributing

Thank you for helping improve the project. Contributions are welcome in
English or Chinese. Please discuss changes to public contracts, database
migrations, or workflow bindings before making a large incompatible change.

## Development setup

Use Python 3.11 or later, Node.js 18 or later, and a virtual environment. From a source checkout,
install the locked development dependencies, then the editable project:

```console
python -m pip install --require-hashes -r requirements.lock.txt
python -m pip install -e . --no-deps --no-build-isolation
```

The examples below use the unified `novel-pipeline` entry point. Supply your
own source text and service configuration when performing live checks; the
automated test suite uses local mocks and does not need an API key or GPU.

## Changes and validation

- Keep production orchestration in `src/pipeline/shuohao/`. The bundled upstream
  resources retain their pinned bytes and license. Change application behavior
  in the Python adapter and verify the original Node quality gates.
- Assign creative JSON to native `writer` subagents. The host imports drafts
  through `novel-pipeline text complete` and resumes the pipeline for validation.
  Preserve frozen requests, accepted artifacts, and their hashes.
- Preserve exact asset identity, run snapshots, and safe retry semantics when
  changing the pipeline or ComfyUI client.
- Add a database migration for a schema change and cover upgrade from an
  existing database. Do not silently recreate or discard user data.
- Update documentation and add focused tests for behavior and failure paths.
- Keep credentials, private inputs, generated media, and local runtime data
  out of commits. Local creative works and writing records belong in `novels/`;
  this directory is ignored by Git and excluded from publication.

Run the checks configured for the project before proposing a change:

```console
python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m build
python -m twine check dist/*
python scripts/wheel_smoke.py --wheel-dir dist
```

Run the relevant document quality gates when changing production instructions. A passing
mock test does not establish story fidelity, ComfyUI node compatibility, or
generated-image and video quality. Describe the checks actually run and any
remaining limitations in the contribution.

## Licensing and provenance

By submitting a contribution, you agree to provide it under the project's
[MIT license](LICENSE). Only contribute content you are entitled to share.
Document the provenance and applicable terms of third-party material; model
weights, custom nodes, and user inputs have their own terms. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
