"""Integration tests for the unified CLI and the official MCP stdio server."""

from __future__ import annotations

import contextlib
import io
import json
import os
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


class UnifiedCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        initialize_workspace(self.workspace)

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
        bindings["profiles"]["ref2va"].setdefault("capabilities", {})["note"] = "selected workspace"
        path.write_text(json.dumps(bindings), encoding="utf-8")
        service = AppService(self.workspace)
        self.assertEqual(
            service.comfy_check("ref2va")["capabilities"]["note"], "selected workspace"
        )

    def test_comfy_lost_submission_records_unknown_without_retry(self):
        class Template:
            has_first_frame = True

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
                service.comfy_submit("ref2va", {"prompt": "a room", "first_frame": "first.png"})
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


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        initialize_workspace(self.workspace)

    async def test_in_memory_session_tools_and_paths(self):
        server = create_server(self.workspace)
        async with create_connected_server_and_client_session(server) as session:
            tools = await session.list_tools()
            mapping = {tool.name: tool for tool in tools.tools}
            for name in (
                "workspace_info",
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
            self.assertEqual(info.structuredContent["workflow"], "shuohao-v1")
            denied = await session.call_tool(
                "asset_link_image", {"asset_id": "unknown", "file": "../outside.png"}
            )
            self.assertTrue(denied.isError)

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
            protected.write_text("PRIVATE_VALUE=preserve-me\n", encoding="utf-8")
            denied_overwrite = await session.call_tool("assets_export", {"file": ".env"})
            self.assertTrue(denied_overwrite.isError)
            self.assertEqual(protected.read_text(encoding="utf-8"), "PRIVATE_VALUE=preserve-me\n")
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
