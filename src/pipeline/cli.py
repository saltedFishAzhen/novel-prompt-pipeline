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
        prog="novel-pipeline", description="Novel prompts, assets, inference, and ComfyUI"
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

    run = sub.add_parser("run", help="infer prompts and optionally generate media")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--config", type=Path)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--render", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="check inputs without LLM or GPU calls")

    inspect = sub.add_parser("inspect", help="inspect a run")
    inspect.add_argument("run", type=Path)
    shots = sub.add_parser("shots", help="inspect or change per-shot generation mode")
    shot_ops = shots.add_subparsers(dest="operation", required=True)
    shot_list = shot_ops.add_parser("list")
    shot_list.add_argument("run", type=Path)
    shot_show = shot_ops.add_parser("show")
    shot_show.add_argument("run", type=Path)
    shot_show.add_argument("panel_index", type=int)
    shot_mode = shot_ops.add_parser("set-mode")
    shot_mode.add_argument("run", type=Path)
    shot_mode.add_argument("panel_index", type=int)
    shot_mode.add_argument("mode", choices=("i2va", "ref2va"))
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

    prompts = sub.add_parser("prompts", help="build, check, publish or evaluate prompt releases")
    prompts.add_argument(
        "--config", type=Path, help="project configuration; uses the same LLM settings as run"
    )
    prompt_operations = prompts.add_subparsers(dest="operation", required=True)
    for operation in ("list", "build", "check", "stage", "publish"):
        item = prompt_operations.add_parser(operation)
        if operation == "list":
            item.add_argument("--version")
        elif operation == "publish":
            publication_mode = item.add_mutually_exclusive_group()
            publication_mode.add_argument("--evaluation", type=Path)
            publication_mode.add_argument("--offline", action="store_true")
    evaluation = prompt_operations.add_parser("evaluate")
    evaluation.add_argument("cases", type=Path, nargs="?")
    target = evaluation.add_mutually_exclusive_group()
    target.add_argument("--version")
    target.add_argument("--candidate", action="store_true")
    evaluation.add_argument("--node", dest="nodes", action="append")
    evaluation.add_argument("--require-coverage", action="store_true")
    evaluation.add_argument("--output-root", type=Path)

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


def _prompts(service: AppService, args: argparse.Namespace):
    if args.operation == "list":
        return service.prompts_list(args.version)
    if args.operation == "build":
        return service.prompts_build()
    if args.operation == "check":
        return service.prompts_build(check=True)
    if args.operation == "stage":
        return service.prompts_stage()
    if args.operation == "publish":
        return service.prompts_publish(args.evaluation, offline=args.offline)
    return service.prompts_evaluate(
        args.cases,
        args.version,
        args.output_root,
        candidate=args.candidate,
        nodes=args.nodes,
        require_coverage=args.require_coverage,
    )


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
                )
            elif args.command == "inspect":
                result = service.run_status(args.run)
            elif args.command == "shots":
                if args.operation == "list":
                    result = service.shots_list(args.run)
                elif args.operation == "show":
                    result = service.shot_get(args.run, args.panel_index)
                else:
                    result = service.shot_set_mode(args.run, args.panel_index, args.mode)
            elif args.command == "attach-job":
                result = service.attach_job(args.run, args.key, args.prompt_id)
            elif args.command == "assets":
                result = _assets(service, args)
            elif args.command == "prompts":
                result = _prompts(service, args)
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
