"""CLI and MCP expose the same workspace-scoped image-task handoff."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from pipeline.cli import main
from pipeline.images.tasks import ImageTaskPending, prepare_image_task
from pipeline.mcp_server import create_server
from pipeline.service import AppService, WorkspacePathError


def make_task(workspace: Path, *, context: dict | None = None) -> tuple[Path, Path, str]:
    run = workspace / "runs" / "story"
    run.mkdir(parents=True)
    reference = workspace / "reference.png"
    Image.new("RGB", (32, 24), "red").save(reference)
    with pytest.raises(ImageTaskPending) as pending:
        prepare_image_task(
            run,
            "asset_scene",
            prompt="Create the room, using the reference for scene identity.",
            prompt_version="test-release",
            kind="scene",
            width=1024,
            height=576,
            references=[{"path": str(reference), "role": "scene identity"}],
            context=context,
        )
    result = workspace / "incoming" / "result.png"
    result.parent.mkdir()
    Image.new("RGB", (48, 32), "blue").save(result)
    return run, result, pending.value.task_id


def test_cli_and_mcp_expose_full_plan_and_multiple_pending_tasks(tmp_path, capsys):
    run = tmp_path / "runs" / "story"
    run.mkdir(parents=True)
    plan = {
        "schema_version": 1,
        "prompt_version": "release-1",
        "assets": [
            {
                "asset_id": "character-a",
                "name": "阿青",
                "kind": "character",
                "asset_version": "version-1",
                "prompt": "阿青穿外套。",
                "layout_prompt": "白底人物板。",
                "style": {"preset": "cinematic_anime_cg"},
                "design_sha256": None,
                "depends_on": [],
                "reference_roles": [{"sha256": "a" * 64, "role": "面部参考"}],
            }
        ],
    }
    state = {
        "status": "awaiting_image",
        "prompt_version": "release-1",
        "nodes": {},
        "media": {},
        "pending_image_task": "task-1",
        "pending_image_tasks": ["task-1", "task-2"],
        "blocked_image_assets": [{"asset_id": "prop-2", "depends_on": ["prop-1"]}],
        "asset_prompt_preparation": {"status": "ready", "total": 2, "prepared": 2},
    }
    (run / "asset_image_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    assert main(["--workspace", str(tmp_path), "images", "plan", "runs/story"]) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert main(["--workspace", str(tmp_path), "inspect", "runs/story"]) == 0
    status = json.loads(capsys.readouterr().out)
    for key in (
        "pending_image_task",
        "pending_image_tasks",
        "blocked_image_assets",
        "asset_prompt_preparation",
    ):
        assert status[key] == state[key]

    async def exercise():
        server = create_server(tmp_path, env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["image_plan_get"].annotations.readOnlyHint is True
            result = await session.call_tool("image_plan_get", {"run": "runs/story"})
            assert not result.isError, result.model_dump()
            assert result.structuredContent == plan
            status = await session.call_tool("run_status", {"run": "runs/story"})
            assert not status.isError, status.model_dump()
            assert status.structuredContent["pending_image_tasks"] == ["task-1", "task-2"]
            assert status.structuredContent["blocked_image_assets"] == state["blocked_image_assets"]
            denied = await session.call_tool("image_plan_get", {"run": "../outside"})
            assert denied.isError

    anyio.run(exercise)


def test_restricted_plan_rejects_escaped_metadata_path(tmp_path):
    run = tmp_path / "runs" / "story"
    run.mkdir(parents=True)
    (run / "asset_image_plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "prompt_version": "release-1",
                "assets": [{"metadata": {"value": "C:\\private\\secret.png"}}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkspacePathError, match="metadata paths"):
        AppService(tmp_path, restricted=True).image_plan_get("runs/story")


def test_plan_rejects_a_different_pinned_run_version(tmp_path):
    run = tmp_path / "runs" / "story"
    run.mkdir(parents=True)
    (run / "asset_image_plan.json").write_text(
        '{"schema_version":1,"prompt_version":"other","assets":[]}', encoding="utf-8"
    )
    (run / "state.json").write_text('{"prompt_version":"expected"}', encoding="utf-8")
    with pytest.raises(ValueError, match="prompt version"):
        AppService(tmp_path, restricted=True).image_plan_get("runs/story")


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":true,"prompt_version":"v","assets":[]}',
        '{"schema_version":1,"prompt_version":"v","assets":[],"extra":0}',
        '{"schema_version":1,"prompt_version":"v","assets":[],"assets":[]}',
    ],
)
def test_plan_rejects_invalid_or_ambiguous_root_schema(tmp_path, payload):
    run = tmp_path / "runs" / "story"
    run.mkdir(parents=True)
    (run / "asset_image_plan.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        AppService(tmp_path, restricted=True).image_plan_get("runs/story")


def test_restricted_plan_rejects_symlink_to_another_workspace_file(tmp_path):
    run = tmp_path / "runs" / "story"
    run.mkdir(parents=True)
    other = tmp_path / "other.json"
    other.write_text('{"schema_version":1,"prompt_version":"x","assets":[]}', encoding="utf-8")
    try:
        (run / "asset_image_plan.json").symlink_to(other)
    except OSError as exc:
        pytest.skip(f"File symlinks unavailable: {exc}")
    with pytest.raises(WorkspacePathError, match="regular file"):
        AppService(tmp_path, restricted=True).image_plan_get("runs/story")


def test_cli_lists_shows_and_imports_a_task(tmp_path, capsys):
    run, result_file, task_id = make_task(tmp_path)

    def call(*args):
        assert main(["--workspace", str(tmp_path), "images", *args]) == 0
        return json.loads(capsys.readouterr().out)

    tasks = call("list", "runs/story")
    assert [item["task_id"] for item in tasks] == [task_id]
    task = call("show", "runs/story", task_id)
    assert task["status"] == "pending"
    assert Path(task["prompt_file"]).read_text(encoding="utf-8") == task["request"]["prompt"]
    assert Path(task["reference_files"][0]).is_file()
    completed = call(
        "complete",
        "runs/story",
        task_id,
        "--file",
        str(result_file),
        "--notes",
        "Checked the room and the scene reference.",
        "--accept",
    )
    assert completed["status"] == "completed"
    assert completed["result"]["review_accepted"] is True
    assert completed["result"]["notes"] == "Checked the room and the scene reference."
    assert Path(completed["result_file"]).read_bytes() == result_file.read_bytes()
    assert Path(completed["result_file"]).is_relative_to(run)


def test_cli_accepts_observed_main_region_without_resizing_the_image(tmp_path, capsys):
    _, result_file, task_id = make_task(tmp_path)
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "images",
                "complete",
                "runs/story",
                task_id,
                "--file",
                str(result_file),
                "--notes",
                "Checked the observed main panel.",
                "--main-region",
                "2",
                "3",
                "40",
                "28",
                "--accept",
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["result"]["main_region"] == {
        "bounds": [2, 3, 40, 28],
        "width": 48,
        "height": 32,
    }
    assert Path(completed["result_file"]).read_bytes() == result_file.read_bytes()


def test_cli_complete_requires_file_and_observation_notes(tmp_path, capsys):
    _, _, task_id = make_task(tmp_path)
    base = ["--workspace", str(tmp_path), "images", "complete", "runs/story", task_id]
    for suffix in (
        [],
        ["--file", "incoming/result.png"],
        ["--notes", "Observed"],
        ["--file", "incoming/result.png", "--notes", "Observed"],
    ):
        with pytest.raises(SystemExit):
            main(base + suffix)
    capsys.readouterr()


def test_mcp_tools_round_trip_with_relative_paths_and_reject_escape(tmp_path):
    run, result_file, task_id = make_task(tmp_path)

    async def exercise():
        server = create_server(tmp_path, env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools["image_tasks_list"].annotations.readOnlyHint is True
            assert tools["image_task_get"].annotations.readOnlyHint is True
            assert tools["image_task_complete"].annotations.readOnlyHint is False

            listing = await session.call_tool("image_tasks_list", {"run": "runs/story"})
            assert not listing.isError, listing.model_dump()
            assert listing.structuredContent["result"][0]["task_id"] == task_id
            shown = await session.call_tool(
                "image_task_get", {"run": "runs/story", "task_id": task_id}
            )
            assert not shown.isError, shown.model_dump()
            record = shown.structuredContent
            for field in ("task_directory", "prompt_file"):
                assert not Path(record[field]).is_absolute()
                assert (tmp_path / record[field]).exists()
            assert len(record["reference_files"]) == 1
            assert not Path(record["reference_files"][0]).is_absolute()
            assert record["request"]["references"][0]["path"] == "references/01.png"

            bad = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": str(result_file),
                    "notes": "Checked",
                    "accepted": True,
                },
            )
            assert bad.isError
            bad = await session.call_tool("image_tasks_list", {"run": "../outside"})
            assert bad.isError
            rejected = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "incoming/result.png",
                    "notes": "The frame is not correct.",
                },
            )
            assert rejected.isError
            coerced = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "incoming/result.png",
                    "notes": "Checked",
                    "accepted": 1,
                },
            )
            assert coerced.isError
            fractional = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "incoming/result.png",
                    "notes": "Checked",
                    "accepted": True,
                    "main_region": [0, 0, 30.5, 20],
                },
            )
            assert fractional.isError
            out_of_bounds = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "incoming/result.png",
                    "notes": "Checked",
                    "accepted": True,
                    "main_region": [0, 0, 49, 32],
                },
            )
            assert out_of_bounds.isError
            still_pending = await session.call_tool(
                "image_task_get", {"run": "runs/story", "task_id": task_id}
            )
            assert still_pending.structuredContent["status"] == "pending"
            done = await session.call_tool(
                "image_task_complete",
                {
                    "run": "runs/story",
                    "task_id": task_id,
                    "file": "incoming/result.png",
                    "notes": "The room contains one table.",
                    "accepted": True,
                    "main_region": [2, 3, 40, 28],
                },
            )
            assert not done.isError, done.model_dump()
            assert done.structuredContent["status"] == "completed"
            assert done.structuredContent["result"]["review_accepted"] is True
            assert done.structuredContent["result"]["main_region"] == {
                "bounds": [2, 3, 40, 28],
                "width": 48,
                "height": 32,
            }
            path = Path(done.structuredContent["result_file"])
            assert not path.is_absolute()
            assert (tmp_path / path).read_bytes() == result_file.read_bytes()
            assert (tmp_path / path).is_relative_to(run)

    anyio.run(exercise)


def test_restricted_response_rejects_absolute_nested_context_path(tmp_path):
    outside = tmp_path.parent / "private-source.png"
    _, _, task_id = make_task(tmp_path, context={"source_path": str(outside)})
    service = AppService(tmp_path, restricted=True)
    with pytest.raises(WorkspacePathError, match="task-relative"):
        service.image_task_get("runs/story", task_id)
    with pytest.raises(WorkspacePathError, match="task-relative"):
        service.image_tasks_list("runs/story")
    with pytest.raises(WorkspacePathError, match="task-relative"):
        service.image_task_complete(
            "runs/story", task_id, "incoming/result.png", "Checked", accepted=True
        )
    assert not (tmp_path / "runs/story/image_tasks" / task_id / "result.json").exists()


def test_restricted_response_rejects_path_hidden_under_arbitrary_context_key(tmp_path):
    outside = tmp_path.parent / "private-source.png"
    _, _, task_id = make_task(tmp_path, context={"metadata": [{"value": str(outside)}]})
    service = AppService(tmp_path, restricted=True)
    with pytest.raises(WorkspacePathError, match="task-relative"):
        service.image_task_get("runs/story", task_id)


def test_restricted_tasks_reject_symlink_to_outside(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    run = root / "runs" / "story"
    run.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (run / "image_tasks").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    service = AppService(root, restricted=True)
    with pytest.raises(WorkspacePathError):
        service.image_tasks_list("runs/story")
