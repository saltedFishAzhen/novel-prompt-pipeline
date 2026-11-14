from pathlib import Path

import pytest

from novel_prompt_pipeline.resources import initialize_workspace


def test_initialize_has_only_runtime_resources_and_is_repeatable(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    assert (tmp_path / "configs" / "pipeline.local.json").is_file()
    assert (tmp_path / "workflows" / "workflow_bindings.json").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert not (tmp_path / "prompts").exists()
    assert not (tmp_path / "runs").exists()
    assert initialize_workspace(tmp_path)["files_created"] == 0


def test_initialize_does_not_overwrite_local_config(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    config = tmp_path / "configs" / "pipeline.local.json"
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        initialize_workspace(tmp_path)
    assert config.read_text(encoding="utf-8") == "{}"
