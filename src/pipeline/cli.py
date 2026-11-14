"""Unified command line interface for the project."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_dotenv
from .service import AppService
from .storage import json_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novel-pipeline",
        description="Six-stage story production, assets, Codex images, ComfyUI video, and voices",
    )
    parser.add_argument(
        "--workspace", type=Path, default=None, help="workspace root (default: current directory)"
    )
    parser.add_argument(
        "--env-file", type=Path, default=None, help="environment file (default: workspace/.env)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize a standalone workspace")
    init.add_argument(
        "path", nargs="?", type=Path, help="workspace directory (default: --workspace)"
    )

    run = sub.add_parser("run", help="run six-stage production and optionally generate media")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument(
        "--stop-after",
        choices=[
            "outline",
            "characters",
            "art",
            "script",
            "staging",
            "prompts",
            "assets",
            "frames",
        ],
        help="stop after the named production or media stage",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--render", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="check inputs without generating media")

    inspect = sub.add_parser("inspect", help="inspect a run")
    inspect.add_argument("run", type=Path)
    text = sub.add_parser("text", help="inspect or submit Codex native subagent text tasks")
    text_ops = text.add_subparsers(dest="operation", required=True)
    text_list = text_ops.add_parser("list", help="list text tasks awaiting Codex subagents")
    text_list.add_argument("run", type=Path)
    text_show = text_ops.add_parser("show", help="show a text task request")
    text_show.add_argument("run", type=Path)
    text_show.add_argument("task_id")
    text_complete = text_ops.add_parser("complete", help="submit a response for engine validation")
    text_complete.add_argument("run", type=Path)
    text_complete.add_argument("task_id")
    text_complete.add_argument("--file", required=True, type=Path)
    text_complete.add_argument("--agent-id", required=True)
    text_complete.add_argument("--notes", default="")
    images = sub.add_parser("images", help="inspect or complete Codex image tasks")
    image_ops = images.add_subparsers(dest="operation", required=True)
    image_list = image_ops.add_parser("list", help="list image tasks in a run")
    image_list.add_argument("run", type=Path)
    image_plan = image_ops.add_parser("plan", help="show the prepared asset image prompts")
    image_plan.add_argument("run", type=Path)
    image_show = image_ops.add_parser("show", help="show one image task and its references")
    image_show.add_argument("run", type=Path)
    image_show.add_argument("task_id")
    image_complete = image_ops.add_parser("complete", help="attach an inspected image result")
    image_complete.add_argument("run", type=Path)
    image_complete.add_argument("task_id")
    image_complete.add_argument("--file", required=True, type=Path)
    image_complete.add_argument("--notes", required=True, help="observed visual evidence")
    image_complete.add_argument(
        "--main-region",
        type=int,
        nargs=4,
        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="observed main-panel pixel bounds in displayed orientation (scene/prop)",
    )
    image_complete.add_argument(
        "--accept",
        action="store_true",
        required=True,
        help="confirm the image has been visually inspected and accepted",
    )
    tts = sub.add_parser("tts", help="check Breeze TTS or create a voice reference WAV")
    tts.add_argument("--config", type=Path, help="project configuration")
    tts_ops = tts.add_subparsers(dest="operation", required=True)
    tts_ops.add_parser("check", help="check the configured TTS service")
    tts_synthesize = tts_ops.add_parser("synthesize", help="create a WAV from a values JSON file")
    tts_synthesize.add_argument("--values", required=True, type=Path)
    tts_synthesize.add_argument("--output", required=True, type=Path)
    tts_synthesize.add_argument("--asset-id", help="character asset to link the sample to")
    post = sub.add_parser("postproduction", help="prepare dialogue and replace video voices")
    post_ops = post.add_subparsers(dest="operation", required=True)
    post_check = post_ops.add_parser("check", help="offline dependency and optional plan check")
    post_check.add_argument("--run", type=Path)
    post_check.add_argument("--plan", type=Path)
    post_prepare = post_ops.add_parser("prepare", help="synthesize frozen dialogue takes")
    post_prepare.add_argument("--run", required=True, type=Path)
    post_prepare.add_argument("--plan", required=True, type=Path)
    post_prepare.add_argument("--retry-uncertain", action="store_true")
    post_render = post_ops.add_parser("render", help="separate and redub an existing video")
    post_render.add_argument("--run", required=True, type=Path)
    post_render.add_argument("--plan-id", required=True)
    post_render.add_argument("--panel-index", required=True, type=int)
    post_render.add_argument("--video", required=True, type=Path)
    post_render.add_argument("--model", required=True, type=Path)
    attach = sub.add_parser(
        "attach-job", help="attach a known remote job after an uncertain submission"
    )
    attach.add_argument("--run", type=Path, required=True)
    attach.add_argument("--key", required=True)
    attach.add_argument("--prompt-id", required=True)

    assets = sub.add_parser("assets", help="manage the project asset library")
    assets.add_argument("--config", type=Path)
    operations = assets.add_subparsers(dest="operation", required=True)
    listing = operations.add_parser("list")
    listing.add_argument("--kind", choices=("character", "scene", "prop"))
    listing.add_argument("--query")
    listing.add_argument("--archived", action="store_true")
    for operation in ("show", "versions", "archive", "restore"):
        item = operations.add_parser(operation)
        item.add_argument("asset_id")
    for operation in ("import", "export"):
        item = operations.add_parser(operation)
        item.add_argument("file", type=Path)
    update = operations.add_parser("update")
    update.add_argument("asset_id")
    update.add_argument("--description-file", type=Path)
    update.add_argument("--aliases")
    for operation in ("link-image", "link-face"):
        link = operations.add_parser(operation)
        link.add_argument("asset_id")
        link.add_argument("file", type=Path)
    db = sub.add_parser("db", help="inspect or migrate the asset database")
    db.add_argument("--config", type=Path)
    db_operations = db.add_subparsers(dest="operation", required=True)
    db_operations.add_parser("info")
    db_operations.add_parser("migrate")

    comfy = sub.add_parser("comfy", help="use remote ComfyUI workflows")
    comfy.add_argument("--config", type=Path)
    comfy_operations = comfy.add_subparsers(dest="operation", required=True)
    comfy_operations.add_parser("servers", help="list configured servers and stage pools")
    check = comfy_operations.add_parser("check")
    check.add_argument("--profile", default="ref2va")
    check.add_argument("--server", help="configured ComfyUI server name")
    check.add_argument(
        "--remote", action="store_true", help="also connect to the configured server"
    )
    upload = comfy_operations.add_parser("upload")
    upload.add_argument("file", type=Path)
    upload.add_argument("--subfolder", default="novel-pipeline")
    upload.add_argument("--server", help="configured ComfyUI server name")
    submit = comfy_operations.add_parser("submit")
    submit.add_argument("--profile", required=True)
    submit.add_argument(
        "--values",
        required=True,
        type=Path,
        help="JSON file containing prompt and template arguments",
    )
    submit.add_argument("--output-dir", default="runs/comfyui", type=Path)
    submit.add_argument("--server", help="server containing the referenced media")
    wait = comfy_operations.add_parser("wait")
    wait.add_argument("prompt_id")
    wait.add_argument("--output-dir", default="runs/comfyui", type=Path)
    wait.add_argument("--timeout", default=3600.0, type=float)
    wait.add_argument("--server", help="server holding the submitted prompt")

    mcp = sub.add_parser("mcp", help="serve MCP tools on stdin/stdout")
    mcp.add_argument("--config", type=Path)
    return parser


def _assets(service: AppService, args: argparse.Namespace):
    op = args.operation
    if op == "list":
        return service.assets_list(args.kind, args.query, args.archived)
    if op == "show":
        return service.asset_get(args.asset_id)
    if op == "versions":
        return service.asset_versions(args.asset_id)
    if op == "import":
        return service.assets_import(args.file)
    if op == "export":
        return service.assets_export(args.file)
    if op == "update":
        description = (
            service.path(args.description_file).read_text(encoding="utf-8")
            if args.description_file
            else None
        )
        return service.asset_update(args.asset_id, description=description, aliases=args.aliases)
    if op == "link-image":
        return service.asset_link_image(args.asset_id, args.file)
    if op == "link-face":
        return service.asset_link_face(args.asset_id, args.file)
    return service.asset_set_archived(args.asset_id, archived=(op == "archive"))


def _comfy(service: AppService, args: argparse.Namespace):
    if args.operation == "servers":
        return service.comfy_servers()
    if args.operation == "check":
        return service.comfy_check(args.profile, remote=args.remote, server=args.server)
    if args.operation == "upload":
        return service.comfy_upload(args.file, subfolder=args.subfolder, server=args.server)
    if args.operation == "submit":
        values = json.loads(service.path(args.values).read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise ValueError("--values must contain a JSON object")
        return service.comfy_submit(args.profile, values, args.output_dir, server=args.server)
    return service.comfy_wait(
        args.prompt_id, args.output_dir, timeout=args.timeout, server=args.server
    )


def main(argv=None) -> int:
    from novel_prompt_pipeline.resources import default_workspace, initialize_workspace

    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = (args.workspace or default_workspace()).expanduser().resolve()
    try:
        if args.command == "init":
            result = initialize_workspace(args.path or workspace)
        else:
            env_file = args.env_file or workspace / ".env"
            if not env_file.is_absolute():
                env_file = workspace / env_file
            if args.command == "mcp":
                # Server startup parameters are chosen by the local user, but
                # its secret source must still stay within its workspace.
                AppService(workspace, restricted=True)._ensure_inside(env_file)
            load_dotenv(env_file)
            if args.command == "mcp":
                from .mcp_server import create_server

                create_server(workspace, config_path=args.config, env_loaded=True).run(
                    transport="stdio"
                )
                return 0
            service = AppService(workspace, config_path=getattr(args, "config", None))
            if args.command == "run":
                result = service.run(
                    args.input,
                    args.output,
                    resume=args.resume,
                    render=args.render,
                    dry_run=args.dry_run,
                    stop_after=args.stop_after,
                )
            elif args.command == "inspect":
                result = service.run_status(args.run)
            elif args.command == "text":
                if args.operation == "list":
                    result = service.text_tasks_list(args.run)
                elif args.operation == "show":
                    result = service.text_task_get(args.run, args.task_id)
                else:
                    result = service.text_task_complete(
                        args.run, args.task_id, args.file, args.agent_id, args.notes
                    )
            elif args.command == "images":
                if args.operation == "list":
                    result = service.image_tasks_list(args.run)
                elif args.operation == "plan":
                    result = service.image_plan_get(args.run)
                elif args.operation == "show":
                    result = service.image_task_get(args.run, args.task_id)
                else:
                    result = service.image_task_complete(
                        args.run,
                        args.task_id,
                        args.file,
                        args.notes,
                        accepted=args.accept,
                        main_region=args.main_region,
                    )
            elif args.command == "tts":
                result = (
                    service.tts_check()
                    if args.operation == "check"
                    else service.tts_synthesize(args.values, args.output, args.asset_id)
                )
            elif args.command == "postproduction":
                if args.operation == "check":
                    result = service.postproduction_check(args.run, args.plan)
                elif args.operation == "prepare":
                    result = service.postproduction_prepare(
                        args.run, args.plan, args.retry_uncertain
                    )
                else:
                    result = service.postproduction_render(
                        args.run, args.plan_id, args.panel_index, args.video, args.model
                    )
            elif args.command == "attach-job":
                result = service.attach_job(args.run, args.key, args.prompt_id)
            elif args.command == "assets":
                result = _assets(service, args)
            elif args.command == "db":
                result = (
                    service.database_info()
                    if args.operation == "info"
                    else service.database_migrate()
                )
            else:
                result = _comfy(service, args)
        print(json_text(result))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
