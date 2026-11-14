import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.shuohao import upstream

STAGES = ("outline", "characters", "art", "script", "staging", "storyboard")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("stage", STAGES)
def test_each_bundled_cli_help(stage: str, tmp_path: Path) -> None:
    assert f"novel-{stage}.mjs" in upstream.tool(stage, ["--help"], cwd=tmp_path)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_invalid_json_reports_validator_output(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(upstream.UpstreamValidationError) as exc:
        upstream.validate("outline", Path("bad.json"), {}, cwd=tmp_path)
    assert exc.value.stderr
    assert "status" in str(exc.value)


def test_missing_node_and_timeout(tmp_path: Path) -> None:
    with patch("pipeline.shuohao.upstream.subprocess.run", side_effect=FileNotFoundError("node")):
        with pytest.raises(upstream.UpstreamError, match="Node"):
            upstream.tool("outline", ["--help"], cwd=tmp_path)
    with patch(
        "pipeline.shuohao.upstream.subprocess.run",
        side_effect=subprocess.TimeoutExpired("node", 1),
    ):
        with pytest.raises(upstream.UpstreamError, match="timed out"):
            upstream.tool("outline", ["--help"], cwd=tmp_path, timeout=1)


def test_instructions_include_production_schema_without_fixtures() -> None:
    text = upstream.instructions("storyboard")
    assert "# references/schema.md" in text
    assert "# references/storyboard-pass.md" in text
    assert "# references/test-fixtures/" not in text
