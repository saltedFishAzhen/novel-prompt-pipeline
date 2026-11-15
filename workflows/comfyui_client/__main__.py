"""Command line entry point: python -m comfyui_client / comfyui-remote."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .bindings import build_graph
from .errors import BindingError, ComfyUIError, ComfyUITimeoutError
from .storyboard import load_storyboard_bundle
from .transport import ComfyUIClient, OutputFile


def _client(args: argparse.Namespace) -> ComfyUIClient:
    base_url = args.base_url or os.environ.get("COMFYUI_URL")
    if not base_url:
        raise ValueError("Set COMFYUI_URL or pass --base-url")
    api_key = args.api_key or os.environ.get("COMFYUI_API_KEY")
    return ComfyUIClient(
        base_url,
        api_key=api_key,
        timeout=args.request_timeout,
        poll_interval=args.poll_interval,
        job_timeout=getattr(args, "job_timeout", 1800.0),
    )


def _add_connection_arguments(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    """Connection options, accepted before or after the subcommand."""
    # ``SUPPRESS`` on the subcommand copy keeps a value parsed before the
    # subcommand from being overwritten by the subparser default.
    missing: Any = argparse.SUPPRESS if inherited else None
    parser.add_argument(
        "--base-url", default=missing, help="remote ComfyUI base URL (or COMFYUI_URL)"
    )
    parser.add_argument(
        "--api-key", default=missing, help="optional bearer token (or COMFYUI_API_KEY)"
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=argparse.SUPPRESS if inherited else 30.0,
        help="seconds per HTTP request",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=argparse.SUPPRESS if inherited else 1.0,
        help="seconds between history polls",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comfyui-remote",
        description="Run a novel-storyboard H3 production package on a remote ComfyUI server",
    )
    _add_connection_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    storyboard = commands.add_parser(
        "storyboard", help="upload, generate and download every segment of an H3 package"
    )
    storyboard.add_argument("--manifest", required=True, help="path to the package manifest.json")
    storyboard.add_argument(
        "--output-dir", default="runs/storyboard", help="directory for the videos and run records"
    )
    storyboard.add_argument(
        "--job-timeout", type=float, default=3600.0, help="seconds allowed per segment"
    )
    storyboard.add_argument(
        "--submit-only", action="store_true", help="queue every segment without waiting"
    )
    storyboard.add_argument(
        "--dry-run", action="store_true", help="validate the package locally, without a server"
    )

    commands.add_parser("check", help="check the remote service")
    commands.add_parser("queue", help="show running and pending prompts")

    wait = commands.add_parser("wait", help="wait for an existing prompt and download results")
    wait.add_argument("prompt_id")
    wait.add_argument("--output-dir", default="runs/storyboard")
    wait.add_argument("--job-timeout", type=float, default=3600.0)

    cancel = commands.add_parser("cancel", help="cancel a running or pending prompt")
    cancel.add_argument("prompt_id")

    for command in commands.choices.values():
        _add_connection_arguments(command, inherited=True)

    return parser


def _check(client: ComfyUIClient) -> int:
    print(json.dumps({"remote": "ok", "status": client.health()}, ensure_ascii=False, indent=2))
    return 0


def _queue(client: ComfyUIClient) -> int:
    print(json.dumps(client.queue(), ensure_ascii=False, indent=2))
    return 0


def _wait(
    client: ComfyUIClient,
    prompt_id: str,
    output_dir: str,
    job_timeout: float,
) -> int:
    output_root = Path(output_dir).expanduser().resolve()
    history = client.wait(prompt_id, timeout=job_timeout)
    outputs = client.download_outputs(
        history, output_root, prompt_id=prompt_id, subdir=prompt_id
    )
    record = {"prompt_id": prompt_id, "status": "completed", **_outputs_record(outputs)}
    _write_run_record(output_root, prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cancel(client: ComfyUIClient, prompt_id: str) -> int:
    print(json.dumps(client.cancel(prompt_id), ensure_ascii=False, indent=2))
    return 0


def _storyboard(args: argparse.Namespace, client: ComfyUIClient | None = None) -> int:
    segments = load_storyboard_bundle(args.manifest)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "segments": [
                        {
                            "segment": item.name,
                            "seconds": item.seconds,
                            "aspect_ratio": item.aspect_ratio,
                            "pictures": len(item.pictures),
                        }
                        for item in segments
                    ],
                    "total_seconds": round(sum(item.seconds for item in segments), 3),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if client is None:
        raise ValueError("storyboard command requires a ComfyUI client")
    output_root = Path(args.output_dir).expanduser().resolve()
    batch_id = uuid.uuid4().hex
    for item in segments:
        remote_names = [
            client.upload_file(path, subfolder=f"storyboard/{batch_id}/{item.name}")
            for path in item.pictures
        ]
        graph = build_graph(
            {
                "prompt": item.prompt,
                "length": item.seconds,
                "aspect_ratio": item.aspect_ratio,
                "reference_images": remote_names,
            }
        )
        prompt_id = client.submit(graph)
        record: dict[str, Any] = {
            "batch_id": batch_id,
            "segment": item.name,
            "seconds": item.seconds,
            "aspect_ratio": item.aspect_ratio,
            "reference_images": remote_names,
            "prompt_id": prompt_id,
            "status": "queued",
        }
        _write_run_record(output_root, prompt_id, record)
        if not args.submit_only:
            history = client.wait(prompt_id, timeout=args.job_timeout)
            outputs = client.download_outputs(
                history, output_root, prompt_id=prompt_id, subdir=item.name
            )
            record.update({"status": "completed", **_outputs_record(outputs)})
            _write_run_record(output_root, prompt_id, record)
        print(json.dumps(record, ensure_ascii=False))
    return 0


def _outputs_record(outputs: list[OutputFile]) -> dict[str, Any]:
    return {
        "files": [str(output.path) for output in outputs],
        "outputs": [output.to_dict() for output in outputs],
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "storyboard" and args.dry_run:
        try:
            return _storyboard(args)
        except (BindingError, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    try:
        client = _client(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "check":
            return _check(client)
        if args.command == "queue":
            return _queue(client)
        if args.command == "wait":
            return _wait(client, args.prompt_id, args.output_dir, args.job_timeout)
        if args.command == "cancel":
            return _cancel(client, args.prompt_id)
        return _storyboard(args, client)
    except ComfyUITimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.prompt_id:
            print(f"prompt_id: {exc.prompt_id}", file=sys.stderr)
            print(
                f"hint: the prompt is still queued; rerun with a larger --job-timeout or "
                f"`wait {exc.prompt_id}`",
                file=sys.stderr,
            )
        return 1
    except (ComfyUIError, BindingError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        prompt_id = getattr(exc, "prompt_id", None)
        if isinstance(prompt_id, str) and prompt_id:
            print(f"prompt_id: {prompt_id}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _write_run_record(output_dir: Path, prompt_id: str, record: dict[str, Any]) -> None:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", prompt_id) or "prompt"
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"comfy-{safe_id}.json"
    # A unique temp name keeps two processes writing the same prompt id apart.
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".comfy-{safe_id}-", suffix=".tmp", dir=output_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp_name, destination)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
