"""Offline integration coverage for prompt workflow interfaces."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from novel_prompt_pipeline.resources import initialize_workspace
from pipeline.cli import main
from pipeline.mcp_server import create_server


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    return workspace.resolve()


def _cli(workspace: Path, capsys, *arguments: str) -> dict:
    assert main(["--workspace", str(workspace), *arguments]) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_lists_coverage_and_stage_does_not_activate(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    listing = _cli(workspace, capsys, "prompts", "list")
    assert listing["node_count"] == len(listing["nodes"]) == 16
    assert all(
        node["default_case_count"] >= 1 and node["contract_case_count"] >= 1
        for node in listing["nodes"].values()
    )
    current_file = workspace / "prompts" / "current.json"
    before = current_file.read_bytes()
    staged = _cli(workspace, capsys, "prompts", "stage")
    assert staged["activated"] is False
    assert current_file.read_bytes() == before
    assert _cli(workspace, capsys, "prompts", "list")["version"] == listing["version"]


def test_cli_candidate_evaluation_uses_default_suite_and_configured_llm(
    tmp_path, capsys, monkeypatch
):
    workspace = _workspace(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "offline-interface-test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-interface-test-key")
    node = "novel_to_script"
    fixtures = workspace / "prompts/tests/fixtures/node_cases.jsonl"
    case = next(
        case
        for case in (json.loads(line) for line in fixtures.read_text(encoding="utf-8").splitlines())
        if case["node"] == node
    )
    with patch("prompt_project.evaluate.OpenAILLM") as llm:
        llm.return_value.complete.return_value = case["valid_output"]
        llm.return_value.last_request_id = "offline-request"
        result = _cli(workspace, capsys, "prompts", "evaluate", "--candidate", "--node", node)
    summary = json.loads((Path(result["output"]) / "run.json").read_text(encoding="utf-8"))
    suite = workspace / "prompts/evaluations/suites/default.jsonl"
    assert (Path(result["output"]) / "cases.jsonl").read_bytes() == suite.read_bytes()
    assert result["candidate"] is True
    assert summary["release"] == result["prompt_version"]
    assert summary["selected_nodes"] == summary["evaluated_nodes"] == [node]
    assert summary["case_count"] >= 1
    assert summary["structural_pass_count"] == summary["case_count"]
    assert llm.call_args.args[0].model == "offline-interface-test-model"
    assert llm.return_value.complete.call_count == summary["case_count"]
    llm.return_value.close.assert_called_once()


def test_cli_and_mcp_publish_route_report_and_mcp_rejects_escape(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    report = workspace / "runs/evaluation/run.json"
    release = SimpleNamespace(version="test-version", root=workspace / "prompts/releases/test")
    with patch("prompt_project.compiler.publish", return_value=release) as publish:
        result = _cli(
            workspace, capsys, "prompts", "publish", "--evaluation", "runs/evaluation/run.json"
        )
        assert result["version"] == release.version
        assert publish.call_args.kwargs["evaluation"] == report.resolve()

        async def exercise_mcp():
            server = create_server(workspace, env_loaded=True)
            async with create_connected_server_and_client_session(server) as session:
                accepted = await session.call_tool(
                    "prompts_publish", {"evaluation": "runs/evaluation/run.json"}
                )
                assert not accepted.isError
                assert accepted.structuredContent["version"] == release.version
                assert publish.call_args.kwargs["evaluation"] == report.resolve()
                denied = await session.call_tool(
                    "prompts_publish", {"evaluation": "../outside.json"}
                )
                assert denied.isError
                assert publish.call_count == 2

        anyio.run(exercise_mcp)


def test_cli_and_mcp_require_report_unless_offline_is_explicit(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    assert main(["--workspace", str(workspace), "prompts", "publish"]) != 0
    capsys.readouterr()
    published = _cli(workspace, capsys, "prompts", "publish", "--offline")
    assert published["version"]

    async def exercise_mcp():
        server = create_server(workspace, env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            missing = await session.call_tool("prompts_publish", {})
            assert missing.isError
            conflicting = await session.call_tool(
                "prompts_publish", {"offline": True, "evaluation": "runs/report.json"}
            )
            assert conflicting.isError
            offline = await session.call_tool("prompts_publish", {"offline": True})
            assert not offline.isError
            assert offline.structuredContent["version"] == published["version"]

    anyio.run(exercise_mcp)
