"""Locate editable workspace resources or immutable defaults bundled in wheels."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def repository_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (
        candidate / "prompts" / "current.json"
    ).is_file():
        return candidate
    return None


def default_workspace() -> Path:
    return Path(os.getenv("NOVEL_PIPELINE_HOME") or Path.cwd()).expanduser().resolve()


def _bundle_root() -> Path:
    root = Path(__file__).resolve().parent / "_resources"
    if root.is_dir():
        return root
    repo = repository_root()
    if repo:
        return repo
    raise FileNotFoundError("Bundled resources are missing; reinstall the project wheel")


def prompt_root(workspace: Path | str | None = None) -> Path:
    active = Path(workspace).expanduser().resolve() if workspace else default_workspace()
    candidate = active / "prompts"
    if (candidate / "current.json").is_file():
        return candidate
    repo = repository_root()
    if repo:
        return repo / "prompts"
    return _bundle_root() / "prompts"


def workflow_root(workspace: Path | str | None = None) -> Path:
    active = Path(workspace).expanduser().resolve() if workspace else default_workspace()
    candidate = active / "workflows"
    if (candidate / "workflow_bindings.json").is_file():
        return candidate
    repo = repository_root()
    if repo:
        return repo / "workflows"
    return _bundle_root() / "workflows"


def initialize_workspace(path: str | Path) -> dict:
    """Copy distributable defaults without overwriting any existing local changes.

    All conflicts are detected before writing. Repeated initialization is allowed
    only where existing files match the defaults; credentials are never replaced.
    """
    destination = Path(path).expanduser().resolve()
    source = _bundle_root()
    selections = [
        ("prompts/src/common.json", "prompts/src/common.json"),
        ("prompts/src/modes", "prompts/src/modes"),
        ("prompts/src/nodes", "prompts/src/nodes"),
        ("prompts/compiled", "prompts/compiled"),
        ("prompts/releases", "prompts/releases"),
        ("prompts/current.json", "prompts/current.json"),
        ("prompts/tools", "prompts/tools"),
        ("prompts/tests/fixtures", "prompts/tests/fixtures"),
        ("prompts/evaluations/suites", "prompts/evaluations/suites"),
        ("workflows", "workflows"),
        ("configs/pipeline.example.json", "configs/pipeline.local.json"),
        ("env.example" if (source / "env.example").is_file() else ".env.example", ".env.example"),
    ]
    pending = []
    for origin, target in selections:
        item = source / origin
        files = [item] if item.is_file() else sorted(item.rglob("*"))
        if not files:
            raise FileNotFoundError(f"Missing distributable resources: {origin}")
        for file in files:
            if not file.is_file() or file.suffix in {".pyc", ".pyo"} or "__pycache__" in file.parts:
                continue
            relative = file.relative_to(item) if item.is_dir() else Path()
            output = destination / target / relative if item.is_dir() else destination / target
            if file.is_symlink() or not output.resolve().is_relative_to(destination):
                raise ValueError("Unsafe initialization resource path")
            if output.exists():
                if (
                    output.is_symlink()
                    or not output.is_file()
                    or output.read_bytes() != file.read_bytes()
                ):
                    raise FileExistsError(
                        f"Workspace file already exists with local changes: {output}"
                    )
            else:
                pending.append((file, output))
    destination.mkdir(parents=True, exist_ok=True)
    for file, output in pending:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, output)
    config = destination / "configs" / "pipeline.local.json"
    # Verify the distributed configuration is readable without needing credentials.
    json.loads(config.read_text(encoding="utf-8"))
    return {
        "workspace": str(destination),
        "config": str(config),
        "env_file": str(destination / ".env.example"),
        "files_created": len(pending),
    }
