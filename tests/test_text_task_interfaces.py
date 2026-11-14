"""CLI and MCP share the Codex text-task handoff and workspace boundary."""

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pipeline.cli import main
from pipeline.mcp_server import create_server
from pipeline.service import AppService, WorkspacePathError
from pipeline.shuohao.tasks import TextTaskPending, prepare_text_task


def make_task(workspace):
    run = workspace / "runs" / "story"
    run.mkdir(parents=True)
    with pytest.raises(TextTaskPending) as pending:
        prepare_text_task(run, stage="outline", prompt="A private prompt")
    (workspace / "response.json").write_text('{"result": "done"}', encoding="utf-8")
    return run, pending.value.task_id


def test_cli_text_round_trip(tmp_path, capsys):
    _, task_id = make_task(tmp_path)
    base = ["--workspace", str(tmp_path), "text"]
    assert main(base + ["list", "runs/story"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["task_id"] == task_id
    assert main(base + ["show", "runs/story", task_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    assert (
        main(
            base
            + [
                "complete",
                "runs/story",
                task_id,
                "--file",
                "response.json",
                "--agent-id",
                "agent-1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "submitted"
    assert result["response"]["agent_id"] == "agent-1"


def test_mcp_text_round_trip_and_workspace_boundary(tmp_path):
    run, task_id = make_task(tmp_path)
    (run / "state.json").write_text(
        json.dumps({"workflow": "shuohao-v1", "pending_text_tasks": [task_id]}), encoding="utf-8"
    )

    async def exercise():
        server = create_server(tmp_path, env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["text_tasks_list"].annotations.readOnlyHint is True
            assert tools["text_task_complete"].annotations.readOnlyHint is False
            shown = await session.call_tool(
                "text_task_get", {"run": "runs/story", "task_id": task_id}
            )
            assert not shown.isError
            assert not Path(shown.structuredContent["prompt_file"]).is_absolute()
            status = await session.call_tool("run_status", {"run": "runs/story"})
            assert status.structuredContent["pending_text_tasks"] == [task_id]
            denied = await session.call_tool(
                "text_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": str(tmp_path / "response.json"),
                    "agent_id": "agent-1",
                },
            )
            assert denied.isError
            denied = await session.call_tool("text_tasks_list", {"run": "../outside"})
            assert denied.isError
            completed = await session.call_tool(
                "text_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "response.json",
                    "agent_id": "agent-1",
                },
            )
            assert not completed.isError
            assert completed.structuredContent["status"] == "submitted"
            assert not Path(completed.structuredContent["response_file"]).is_absolute()

    anyio.run(exercise)


def test_restricted_service_rejects_external_task_metadata(tmp_path):
    service = AppService(tmp_path, restricted=True)
    with pytest.raises(WorkspacePathError):
        service._present_text_task({"request": {"prompt_file": "../secret"}})
