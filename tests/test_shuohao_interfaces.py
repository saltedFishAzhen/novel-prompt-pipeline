import json
import sys
import types

import pytest

from pipeline.cli import build_parser
from pipeline.service import AppService, WorkspacePathError


def test_run_uses_production_and_rejects_legacy_resume(tmp_path, monkeypatch):
    calls = []

    class ProductionPipeline:
        def __init__(self, config):
            calls.append(("init", config))

        def run(self, source, output, **kwargs):
            calls.append(("run", source, output, kwargs))
            return {"status": "ready"}

        def close(self):
            calls.append(("close",))

    monkeypatch.setitem(
        sys.modules,
        "pipeline.shuohao.engine",
        types.SimpleNamespace(ProductionPipeline=ProductionPipeline),
    )
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "config", lambda: "config")
    (tmp_path / "source.txt").write_text("story", encoding="utf-8")
    assert service.run("source.txt", "run", stop_after="outline")["status"] == "ready"
    assert calls[1][1] == "story"
    assert calls[1][3]["stop_after"] == "outline"
    assert calls[-1] == ("close",)
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "state.json").write_text("{}", encoding="utf-8")
    calls.clear()
    with pytest.raises(ValueError, match="Unsupported run format"):
        service.run("source.txt", "run", resume=True)
    assert not calls


def test_status_exposes_production_artifacts_and_checks_boundary(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    state = {
        "workflow": "shuohao-v1",
        "status": "ready",
        "stage": "outline",
        "stages": {"outline": {"status": "done", "artifact": "outline.json"}},
        "artifacts": {"outline": "outline.json"},
        "pending_image_tasks": ["task"],
    }
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    service = AppService(tmp_path, restricted=True)
    result = service.run_status("run")
    assert result["artifacts"]["outline"] == str((run / "outline.json").relative_to(tmp_path))
    assert result["stages"]["outline"]["status"] == "done"
    assert result["pending_image_tasks"] == ["task"]
    state["artifacts"]["outline"] = "../../outside"
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(WorkspacePathError):
        service.run_status("run")


def test_cli_stages():
    for stage in (
        "outline",
        "characters",
        "art",
        "script",
        "staging",
        "prompts",
        "assets",
        "frames",
    ):
        assert (
            build_parser()
            .parse_args(["run", "--input", "a", "--output", "b", "--stop-after", stage])
            .stop_after
            == stage
        )


def test_public_interfaces_exclude_training_and_legacy_run_helpers(tmp_path):
    import asyncio

    from pipeline.mcp_server import create_server

    parser = build_parser()
    help_text = parser.format_help()
    assert "prompts" not in help_text
    assert "shots" not in help_text
    assert "scenes" not in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["prompts", "check"])
    server = create_server(tmp_path)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "pipeline_run" in names
    assert (
        not {
            "prompts_list",
            "prompts_check",
            "prompts_stage",
            "prompts_publish",
            "prompts_evaluate",
            "asset_run_fork",
            "shots_list",
            "shot_get",
            "shot_set_mode",
            "scene_review",
        }
        & names
    )
