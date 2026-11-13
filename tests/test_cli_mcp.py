"""Integration tests for the unified CLI and the official MCP stdio server."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from novel_prompt_pipeline.resources import initialize_workspace
from pipeline.cli import main
from pipeline.config import Config
from pipeline.mcp_server import create_server
from pipeline.service import AppService, WorkspacePathError
from prompt_project import PromptRelease


def create_shot_run(workspace: Path) -> Path:
    run = workspace / "runs/shot-run"
    run.mkdir(parents=True)
    state = {
        "identity": {"config": {"batch_size": 1}},
        "nodes": {
            "script": {"value": [{"panel_index": 0, "text": "阿青进屋。"}]},
            "storyboard_0000": {"value": []},
            "continuity_0000": {"value": {}},
        },
        "panels": [
            {
                "panel_index": 0,
                "generation_mode": "i2va",
                "prompt": "image",
                "video_prompt": "video",
                "duration_seconds": 5,
            }
        ],
        "media": {},
        "status": "inferred",
        "end_state": "阿青在屋里。",
    }
    (run / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    release = PromptRelease.load(workspace / "prompts")
    shutil.copytree(release.root, run / "prompts")
    return run


class UnifiedCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        initialize_workspace(self.workspace)
        self.environment = patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-secret-value", "LLM_MODEL": "test-model"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _run(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.workspace), *arguments])
        return code, json.loads(output.getvalue())

    def test_default_asset_library_uses_runs_library(self):
        empty_config = self.workspace / "configs" / "empty.json"
        empty_config.write_text("{}", encoding="utf-8")
        config = AppService(self.workspace, config_path=empty_config, restricted=True).config()
        self.assertEqual(Path(config.asset_library), self.workspace / "runs/library/assets.sqlite3")
        self.assertEqual(
            Path(Config().asset_library).parts[-3:], ("runs", "library", "assets.sqlite3")
        )

    def test_workspace_prompt_listing_and_dry_run_use_same_release(self):
        (self.workspace / "chapter.txt").write_text("阿青走进屋里。", encoding="utf-8")
        code, prompts = self._run("prompts", "list")
        self.assertEqual(code, 0)
        self.assertIn("novel_to_script", prompts["nodes"])
        code, dry = self._run("run", "--input", "chapter.txt", "--output", "runs/test", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(dry["prompt_version"], prompts["version"])
        self.assertFalse((self.workspace / "runs" / "test").exists())
        self.assertNotIn("test-secret-value", json.dumps(dry))

    def test_new_run_can_retry_with_a_released_native_lock_marker(self):
        source = self.workspace / "chapter.txt"
        source.write_text("阿青走进屋里。", encoding="utf-8")
        output = self.workspace / "runs/retry"
        output.mkdir(parents=True)
        (output / ".run.lock").write_bytes(b"")
        service = AppService(self.workspace)
        with patch("pipeline.engine.Pipeline") as pipeline:
            pipeline.return_value.run.return_value = {"output": str(output), "status": "inferred"}
            result = service.run("chapter.txt", "runs/retry")
        self.assertEqual(result["status"], "inferred")
        pipeline.return_value.run.assert_called_once()
        (output / "user-data.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not empty"):
            service.run("chapter.txt", "runs/retry")

    def test_prompt_evaluation_uses_the_same_configured_llm(self):
        from unittest.mock import patch as mock_patch

        cases = self.workspace / "cases.jsonl"
        cases.write_text("{}\n", encoding="utf-8")
        with mock_patch(
            "prompt_project.evaluate.run_evaluation",
            return_value=self.workspace / "runs" / "evaluation",
        ) as evaluate:
            code, result = self._run(
                "prompts", "--config", "configs/pipeline.local.json", "evaluate", "cases.jsonl"
            )
        self.assertEqual(code, 0)
        self.assertEqual(result["output"], str(self.workspace / "runs" / "evaluation"))
        self.assertEqual(evaluate.call_args.kwargs["llm_config"].model, "test-model")

    def test_workspace_path_guard_rejects_absolute_parent_and_symlink_escape(self):
        service = AppService(self.workspace, restricted=True)
        self.assertEqual(
            service.path("prompts/current.json"),
            (self.workspace / "prompts/current.json").resolve(),
        )
        for path in (
            "../outside",
            "a/../../outside",
            str(self.workspace / "prompts/current.json"),
            "C:\\outside",
        ):
            with self.subTest(path=path), self.assertRaises(WorkspacePathError):
                service.path(path)
        with tempfile.TemporaryDirectory() as outside:
            link = self.workspace / "link"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit directory symlinks")
            with self.assertRaises(WorkspacePathError):
                service.path("link/escape.txt")

    def test_mcp_rejects_configured_database_outside_workspace(self):
        config_path = self.workspace / "configs/pipeline.local.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["asset_library"] = "../../external.sqlite3"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        service = AppService(self.workspace, restricted=True)
        with self.assertRaises(WorkspacePathError):
            service.config()

    def test_mcp_checks_auto_discovered_config_before_reading_it(self):
        service = AppService(self.workspace, restricted=True)
        original = service._ensure_inside

        def reject_config_path(path):
            if Path(path).name == "pipeline.local.json":
                raise WorkspacePathError("redirected config")
            return original(path)

        with (
            patch.object(service, "_ensure_inside", side_effect=reject_config_path),
            patch("pipeline.service.Config.load") as loader,
        ):
            with self.assertRaises(WorkspacePathError):
                service.config()
        loader.assert_not_called()

    def test_stdio_server_checks_dotenv_before_loading(self):
        with (
            patch.object(AppService, "_ensure_inside", side_effect=WorkspacePathError("outside")),
            patch("pipeline.mcp_server.load_dotenv") as loader,
        ):
            with self.assertRaises(WorkspacePathError):
                create_server(self.workspace)
        loader.assert_not_called()

    def test_cli_mcp_rejects_external_dotenv_before_loading(self):
        external = self.workspace.parent / "external-secrets.env"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch("pipeline.cli.load_dotenv") as loader:
            code = main(["--workspace", str(self.workspace), "--env-file", str(external), "mcp"])
        self.assertEqual(code, 1)
        self.assertIn("inside the configured workspace", stderr.getvalue())
        loader.assert_not_called()

    def test_mcp_rejects_managed_files_symlink_outside_workspace(self):
        service = AppService(self.workspace, restricted=True)
        managed_files = Path(service.config().asset_library).parent / "files"
        managed_files.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as outside:
            try:
                managed_files.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit directory symlinks")
            with self.assertRaises(WorkspacePathError):
                service.config()

    def test_selected_workspace_uses_its_own_workflow_bindings(self):
        path = self.workspace / "workflows/workflow_bindings.json"
        bindings = json.loads(path.read_text(encoding="utf-8"))
        bindings["profiles"]["qwen"].setdefault("capabilities", {})["note"] = "selected workspace"
        path.write_text(json.dumps(bindings), encoding="utf-8")
        service = AppService(self.workspace)
        self.assertEqual(service.comfy_check("qwen")["capabilities"]["note"], "selected workspace")

    def test_minimal_config_defaults_paths_to_selected_workspace(self):
        path = self.workspace / "configs/custom.json"
        path.write_text(json.dumps({"llm": {"model": "custom-model"}}), encoding="utf-8")
        service = AppService(self.workspace, config_path="configs/custom.json", restricted=True)
        config = service.config()
        self.assertEqual(Path(config.prompt_project), self.workspace / "prompts")
        self.assertEqual(Path(config.asset_library), self.workspace / "runs/library/assets.sqlite3")
        self.assertEqual(Path(config.media.workflows_dir), self.workspace / "workflows")

    def test_prompt_publish_is_isolated_to_selected_workspace(self):
        with tempfile.TemporaryDirectory() as second:
            other = Path(second).resolve()
            initialize_workspace(other)
            service, other_service = AppService(self.workspace), AppService(other)
            before = other_service.prompts_list()["version"]
            source = self.workspace / "prompts/src/nodes/rewrite.md"
            source.write_text(
                source.read_text(encoding="utf-8") + "\n保持叙事清晰。\n", encoding="utf-8"
            )
            result = service.prompts_publish(offline=True)
            self.assertNotEqual(result["version"], before)
            self.assertEqual(other_service.prompts_list()["version"], before)
            self.assertEqual(service.prompts_list()["version"], result["version"])

    def test_workspace_prompt_scripts_are_not_executed_as_trusted_code(self):
        script = self.workspace / "prompts/tools/build.py"
        marker = self.workspace / "script-executed.txt"
        script.write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ran')\n",
            encoding="utf-8",
        )
        try:
            AppService(self.workspace).prompts_build()
        except (ValueError, RuntimeError, OSError):
            pass
        self.assertFalse(marker.exists())

    def test_comfy_lost_submission_records_unknown_without_retry(self):
        class Template:
            def prepare(self, prompt, **values):
                return {"1": {"class_type": "Example", "inputs": {"prompt": prompt}}}

        class Client:
            submissions = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def submit(self, graph):
                self.submissions += 1
                raise TimeoutError("response lost")

        client = Client()
        service = AppService(self.workspace)
        with (
            patch.object(service, "_comfy_template", return_value=Template()),
            patch.object(service, "_comfy_client", return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                service.comfy_submit("qwen", {"prompt": "a room"})
        records = list((self.workspace / "runs/comfyui").glob("submission-*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(client.submissions, 1)
        self.assertNotIn("test-secret-value", json.dumps(record))

    def test_mcp_comfy_download_uses_job_subdirectory(self):
        output = self.workspace / "outputs"
        output.mkdir()
        existing = output / "result.png"
        existing.write_bytes(b"existing")

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def wait(self, prompt_id, timeout):
                return {"outputs": {}}

            def download_outputs(self, history, directory, prompt_id):
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / "result.png"
                target.write_bytes(b"remote")
                return [str(target)]

        service = AppService(self.workspace, restricted=True)
        with patch.object(service, "_comfy_client", return_value=Client()):
            result = service.comfy_wait("job-id", "outputs")
        self.assertEqual(existing.read_bytes(), b"existing")
        downloaded = self.workspace / result["files"][0]
        self.assertEqual(downloaded.read_bytes(), b"remote")
        self.assertNotEqual(downloaded, existing)

    def test_default_prompt_evaluation_directory_is_checked_before_calling_llm(self):
        service = AppService(self.workspace, restricted=True)
        original = service._ensure_inside

        def reject_evaluation_path(path):
            if Path(path).name == "prompt-evaluations":
                raise WorkspacePathError("redirected outside")
            return original(path)

        with (
            patch.object(service, "_ensure_inside", side_effect=reject_evaluation_path),
            patch("prompt_project.evaluate.run_evaluation") as evaluate,
        ):
            with self.assertRaises(WorkspacePathError):
                service.prompts_evaluate("cases.jsonl")
        evaluate.assert_not_called()

    def test_comfy_job_directory_is_checked_before_connecting(self):
        service = AppService(self.workspace, restricted=True)
        original = service._ensure_inside

        def reject_job_path(path):
            if Path(path).name.startswith("job-"):
                raise WorkspacePathError("redirected outside")
            return original(path)

        with (
            patch.object(service, "_ensure_inside", side_effect=reject_job_path),
            patch.object(service, "_comfy_client") as client,
        ):
            with self.assertRaises(WorkspacePathError):
                service.comfy_wait("job-id", "outputs")
        client.assert_not_called()

    def test_cli_shot_mode_change_invalidates_affected_prompt(self):
        run = create_shot_run(self.workspace)
        code, shots = self._run("shots", "list", "runs/shot-run")
        self.assertEqual((code, shots[0]["generation_mode"]), (0, "i2va"))
        code, shot = self._run("shots", "show", "runs/shot-run", "0")
        self.assertEqual((code, shot["panel_index"]), (0, 0))
        code, changed = self._run("shots", "set-mode", "runs/shot-run", "0", "ref2va")
        self.assertEqual(
            (code, changed["generation_mode"], changed["status"]), (0, "ref2va", "needs_recompute")
        )
        self.assertIsNone(changed["video_prompt"])
        self.assertEqual(
            json.loads((run / "state.json").read_text(encoding="utf-8"))["status"], "stale"
        )


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        initialize_workspace(self.workspace)
        self.environment = patch.dict(
            os.environ, {"OPENAI_API_KEY": "mcp-test-secret", "LLM_MODEL": "mcp-test-model"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_in_memory_session_tools_paths_and_secret_redaction(self):
        server = create_server(self.workspace)
        async with create_connected_server_and_client_session(server) as session:
            tools = await session.list_tools()
            mapping = {tool.name: tool for tool in tools.tools}
            for name in (
                "workspace_info",
                "prompts_list",
                "asset_get",
                "asset_update",
                "pipeline_run",
                "run_status",
                "comfy_check",
            ):
                self.assertIn(name, mapping)
            self.assertTrue(mapping["workspace_info"].annotations.readOnlyHint)
            self.assertFalse(mapping["asset_update"].annotations.readOnlyHint)
            info = await session.call_tool("workspace_info", {})
            self.assertFalse(info.isError)
            self.assertTrue(info.structuredContent["llm_key_configured"])
            self.assertNotIn("mcp-test-secret", info.model_dump_json())
            denied = await session.call_tool(
                "asset_link_image", {"asset_id": "unknown", "file": "../outside.png"}
            )
            self.assertTrue(denied.isError)
            self.assertNotIn("mcp-test-secret", denied.model_dump_json())

    async def test_stdio_session_lists_tools_and_accepts_dry_run(self):
        (self.workspace / "chapter.txt").write_text("阿青走进屋里。", encoding="utf-8")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pipeline", "--workspace", str(self.workspace), "mcp"],
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        with anyio.fail_after(25):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    self.assertIn(
                        "pipeline_run", {item.name for item in (await session.list_tools()).tools}
                    )
                    output = await session.call_tool(
                        "pipeline_run",
                        {"input_path": "chapter.txt", "output_path": "runs/dry", "dry_run": True},
                    )
                    self.assertFalse(output.isError, output.model_dump())
                    self.assertGreater(output.structuredContent["source_characters"], 0)
                    self.assertFalse((self.workspace / "runs/dry").exists())
                    self.assertNotIn("mcp-test-secret", output.model_dump_json())

    async def test_asset_media_export_uses_safe_relative_paths(self):
        asset = {
            "asset_id": "character-a",
            "kind": "character",
            "name": "阿青",
            "aliases": "阿青",
            "description": "A character with a plain coat.",
        }
        (self.workspace / "assets.json").write_text(
            json.dumps([asset], ensure_ascii=False), encoding="utf-8"
        )
        (self.workspace / "reference.png").write_bytes(b"reference image bytes")
        server = create_server(self.workspace)
        async with create_connected_server_and_client_session(server) as session:
            imported = await session.call_tool("assets_import", {"file": "assets.json"})
            self.assertFalse(imported.isError, imported.model_dump())
            linked = await session.call_tool(
                "asset_link_image", {"asset_id": "character-a", "file": "reference.png"}
            )
            self.assertFalse(linked.isError, linked.model_dump())
            self.assertFalse(Path(linked.structuredContent["local_path"]).is_absolute())
            exported = await session.call_tool("assets_export", {"file": "exports/library.json"})
            self.assertFalse(exported.isError, exported.model_dump())
            protected = self.workspace / ".env"
            protected.write_text("OPENAI_API_KEY=preserve-me\n", encoding="utf-8")
            denied_overwrite = await session.call_tool("assets_export", {"file": ".env"})
            self.assertTrue(denied_overwrite.isError)
            self.assertEqual(protected.read_text(encoding="utf-8"), "OPENAI_API_KEY=preserve-me\n")
            export = json.loads(
                (self.workspace / "exports/library.json").read_text(encoding="utf-8")
            )
            self.assertFalse(Path(export["assets"][0]["files"][0]["local_path"]).is_absolute())
            imported_again = await session.call_tool(
                "assets_import", {"file": "exports/library.json"}
            )
            self.assertFalse(imported_again.isError, imported_again.model_dump())
            export["assets"][0]["files"][0]["local_path"] = "../outside.png"
            (self.workspace / "exports/unsafe.json").write_text(
                json.dumps(export), encoding="utf-8"
            )
            denied = await session.call_tool("assets_import", {"file": "exports/unsafe.json"})
            self.assertTrue(denied.isError)

    async def test_shot_tools_share_the_same_workspace_scoped_operations(self):
        create_shot_run(self.workspace)
        async with create_connected_server_and_client_session(
            create_server(self.workspace)
        ) as session:
            shot = await session.call_tool("shot_get", {"run": "runs/shot-run", "panel_index": 0})
            self.assertFalse(shot.isError, shot.model_dump())
            self.assertEqual(shot.structuredContent["generation_mode"], "i2va")
            changed = await session.call_tool(
                "shot_set_mode", {"run": "runs/shot-run", "panel_index": 0, "mode": "ref2va"}
            )
            self.assertFalse(changed.isError, changed.model_dump())
            self.assertEqual(changed.structuredContent["status"], "needs_recompute")
            denied = await session.call_tool("shots_list", {"run": "../outside"})
            self.assertTrue(denied.isError)

    async def test_publish_tool_is_used_by_subsequent_pipeline_run(self):
        (self.workspace / "chapter.txt").write_text("阿青走进屋里。", encoding="utf-8")
        source = self.workspace / "prompts/src/nodes/rewrite.md"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n保持叙事清晰。\n", encoding="utf-8"
        )
        async with create_connected_server_and_client_session(
            create_server(self.workspace)
        ) as session:
            before = await session.call_tool("workspace_info", {})
            published = await session.call_tool("prompts_publish", {"offline": True})
            self.assertFalse(published.isError, published.model_dump())
            version = published.structuredContent["version"]
            self.assertNotEqual(version, before.structuredContent["prompt_version"])
            planned = await session.call_tool(
                "pipeline_run",
                {"input_path": "chapter.txt", "output_path": "runs/dry", "dry_run": True},
            )
            self.assertFalse(planned.isError, planned.model_dump())
            self.assertEqual(planned.structuredContent["prompt_version"], version)

    async def test_run_state_symlink_cannot_read_outside_workspace(self):
        run = self.workspace / "runs/symlinked"
        run.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / "private.json"
            source.write_text(
                json.dumps({"status": "private", "nodes": {}, "media": {}}), encoding="utf-8"
            )
            try:
                (run / "state.json").symlink_to(source)
            except OSError:
                self.skipTest("Host does not permit file symlinks")
            async with create_connected_server_and_client_session(
                create_server(self.workspace)
            ) as session:
                result = await session.call_tool("run_status", {"run": "runs/symlinked"})
                self.assertTrue(result.isError)
                self.assertNotIn("private", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()
