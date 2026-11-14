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
        prog="python -m comfyui_client", description="Remote ComfyUI API client"
    )
    parser.add_argument(
        "--base-url",
        help="remote ComfyUI base URL (or COMFYUI_URL; COMFYUI_BASE_URL is also accepted)",
    )
    parser.add_argument("--api-key", help="optional bearer token (or COMFYUI_API_KEY)")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="check the remote service and queue status")

    upload = commands.add_parser(
        "upload", help="upload an image, audio or other file to ComfyUI input storage"
    )
    upload.add_argument("path")
    upload.add_argument("--subfolder", default="codex")

    submit = commands.add_parser("submit", help="queue an API-format graph JSON file")
    submit.add_argument("--graph", required=True, help="path to an API-format workflow JSON")
    submit.add_argument("--output-dir", default="runs/comfyui")
    submit.add_argument("--client-id")

    run = commands.add_parser(
        "run", help="submit a graph, wait for completion and download its outputs"
    )
    run.add_argument("--graph", required=True, help="path to an API-format workflow JSON")
    run.add_argument("--output-dir", default="runs/comfyui")
    run.add_argument("--client-id")
    run.add_argument("--timeout", type=float, default=3600.0)

    wait = commands.add_parser("wait", help="wait for an existing prompt and download results")
    wait.add_argument("prompt_id")
    wait.add_argument("--output-dir", default="runs/comfyui")
    wait.add_argument("--timeout", type=float, default=3600.0)

    return parser


def _check(client: ComfyUIClient) -> int:
    remote = client.health()
    print(json.dumps({"remote": "ok", "status": remote}, ensure_ascii=False, indent=2))
    return 0


def _upload(client: ComfyUIClient, path: str, subfolder: str) -> int:
    print(
        json.dumps(
            {
                "remote_name": client.upload_file(path, subfolder=subfolder),
                "source": str(Path(path).resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _submit(client: ComfyUIClient, graph_path: str, output_dir: str, client_id: str | None) -> int:
    prompt_id = client.submit(_read_json(graph_path), client_id=client_id)
    record = {
        "prompt_id": prompt_id,
        "graph": str(Path(graph_path).resolve()),
        "status": "queued",
    }
    _write_run_record(Path(output_dir).expanduser().resolve(), prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _run(
    client: ComfyUIClient,
    graph_path: str,
    output_dir: str,
    client_id: str | None,
    timeout: float,
) -> int:
    result = client.run(
        _read_json(graph_path),
        Path(output_dir).expanduser().resolve(),
        client_id=client_id,
        timeout=timeout,
    )
    prompt_id = result["prompt_id"]
    record = {
        "prompt_id": prompt_id,
        "graph": str(Path(graph_path).resolve()),
        "status": "completed",
        "files": result["files"],
    }
    _write_run_record(Path(output_dir).expanduser().resolve(), prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _wait(client: ComfyUIClient, prompt_id: str, output_dir: str, timeout: float) -> int:
    output_root = Path(output_dir).expanduser().resolve()
    history = client.wait(prompt_id, timeout=timeout)
    files = client.download_outputs(history, output_root, prompt_id=prompt_id)
    record = {"prompt_id": prompt_id, "status": "completed", "files": files}
    _write_run_record(output_root, prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        client = _client(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "check":
            return _check(client)
        if args.command == "upload":
            return _upload(client, args.path, args.subfolder)
        if args.command == "submit":
            return _submit(client, args.graph, args.output_dir, args.client_id)
        if args.command == "run":
            return _run(client, args.graph, args.output_dir, args.client_id, args.timeout)
        if args.command == "wait":
            return _wait(client, args.prompt_id, args.output_dir, args.timeout)
        parser.error(f"unknown command: {args.command}")
    except (ComfyUIError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        prompt_id = getattr(exc, "prompt_id", None)
        if isinstance(prompt_id, str) and prompt_id:
            print(f"prompt_id: {prompt_id}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _write_run_record(output_dir: Path, prompt_id: str, record: dict[str, Any]) -> None:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", prompt_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"comfy-{safe_id}.json"
    temp = destination.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, destination)


if __name__ == "__main__":
    raise SystemExit(main())
