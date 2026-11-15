"""Command line entry point: python -m comfyui_client."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .bindings import build_graph
from .errors import BindingError, ComfyUIError, ComfyUITimeoutError
from .transport import ComfyUIClient, OutputFile


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
        job_timeout=getattr(args, "job_timeout", 1800.0),
    )


def _read_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


def _add_graph_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph", help="path to a ready API-format workflow JSON")
    source.add_argument(
        "--profile",
        help="workflow profile from workflow_bindings.json (for example ref2va or i2va)",
    )
    parser.add_argument(
        "--values",
        help="JSON object with profile values: prompt, seed, width, height, length, "
        "first_frame, reference_images, reference_audios",
    )
    parser.add_argument(
        "--bindings-file",
        help="workflow_bindings.json to read profiles from (default: the bundled copy)",
    )
    parser.add_argument(
        "--workflows-dir",
        help="directory holding the profile workflow JSON files (default: next to the bindings)",
    )


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", default="runs/comfyui")
    parser.add_argument(
        "--no-run-subdir",
        dest="run_subdir",
        action="store_false",
        help="write outputs straight into --output-dir instead of a per-prompt subdirectory",
    )
    parser.add_argument(
        "--job-timeout",
        "--timeout",
        dest="job_timeout",
        type=float,
        default=3600.0,
        help="seconds to wait for the prompt to finish (not the HTTP timeout)",
    )


def _graph_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    values_path = getattr(args, "values", None)
    if args.graph:
        if values_path:
            raise ValueError("--values can only be used with --profile")
        return _read_json(args.graph), str(Path(args.graph).resolve())
    if not args.profile:
        raise ValueError("pass --graph or --profile")
    values = _read_json(values_path) if values_path else {}
    return (
        build_graph(
            args.profile,
            values,
            bindings_file=args.bindings_file,
            workflows_dir=args.workflows_dir,
        ),
        f"profile:{args.profile}",
    )


def _add_connection_arguments(parser: argparse.ArgumentParser, *, inherited: bool = False) -> None:
    """Connection options, accepted before or after the subcommand."""
    # ``SUPPRESS`` on the subcommand copy keeps a value parsed before the
    # subcommand from being overwritten by the subparser default.
    missing: Any = argparse.SUPPRESS if inherited else None
    parser.add_argument(
        "--base-url",
        default=missing,
        help="remote ComfyUI base URL (or COMFYUI_URL; COMFYUI_BASE_URL is also accepted)",
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
        prog="python -m comfyui_client", description="Remote ComfyUI API client"
    )
    _add_connection_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    subparsers = [
        commands.add_parser("check", help="check the remote service and queue status"),
        commands.add_parser("queue", help="show running and pending prompts"),
    ]

    upload = commands.add_parser(
        "upload", help="upload an image, audio or other file to ComfyUI input storage"
    )
    upload.add_argument("path")
    upload.add_argument("--subfolder", default="")
    upload.add_argument("--overwrite", action="store_true")
    subparsers.append(upload)

    submit = commands.add_parser(
        "submit", help="queue a bound profile graph or a ready API-format graph JSON"
    )
    _add_graph_arguments(submit)
    submit.add_argument("--output-dir", default="runs/comfyui")
    submit.add_argument("--client-id")
    subparsers.append(submit)

    run = commands.add_parser(
        "run", help="submit a graph, wait for completion and download its outputs"
    )
    _add_graph_arguments(run)
    _add_output_arguments(run)
    run.add_argument("--client-id")
    subparsers.append(run)

    wait = commands.add_parser("wait", help="wait for an existing prompt and download results")
    wait.add_argument("prompt_id")
    _add_output_arguments(wait)
    subparsers.append(wait)

    cancel = commands.add_parser("cancel", help="cancel a running or pending prompt")
    cancel.add_argument("prompt_id")
    subparsers.append(cancel)

    for command in subparsers:
        _add_connection_arguments(command, inherited=True)

    return parser


def _check(client: ComfyUIClient) -> int:
    remote = client.health()
    print(json.dumps({"remote": "ok", "status": remote}, ensure_ascii=False, indent=2))
    return 0


def _queue(client: ComfyUIClient) -> int:
    print(json.dumps(client.queue(), ensure_ascii=False, indent=2))
    return 0


def _upload(client: ComfyUIClient, path: str, subfolder: str, overwrite: bool) -> int:
    print(
        json.dumps(
            {
                "remote_name": client.upload_file(path, subfolder=subfolder, overwrite=overwrite),
                "source": str(Path(path).resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _submit(
    client: ComfyUIClient,
    graph: dict[str, Any],
    source: str,
    output_dir: str,
    client_id: str | None,
) -> int:
    prompt_id = client.submit(graph, client_id=client_id)
    record = {"prompt_id": prompt_id, "workflow": source, "status": "queued"}
    _write_run_record(Path(output_dir).expanduser().resolve(), prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _run(
    client: ComfyUIClient,
    graph: dict[str, Any],
    source: str,
    output_dir: str,
    client_id: str | None,
    job_timeout: float,
    run_subdir: bool,
) -> int:
    output_root = Path(output_dir).expanduser().resolve()
    result = client.run(
        graph,
        output_root,
        client_id=client_id,
        timeout=job_timeout,
        subdir=None if run_subdir else "",
    )
    record = {"workflow": source, "status": "completed", **result.to_dict()}
    _write_run_record(output_root, result.prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _wait(
    client: ComfyUIClient,
    prompt_id: str,
    output_dir: str,
    job_timeout: float,
    run_subdir: bool,
) -> int:
    output_root = Path(output_dir).expanduser().resolve()
    history = client.wait(prompt_id, timeout=job_timeout)
    outputs = client.download_outputs(
        history,
        output_root,
        prompt_id=prompt_id,
        subdir=prompt_id if run_subdir else "",
    )
    # Same keys as `run` so both commands write one record schema.
    record = {"prompt_id": prompt_id, "workflow": None, "status": "completed"}
    record.update(_outputs_record(outputs))
    _write_run_record(output_root, prompt_id, record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _cancel(client: ComfyUIClient, prompt_id: str) -> int:
    print(json.dumps(client.cancel(prompt_id), ensure_ascii=False, indent=2))
    return 0


def _outputs_record(outputs: list[OutputFile]) -> dict[str, Any]:
    return {
        "files": [str(output.path) for output in outputs],
        "outputs": [output.to_dict() for output in outputs],
    }


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
        if args.command == "queue":
            return _queue(client)
        if args.command == "upload":
            return _upload(client, args.path, args.subfolder, args.overwrite)
        if args.command == "submit":
            graph, source = _graph_from_args(args)
            return _submit(client, graph, source, args.output_dir, args.client_id)
        if args.command == "run":
            graph, source = _graph_from_args(args)
            return _run(
                client,
                graph,
                source,
                args.output_dir,
                args.client_id,
                args.job_timeout,
                args.run_subdir,
            )
        if args.command == "wait":
            return _wait(client, args.prompt_id, args.output_dir, args.job_timeout, args.run_subdir)
        if args.command == "cancel":
            return _cancel(client, args.prompt_id)
        parser.error(f"unknown command: {args.command}")
        return 2
    except ComfyUITimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.prompt_id:
            print(f"prompt_id: {exc.prompt_id}", file=sys.stderr)
            print(
                f"hint: the prompt is still queued; rerun with a larger --job-timeout or "
                f"`cancel {exc.prompt_id}`",
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
