"""Prompt build and asset validation must treat workspace files as data only."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from prompt_project.asset_compiler import compile_assets, render
from prompt_project.compiler import build, publish, stage
from prompt_project.inventory import inventory
from prompt_project.release import PromptReleaseError
from prompt_project.validation import check_templates, selftest, validate_assets

REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / "prompts"


def test_initialization_excludes_private_style_sources(tmp_path, monkeypatch):
    from novel_prompt_pipeline import resources

    bundle = tmp_path / "bundle"
    for relative in (
        "prompts/src",
        "prompts/compiled",
        "prompts/releases",
        "prompts/tools",
        "prompts/tests/fixtures",
        "prompts/evaluations/suites",
        "workflows",
        "configs",
    ):
        shutil.copytree(
            REPO / relative,
            bundle / relative,
            ignore=shutil.ignore_patterns("styles", "*.local.json", "__pycache__"),
        )
    for relative in ("prompts/current.json", ".env.example"):
        shutil.copyfile(REPO / relative, bundle / relative)
    private = bundle / "prompts/src/styles/private-reference.png"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private user visual reference")
    monkeypatch.setattr(resources, "_bundle_root", lambda: bundle)
    destination = tmp_path / "workspace"
    resources.initialize_workspace(destination)
    assert (destination / "prompts/src/nodes/index.json").is_file()
    assert not (destination / "prompts/src/styles").exists()


def test_mode_state_overrides_do_not_mutate_common_rules():
    common = json.loads((PROMPTS / "src/common.json").read_text(encoding="utf-8"))
    original = json.loads(json.dumps(common))
    mode = json.loads((PROMPTS / "src/modes/prop.json").read_text(encoding="utf-8"))
    mode["state_rules_override"] = {"variant_only": "UNIQUE STATE POLICY"}
    assert "UNIQUE STATE POLICY" in render(mode, common)
    assert common == original
    character = json.loads((PROMPTS / "src/modes/character.json").read_text(encoding="utf-8"))
    assert "UNIQUE STATE POLICY" not in render(character, common)
    mode["state_rules_override"]["unknown_policy"] = "invalid"
    with pytest.raises(ValueError, match="state_rules_override"):
        render(mode, common)


def workspace(tmp_path: Path, name: str = "workspace") -> Path:
    root = tmp_path / name / "prompts"
    for rel in ("src", "compiled", "tests/fixtures", "evaluations/suites"):
        shutil.copytree(PROMPTS / rel, root / rel)
    return root


def test_compiler_uses_trusted_code_not_workspace_scripts(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    tools = root / "tools"
    tools.mkdir()
    marker = tmp_path / "workspace-script-executed"
    for name in ("build.py", "cli.py", "validate_asset_full.py"):
        (tools / name).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
    with patch("subprocess.run", side_effect=AssertionError("workspace executable invoked")):
        assert build(root=root) == []
        assert build(root=root, check=True) == []
        published = publish(root=root, offline=True)
    assert not marker.exists()
    assert (
        published.version
        == json.loads((root / "current.json").read_text(encoding="utf-8"))["version"]
    )
    for mode in ("character", "scene", "prop"):
        assert published.text(f"asset.{mode}") == (
            root / "compiled" / f"asset.{mode}.txt"
        ).read_text(encoding="utf-8")


def test_distinct_workspace_sources_and_outputs_remain_isolated(tmp_path: Path) -> None:
    left = workspace(tmp_path, "left")
    right = workspace(tmp_path, "right")
    common = right / "src" / "common.json"
    source = json.loads(common.read_text(encoding="utf-8"))
    source["output_format"]["english"] += " Different project instruction."
    common.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    assert build(root=left) == []
    assert build(root=right) == []
    assert build(root=left, check=True) == []
    assert build(root=right, check=True) == []
    assert (left / "compiled" / "asset.scene.txt").read_bytes() != (
        right / "compiled" / "asset.scene.txt"
    ).read_bytes()
    assert all(ok for _, ok in check_templates(left))
    assert all(ok for _, ok in check_templates(right))


def test_publication_rejects_bad_fixture_before_changing_current(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    assert build(root=root) == []
    first = publish(root=root, offline=True)
    fixture = root / "tests" / "fixtures" / "scene_ok.json"
    fixture.write_text('{"results":[]}', encoding="utf-8")
    assert selftest(root)
    with pytest.raises(PromptReleaseError, match="selftest"):
        publish(root=root, offline=True)
    assert (
        json.loads((root / "current.json").read_text(encoding="utf-8"))["version"] == first.version
    )


def test_rejects_untrusted_output_names_and_bad_node_paths(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    mode_file = root / "src" / "modes" / "scene.json"
    mode = json.loads(mode_file.read_text(encoding="utf-8"))
    mode["output_filename"] = "../../outside.txt"
    mode_file.write_text(json.dumps(mode, ensure_ascii=False), encoding="utf-8")
    assert compile_assets(root)
    assert not (tmp_path / "outside.txt").exists()
    shutil.copyfile(PROMPTS / "src" / "modes" / "scene.json", mode_file)
    index_path = root / "src" / "nodes" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["nodes"]["novel_to_script"]["file"] = "../../outside.txt"
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PromptReleaseError):
        build(root=root)
    index["nodes"]["novel_to_script"]["file"] = str(root / "src" / "nodes" / "novel_to_script.md")
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PromptReleaseError):
        build(root=root)


def test_compilation_does_not_follow_a_preexisting_hard_link(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    protected = root / "protected.txt"
    protected.write_text("original data", encoding="utf-8")
    compiled = root / "compiled" / "asset.scene.txt"
    compiled.unlink()
    try:
        os.link(protected, compiled)
    except OSError as exc:
        pytest.skip(f"Hard links are not available: {exc}")
    assert compile_assets(root) == []
    assert protected.read_text(encoding="utf-8") == "original data"


def test_asset_fixture_passes_and_invalid_outputs_fail() -> None:
    for kind in ("character", "scene", "prop"):
        fixtures = PROMPTS / "tests" / "fixtures"
        text = (fixtures / f"{kind}_ok.json").read_text(encoding="utf-8")
        source = (fixtures / f"{kind}_src.txt").read_text(encoding="utf-8")
        assert validate_assets(text, kind, source) == []
        assert validate_assets('{"results":[]}', kind, source)


def test_publication_requires_report_or_explicit_offline_and_preserves_current(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path)
    first = publish(root=root, offline=True)
    pointer = (root / "current.json").read_bytes()
    with pytest.raises(PromptReleaseError, match="requires an evaluation"):
        publish(root=root)
    with pytest.raises(PromptReleaseError, match="not both"):
        publish(root=root, evaluation=root / "report.json", offline=True)
    assert (root / "current.json").read_bytes() == pointer
    assert json.loads(pointer)["version"] == first.version


def test_flat_compiled_outputs_preserve_release_bytes_and_version(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    candidate = stage(root=root)
    active = json.loads((PROMPTS / "current.json").read_text(encoding="utf-8"))["version"]
    assert candidate.version == active
    nodes = inventory(root)
    expected = {f"{name}.txt" for name in nodes}
    assert {item.name for item in (root / "compiled").iterdir()} == expected
    for name, entry in nodes.items():
        raw = (root / entry["compiled"]).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
        assert raw == candidate.text(name).encode("utf-8")


def test_build_cleans_only_known_legacy_files_and_flags_extra_outputs(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    compiled = root / "compiled"
    legacy_nodes = compiled / "nodes"
    legacy_nodes.mkdir()
    (legacy_nodes / "novel_to_script.md").write_text("old generated prompt", encoding="utf-8")
    preserved = legacy_nodes / "unrecognized.md"
    preserved.write_text("user content", encoding="utf-8")
    (compiled / "scene.txt").write_text("old generated prompt", encoding="utf-8")
    extra = compiled / "other.txt"
    extra.write_text("user content", encoding="utf-8")

    assert build(root=root) == []
    assert not (legacy_nodes / "novel_to_script.md").exists()
    assert not (compiled / "scene.txt").exists()
    assert preserved.read_text(encoding="utf-8") == "user content"
    assert extra.read_text(encoding="utf-8") == "user content"
    errors = build(root=root, check=True)
    assert any("compiled/nodes" in error for error in errors)
    assert any("compiled/other.txt" in error for error in errors)
    preserved.unlink()
    legacy_nodes.rmdir()
    extra.unlink()
    assert build(root=root, check=True) == []


def test_check_rejects_crlf_or_bom_and_compilation_restores_lf(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    target = root / "compiled/asset.scene.txt"
    original = target.read_bytes()
    target.write_bytes(b"\xef\xbb\xbf" + original.replace(b"\n", b"\r\n"))
    assert any("scene" in error for error in build(root=root, check=True))
    assert build(root=root) == []
    assert target.read_bytes() == original


def test_case_insensitive_node_filename_collisions_are_rejected(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    path = root / "src/nodes/index.json"
    index = json.loads(path.read_text("utf-8"))
    index["nodes"]["ASSET.CHARACTER"] = index["nodes"]["novel_to_script"]
    path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(PromptReleaseError, match="Duplicate compiled prompt filename"):
        build(root=root)
