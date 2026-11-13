from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from novel_prompt_pipeline.resources import prompt_root

from .asset_compiler import atomic_write_text, compile_assets, safe_path
from .release import PromptRelease, PromptReleaseError
from .validation import check_templates, selftest

# Compatibility for callers which display the default project directory.  All
# operations resolve their own root and never mutate this module-level value.
ROOT = prompt_root()
PROMPT_PATTERN = re.compile(r"\{\{([\w.:-]+)\}\}")


def _project(root: str | Path | None) -> Path:
    return Path(root).expanduser().resolve() if root is not None else prompt_root().resolve()


def _read_index(root: Path) -> dict[str, Any]:
    index_path = safe_path(root, "src", "nodes", "index.json")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptReleaseError(f"Cannot read node registry: {index_path}") from exc
    if not isinstance(index.get("nodes"), dict) or not index["nodes"]:
        raise PromptReleaseError("Node registry must contain at least one node")
    return index


def _node_sources(index: dict[str, Any], root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    filenames = {f"asset.{mode}.txt" for mode in ("scene", "character", "prop")}
    for node_id, entry in index["nodes"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node_id):
            raise PromptReleaseError(f"Unsafe node id: {node_id}")
        output = f"{node_id}.txt".casefold()
        if output in filenames:
            raise PromptReleaseError(f"Duplicate compiled prompt filename: {node_id}")
        filenames.add(output)
        filename = entry.get("file") if isinstance(entry, dict) else None
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise PromptReleaseError(f"Node source must be a direct filename: {node_id}")
        path = safe_path(root, "src", "nodes", filename).resolve()
        if path.parent != safe_path(root, "src", "nodes").resolve():
            raise PromptReleaseError(f"Node source must be a direct child of src/nodes: {node_id}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptReleaseError(f"Missing node source for {node_id}: {path}") from exc
        if text.startswith("\ufeff"):
            raise PromptReleaseError(f"Node source must not include a UTF-8 BOM: {node_id}")
        declared = entry.get("variables")
        if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
            raise PromptReleaseError(f"Node {node_id} must declare a string array named variables")
        if len(set(declared)) != len(declared):
            raise PromptReleaseError(f"Node {node_id} repeats declared variables")
        used = set(PROMPT_PATTERN.findall(text))
        unexpected = used - set(declared)
        unused = set(declared) - used
        if unexpected or unused:
            raise PromptReleaseError(
                f"Node {node_id} variable mismatch; undeclared: {sorted(unexpected)}, unused: {sorted(unused)}"
            )
        if not isinstance(entry.get("output_contract"), dict):
            raise PromptReleaseError(f"Node {node_id} has no output_contract")
        result[node_id] = (text, entry)
    return result


def build(*, check: bool = False, root: str | Path | None = None) -> list[str]:
    """Compile the existing three asset prompts and all registered pipeline nodes."""
    root = _project(root)
    index = _read_index(root)
    nodes = _node_sources(index, root)
    errors = compile_assets(root, check=check)
    if errors and not check:
        return errors

    if check:
        for node_id, (text, _) in nodes.items():
            output = safe_path(root, "compiled", f"{node_id}.txt")
            if not output.is_file() or output.read_bytes() != text.encode("utf-8"):
                errors.append(
                    f"{node_id}: compiled/{node_id}.txt is not synchronized with src/nodes/"
                )
        expected = {f"{node_id}.txt" for node_id in nodes}
        expected.update(f"asset.{mode}.txt" for mode in ("scene", "character", "prop"))
        destination = safe_path(root, "compiled")
        if destination.is_dir():
            for item in destination.iterdir():
                if item.name not in expected:
                    errors.append(f"Unexpected compiled output: compiled/{item.name}")
        else:
            errors.append("Missing compiled output directory: compiled/")
        from .inventory import check_contracts

        errors.extend(check_contracts(root))
        return errors

    for node_id, (text, _) in nodes.items():
        atomic_write_text(safe_path(root, "compiled", f"{node_id}.txt"), text)
    legacy = safe_path(root, "compiled", "nodes")
    if legacy.is_dir():
        for _, entry in nodes.values():
            output = safe_path(root, "compiled", "nodes", entry["file"])
            if output.exists():
                if not output.is_file():
                    raise PromptReleaseError(f"Legacy compiled output is not a file: {output}")
                output.unlink()
        try:
            legacy.rmdir()
        except OSError:
            pass  # Preserve unrecognized files; the check reports the extra directory.
    return errors


def validate_sources(root: str | Path | None = None) -> dict[str, Any]:
    root = _project(root)
    index = _read_index(root)
    nodes = _node_sources(index, root)
    expected = set(index["nodes"])
    if set(nodes) != expected:
        raise PromptReleaseError("Could not resolve all registered prompt nodes")
    for mode in ("scene", "character", "prop"):
        if not safe_path(root, "compiled", f"asset.{mode}.txt").is_file():
            raise PromptReleaseError(f"Missing compiled asset prompt: compiled/asset.{mode}.txt")
    return index


def stage(*, root: str | Path | None = None) -> PromptRelease:
    """Build and verify an immutable candidate without changing current.json."""
    root = _project(root)
    errors = build(check=False, root=root)
    if errors:
        raise PromptReleaseError("Build failed:\n" + "\n".join(errors))
    errors = build(check=True, root=root)
    if errors:
        raise PromptReleaseError("Consistency check failed:\n" + "\n".join(errors))
    index = validate_sources(root)
    failed = [label for label, ok in check_templates(root) if not ok]
    if failed:
        raise PromptReleaseError("Asset consistency checks failed:\n" + "\n".join(failed))
    errors = selftest(root)
    if errors:
        raise PromptReleaseError("Asset selftest failed:\n" + "\n".join(errors))
    release_root = safe_path(root, "releases")

    file_payloads: dict[str, bytes] = {}
    assets: dict[str, dict[str, str]] = {}
    for mode in ("scene", "character", "prop"):
        name = f"files/assets/{mode}.txt"
        file_payloads[name] = safe_path(root, "compiled", f"asset.{mode}.txt").read_bytes()
        assets[mode] = {"file": name, "sha256": hashlib.sha256(file_payloads[name]).hexdigest()}

    nodes: dict[str, dict[str, Any]] = {}
    for node_id, entry in index["nodes"].items():
        name = f"files/nodes/{entry['file']}"
        file_payloads[name] = safe_path(root, "compiled", f"{node_id}.txt").read_bytes()
        nodes[node_id] = {
            "file": name,
            "sha256": hashlib.sha256(file_payloads[name]).hexdigest(),
            "variables": entry["variables"],
            "output_contract": entry["output_contract"],
        }

    asset_variables = ["小说原文", "推文文案", "故事情节"]
    for mode in ("scene", "character", "prop"):
        entry = assets[mode]
        mode_config = json.loads(
            safe_path(root, "src", "modes", f"{mode}.json").read_text(encoding="utf-8")
        )
        aspect_ratio = mode_config.get("image_aspect_ratio")
        if aspect_ratio not in (None, "1:1", "16:9"):
            raise PromptReleaseError(f"Unsupported image aspect ratio for asset.{mode}")
        nodes[f"asset.{mode}"] = {
            **entry,
            "variables": asset_variables,
            "output_contract": {
                "format": "json_asset_array",
                "entry_fields": ["name", "aliases", "description"],
                "description": "JSON array of scene, character, or prop asset cards.",
                **({"image_aspect_ratio": aspect_ratio} if aspect_ratio else {}),
            },
        }

    content_hash = hashlib.sha256()
    content_hash.update(
        json.dumps(
            {"assets": assets, "nodes": nodes},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    version = content_hash.hexdigest()
    manifest = {
        "schema_version": 1,
        "version": version,
        "assets": assets,
        "nodes": nodes,
    }
    release_dir = safe_path(root, "releases", version)
    release_root.mkdir(parents=True, exist_ok=True)
    if release_dir.exists():
        existing = PromptRelease(root, version)
        if existing.manifest != manifest:
            raise PromptReleaseError(f"Refusing to overwrite immutable release {version}")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".release-", dir=release_root))
        try:
            for relative, content in file_payloads.items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="",
            )
            os.replace(staging, release_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    return PromptRelease(root, version)


def publish(
    *,
    root: str | Path | None = None,
    evaluation: str | Path | None = None,
    offline: bool = False,
) -> PromptRelease:
    """Activate a checked candidate with an evaluation or explicit offline approval."""
    if evaluation is not None and offline:
        raise PromptReleaseError("Choose an evaluation report or offline publication, not both")
    if evaluation is None and not offline:
        raise PromptReleaseError(
            "Publication requires an evaluation report or explicit offline=True"
        )
    root = _project(root)
    release = stage(root=root)
    if evaluation is not None:
        from .evaluate import verify_evaluation

        verify_evaluation(release, Path(evaluation))
    pointer = safe_path(root, "current.json")
    fd, tmp_name = tempfile.mkstemp(prefix=".current-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump({"version": release.version}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, pointer)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return release
