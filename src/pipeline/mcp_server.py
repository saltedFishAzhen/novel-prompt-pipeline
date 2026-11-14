"""Model Context Protocol interface, restricted to a local workspace.

The server exposes application operations rather than a shell, a SQL console,
or arbitrary remote workflow execution. The official MCP SDK owns the stdio
transport; tool work is moved off the protocol event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictInt

from .config import load_dotenv
from .service import AppService


def create_server(
    workspace: str | Path, config_path: str | Path | None = None, *, env_loaded: bool = False
) -> FastMCP:
    root = Path(workspace).expanduser().resolve()
    app = AppService(root, config_path=config_path, restricted=True)
    if not env_loaded:
        app._ensure_inside(root / ".env")
        load_dotenv(root / ".env")
    server = FastMCP(
        "Novel Prompt Pipeline",
        instructions=(
            "Operate only on paths relative to the configured workspace. "
            "pipeline_run may take a long time. Cancelling a tool request does not "
            "guarantee that an already-submitted remote job has stopped; inspect "
            "the run and resume it before attempting another submission. If a run "
            "awaits an image, inspect its task and reference files, use the active "
            "Codex image-generation capability, copy the generated result into the "
            "workspace, inspect it, complete the task with accepted=true and observation "
            "notes only when it is acceptable, and resume the run."
        ),
    )
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    remote = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    remote_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @server.tool(annotations=read_only)
    async def workspace_info() -> dict[str, Any]:
        """Return current workspace, workflow, and configured service availability."""
        return await asyncio.to_thread(app.workspace_info)

    # Opening an older asset catalog can perform its first schema migration.
    @server.tool(annotations=write)
    async def assets_list(
        kind: Literal["character", "scene", "prop"] | None = None,
        query: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List project assets, optionally filtered by character, scene, prop, or name."""
        return await asyncio.to_thread(app.assets_list, kind, query, include_archived)

    @server.tool(annotations=write)
    async def asset_get(asset_id: str, version: str | None = None) -> dict[str, Any]:
        """Get an asset's metadata and linked files, optionally at an earlier version."""
        return await asyncio.to_thread(app.asset_get, asset_id, version)

    @server.tool(annotations=write)
    async def asset_versions(asset_id: str) -> list[dict[str, Any]]:
        """List retained versions of one asset."""
        return await asyncio.to_thread(app.asset_versions, asset_id)

    @server.tool(annotations=write)
    async def asset_update(
        asset_id: str, description: str | None = None, aliases: str | None = None
    ) -> dict[str, Any]:
        """Create a new asset metadata version, retaining previous versions."""
        return await asyncio.to_thread(app.asset_update, asset_id, description, aliases)

    @server.tool(annotations=write)
    async def asset_link_image(asset_id: str, file: str) -> dict[str, Any]:
        """Copy a workspace-relative image into the asset library as a reference."""
        return await asyncio.to_thread(app.asset_link_image, asset_id, file)

    @server.tool(annotations=write)
    async def asset_link_face(asset_id: str, file: str) -> dict[str, Any]:
        """Attach a workspace portrait as the face reference for a character asset."""
        return await asyncio.to_thread(app.asset_link_face, asset_id, file)

    @server.tool(annotations=write)
    async def asset_archive(asset_id: str) -> dict[str, Any]:
        """Archive an asset without removing its history or files."""
        return await asyncio.to_thread(app.asset_set_archived, asset_id, True)

    @server.tool(annotations=write)
    async def asset_restore(asset_id: str) -> dict[str, Any]:
        """Restore a previously archived asset."""
        return await asyncio.to_thread(app.asset_set_archived, asset_id, False)

    @server.tool(annotations=write)
    async def assets_import(file: str) -> list[dict[str, Any]]:
        """Import a workspace-relative asset JSON file and any contained workspace media."""
        return await asyncio.to_thread(app.assets_import, file)

    @server.tool(annotations=write)
    async def assets_export(file: str) -> dict[str, str]:
        """Export this project's asset catalog to a workspace-relative JSON file."""
        return await asyncio.to_thread(app.assets_export, file)

    @server.tool(annotations=remote)
    async def pipeline_run(
        input_path: str,
        output_path: str,
        resume: bool = False,
        render: bool = False,
        dry_run: bool = False,
        stop_after: Literal[
            "outline", "characters", "art", "script", "staging", "prompts", "assets", "frames"
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Run or resume the pipeline and wait for completion. Paths are workspace-relative.

        stop_after selects a production stage (outline, characters, art, script,
        staging, prompts) or media checkpoint (assets, frames).
        Rendering can pause for a Codex image task and can submit video jobs to
        ComfyUI. Cancelling does not ensure a remote job stops; inspect the run
        before resuming after a disconnect.
        """
        return await asyncio.to_thread(
            app.run,
            input_path,
            output_path,
            resume=resume,
            render=render,
            dry_run=dry_run,
            stop_after=stop_after,
        )

    @server.tool(annotations=read_only)
    async def run_status(run: str) -> dict[str, Any]:
        """Inspect saved status and remote prompt IDs for a workspace-relative run."""
        return await asyncio.to_thread(app.run_status, run)

    @server.tool(annotations=read_only)
    async def text_tasks_list(run: str) -> list[dict[str, Any]]:
        """List text tasks waiting for Codex native subagents in a workspace-relative run."""
        return await asyncio.to_thread(app.text_tasks_list, run)

    @server.tool(annotations=read_only)
    async def text_task_get(run: str, task_id: str) -> dict[str, Any]:
        """Read a text task request for a Codex native subagent."""
        return await asyncio.to_thread(app.text_task_get, run, task_id)

    @server.tool(annotations=write)
    async def text_task_complete(
        run: str, task_id: str, file: str, agent_id: str, notes: str = ""
    ) -> dict[str, Any]:
        """Submit a subagent response file for subsequent engine validation."""
        return await asyncio.to_thread(app.text_task_complete, run, task_id, file, agent_id, notes)

    @server.tool(annotations=read_only)
    async def image_tasks_list(run: str) -> list[dict[str, Any]]:
        """List image-generation checkpoints for a workspace-relative run."""
        return await asyncio.to_thread(app.image_tasks_list, run)

    @server.tool(annotations=read_only)
    async def image_plan_get(run: str) -> dict[str, Any]:
        """Read the completed asset prompt plan for a workspace-relative run."""
        return await asyncio.to_thread(app.image_plan_get, run)

    @server.tool(annotations=read_only)
    async def image_task_get(run: str, task_id: str) -> dict[str, Any]:
        """Show a task prompt and its fixed workspace-relative reference images."""
        return await asyncio.to_thread(app.image_task_get, run, task_id)

    @server.tool(annotations=write)
    async def image_task_complete(
        run: str,
        task_id: str,
        file: str,
        notes: str,
        accepted: StrictBool = False,
        main_region: list[StrictInt] | None = None,
    ) -> dict[str, Any]:
        """Accept an image; optional main_region is [left, top, right, bottom] in display pixels."""
        return await asyncio.to_thread(
            app.image_task_complete,
            run,
            task_id,
            file,
            notes,
            accepted=accepted,
            main_region=main_region,
        )

    @server.tool(annotations=remote_read)
    async def tts_check() -> dict[str, Any]:
        """Check the separately configured Breeze TTS endpoint and its model identity."""
        return await asyncio.to_thread(app.tts_check)

    @server.tool(annotations=remote)
    async def tts_synthesize(
        values: str, output: str, asset_id: str | None = None
    ) -> dict[str, Any]:
        """Create a WAV and provenance from workspace-relative inputs; optionally link a character."""
        return await asyncio.to_thread(app.tts_synthesize, values, output, asset_id)

    @server.tool(annotations=read_only)
    async def postproduction_check(
        run: str | None = None, plan: str | None = None
    ) -> dict[str, Any]:
        """Check offline dependencies and optionally validate a workspace dialogue plan."""
        return await asyncio.to_thread(app.postproduction_check, run, plan)

    @server.tool(annotations=remote)
    async def postproduction_prepare(
        run: str, plan: str, retry_uncertain: StrictBool = False
    ) -> dict[str, Any]:
        """Prepare pinned Breeze dialogue takes; interrupted requests need explicit retry."""
        return await asyncio.to_thread(app.postproduction_prepare, run, plan, retry_uncertain)

    @server.tool(annotations=write)
    async def postproduction_render(
        run: str, plan_id: str, panel_index: StrictInt, video: str, model: str
    ) -> dict[str, Any]:
        """Separate and redub an existing workspace video with local model files."""
        return await asyncio.to_thread(
            app.postproduction_render, run, plan_id, panel_index, video, model
        )

    @server.tool(annotations=write)
    async def attach_job(run: str, key: str, prompt_id: str) -> dict[str, str]:
        """Attach a verified remote prompt ID to a run whose submission response was lost."""
        return await asyncio.to_thread(app.attach_job, run, key, prompt_id)

    @server.tool(annotations=remote_read)
    async def comfy_servers() -> dict[str, Any]:
        """List configured ComfyUI servers and stage pools without exposing URLs or keys."""
        return await asyncio.to_thread(app.comfy_servers)

    @server.tool(annotations=remote_read)
    async def comfy_check(
        profile: str = "ref2va", remote: bool = False, server: str | None = None
    ) -> dict[str, Any]:
        """Inspect a workflow profile; optionally query the configured ComfyUI server."""
        return await asyncio.to_thread(app.comfy_check, profile, remote=remote, server=server)

    @server.tool(annotations=remote)
    async def comfy_upload(
        file: str, subfolder: str = "novel-pipeline", server: str | None = None
    ) -> dict[str, str]:
        """Upload a workspace-relative media file to configured ComfyUI input storage."""
        return await asyncio.to_thread(app.comfy_upload, file, subfolder=subfolder, server=server)

    @server.tool(annotations=remote)
    async def comfy_submit(
        profile: str,
        values: dict[str, Any],
        output_dir: str = "runs/comfyui",
        server: str | None = None,
    ) -> dict[str, Any]:
        """Submit a named workflow profile and save its returned remote prompt ID."""
        return await asyncio.to_thread(app.comfy_submit, profile, values, output_dir, server=server)

    @server.tool(annotations=remote)
    async def comfy_wait(
        prompt_id: str,
        output_dir: str = "runs/comfyui",
        timeout: float = 3600,
        server: str | None = None,
    ) -> dict[str, Any]:
        """Wait for an existing ComfyUI prompt and download outputs into the workspace."""
        return await asyncio.to_thread(
            app.comfy_wait, prompt_id, output_dir, timeout=timeout, server=server
        )

    @server.tool(annotations=write)
    async def database_info() -> dict[str, Any]:
        """Initialize if needed and return the asset database's schema revision."""
        return await asyncio.to_thread(app.database_info)

    @server.tool(annotations=write)
    async def database_migrate() -> dict[str, Any]:
        """Apply the supported asset database migrations."""
        return await asyncio.to_thread(app.database_migrate)

    return server
