"""Command line entry point: python -m comfyui_client."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .client import ComfyUIClient, ComfyUIError
from .templates import BindingError, get_default_template


def _client(args: argparse.Namespace) -> ComfyUIClient:
    base_url = args.base_url or os.environ.get("COMFYUI_URL") or os.environ.get("COMFYUI_BASE_URL")
    if not base_url:
        raise ValueError("Set COMFYUI_URL or pass --base-url")
    api_key = args.api_key or os.environ.get("COMFYUI_API_KEY")
    return ComfyUIClient(
        base_url,
        api_key=api_key,
        timeout=args.request_timeout,
        poll_interval=args.poll_interval,
    )


def _read_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m comfyui_client", description="Remote ComfyUI API helper"
    )
    parser.add_argument(
        "--base-url",
        help="remote ComfyUI base URL (or COMFYUI_URL; COMFYUI_BASE_URL is also accepted)",
    )
    parser.add_argument("--api-key", help="optional bearer token (or COMFYUI_API_KEY)")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check", help="check local workflow profile and optionally remote service"
    )
    check.add_argument("--profile", choices=("qwen", "qwen-unlock", "ref2va", "i2va"))

    upload = commands.add_parser(
        "upload", help="upload an image, audio or other file to ComfyUI input storage"
    )
    upload.add_argument("path")
    upload.add_argument("--subfolder", default="codex")

    submit = commands.add_parser("submit", help="bind and submit a checked-in workflow template")
    submit.add_argument("--profile", required=True)
    submit.add_argument("--values", required=True, help="JSON object with supported profile values")
    submit.add_argument("--output-dir", default="runs/comfyui")
    submit.add_argument("--client-id")

    wait = commands.add_parser("wait", help="wait for an existing prompt and download results")
    wait.add_argument("prompt_id")
    wait.add_argument("--output-dir", default="runs/comfyui")
    wait.add_argument("--timeout", type=float, default=3600.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            if args.profile:
                template = get_default_template(args.profile)
                print(
                    json.dumps(
                        {
                            "profile": args.profile,
                            "capabilities": template.capabilities,
                            "workflow_nodes": len(
                                [k for k in template._graph if not str(k).startswith("_")]
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            if args.base_url or os.environ.get("COMFYUI_URL") or os.environ.get("COMFYUI_BASE_URL"):
                remote = _client(args).health()
                print(json.dumps({"remote": "ok", "status": remote}, ensure_ascii=False, indent=2))
            if not args.profile and not (
                args.base_url or os.environ.get("COMFYUI_URL") or os.environ.get("COMFYUI_BASE_URL")
            ):
                parser.error("check needs --profile or COMFYUI_URL")
            return 0

        client = _client(args)
        if args.command == "upload":
            print(
                json.dumps(
                    {
                        "remote_name": client.upload_file(args.path, subfolder=args.subfolder),
                        "source": str(Path(args.path).resolve()),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "submit":
            values = _read_json(args.values)
            prompt = values.pop("prompt", None)
            if not isinstance(prompt, str):
                raise ValueError("--values JSON must contain a string 'prompt'")
            template = get_default_template(args.profile)
            graph = template.prepare(prompt, **values)
            prompt_id = client.submit(graph, client_id=args.client_id)
            output_dir = Path(args.output_dir).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            run_record = {
                "profile": args.profile,
                "prompt_id": prompt_id,
                "workflow": template.name,
                "values": {"prompt": prompt, **values},
                "status": "queued",
            }
            _write_run_record(output_dir, prompt_id, run_record)
            print(json.dumps(run_record, ensure_ascii=False, indent=2))
            return 0
        if args.command == "wait":
            history = client.wait(args.prompt_id, timeout=args.timeout)
            files = client.download_outputs(history, args.output_dir, prompt_id=args.prompt_id)
            record = {
                "prompt_id": args.prompt_id,
                "status": "completed",
                "files": files,
            }
            _write_run_record(Path(args.output_dir).expanduser().resolve(), args.prompt_id, record)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
    except (ComfyUIError, BindingError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if getattr(exc, "prompt_id", None):
            print(f"prompt_id: {exc.prompt_id}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 2


def _write_run_record(output_dir: Path, prompt_id: str, record: dict[str, Any]) -> None:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", prompt_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"comfy-{safe_id}.json"
    temp = destination.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, destination)


if __name__ == "__main__":
    raise SystemExit(main())
