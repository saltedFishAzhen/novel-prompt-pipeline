"""Compile a request JSON to an H3 prompt or ComfyUI values; never generate video."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from comfyui_client import ComfyUIClient, ComfyUIError

from .client import H3Client, H3Error, ReferenceImage
from .prompts import I2VA_FIELDS, I2VA_HEADER, REF2VA_FIELDS


def _read_image(value: Any, root: Path) -> ReferenceImage:
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict) or "path" not in value:
        raise H3Error("each image must be a local path or an object with path, name, remote_name")
    unknown = set(value) - {"path", "name", "remote_name"}
    if unknown:
        raise H3Error("unknown image fields: " + ", ".join(sorted(unknown)))
    if not isinstance(value["path"], str) or not value["path"].strip():
        raise H3Error("image path must be a non-empty string")
    path = Path(value["path"]).expanduser()
    return ReferenceImage(
        path if path.is_absolute() else root / path,
        name=value.get("name"),
        remote_name=value.get("remote_name"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, prog="python -m h3_client")
    parser.add_argument(
        "--input", required=True, help="request JSON (image paths relative to this file)"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="output values JSON, or plain prompt text with --prompt-only",
    )
    parser.add_argument(
        "--env-file", default=".env", help="dotenv file; default .env in current directory"
    )
    parser.add_argument("--model", help="vision-capable LLM (or H3_LLM_MODEL / OPENAI_MODEL)")
    parser.add_argument("--base-url", help="LLM OpenAI-compatible base URL including /v1 if needed")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        help="LLM reasoning effort (or H3_LLM_REASONING_EFFORT; defaults to low)",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="LLM request timeout in seconds"
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload images missing remote_name (incompatible with --prompt-only)",
    )
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="write H3 prompt text only (incompatible with --upload)",
    )
    parser.add_argument("--comfyui-base-url", help="upload destination (or COMFYUI_URL)")
    return parser


def _previous_result(path: Path) -> bool:
    """Recognize values or complete H3 prompt text without treating arbitrary files as output."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return False
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    fields = {
        "prompt",
        "first_frame",
        "reference_images",
        "reference_audios",
        "seed",
        "width",
        "height",
        "length",
        "negative_prompt",
    }
    if isinstance(value, dict) and set(value) <= fields and isinstance(value.get("prompt"), str):
        # i2va values carry first_frame; ref2va values carry reference_images.
        references = value.get("reference_images")
        if isinstance(value.get("first_frame"), str) or (
            isinstance(references, list)
            and bool(references)
            and all(isinstance(item, str) for item in references)
        ):
            return True
    prompt = text.strip()
    if prompt.startswith(I2VA_HEADER + "\n"):
        expected = I2VA_FIELDS
    elif prompt.startswith("subject_definitions:"):
        expected = REF2VA_FIELDS
    else:
        return False
    headings = re.findall(r"(?m)^(\w+):", prompt)
    return [heading for heading in headings if heading in expected] == list(expected)


def _quarantine_previous_result(path: Path) -> None:
    """Move a recognizable previous result aside so it cannot be consumed by mistake.

    Deleting it would lose a result the caller may still want, while leaving it in
    place would let the next command in a chain read a stale file after a failure.
    Renaming satisfies both; anything unrecognized is never touched.
    """
    if not _previous_result(path):
        return
    stale = path.with_name(path.name + ".stale")
    try:
        os.replace(path, stale)
    except OSError as exc:
        print(f"warning: could not move stale output aside: {exc}", file=sys.stderr)
        return
    print(f"warning: previous result moved to {stale}", file=sys.stderr)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output: Path | None = None
    protected: set[Path] = set()
    try:
        source = Path(args.input).expanduser().resolve()
        output = Path(args.output).expanduser().resolve()
        protected = {source, Path(args.env_file).expanduser().resolve(), Path(".env").resolve()}
        load_dotenv(args.env_file, override=False)
        request = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(request, dict):
            raise H3Error("input must contain a JSON object")
        unknown = set(request) - {"type", "storyboard", "reference_images", "values"}
        if unknown:
            raise H3Error("unknown request fields: " + ", ".join(sorted(unknown)))
        raw_images = request.get("reference_images")
        if not isinstance(raw_images, list):
            raise H3Error("reference_images must be an ordered array")
        # Protect input files even if a later image or value fails validation.
        for item in raw_images:
            path = (
                item
                if isinstance(item, str)
                else item.get("path")
                if isinstance(item, dict)
                else None
            )
            if isinstance(path, str) and path.strip():
                image_path = Path(path).expanduser()
                protected.add(
                    (
                        image_path if image_path.is_absolute() else source.parent / image_path
                    ).resolve()
                )
        images = [_read_image(value, source.parent) for value in raw_images]
        if args.prompt_only and args.upload:
            raise H3Error("--prompt-only and --upload are mutually exclusive")
        settings = request.get("values", {})
        if not isinstance(settings, dict):
            raise H3Error("values must be an object of optional generation settings")
        named_paths = {image.path for image in images if image.remote_name}
        if (
            not args.prompt_only
            and not args.upload
            and any(image.path not in named_paths for image in images)
        ):
            raise H3Error("provide remote_name for images already uploaded, or use --upload")
        comfy_url = (
            args.comfyui_base_url or os.getenv("COMFYUI_URL") or os.getenv("COMFYUI_BASE_URL")
        )
        if args.upload and not comfy_url:
            raise H3Error("--upload requires COMFYUI_URL or --comfyui-base-url")
        if output in protected or output.name.lower().startswith(".env"):
            raise H3Error("output must not overwrite the request, a reference image or an env file")
        with H3Client(
            model=args.model,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
        ) as compiler:
            result = compiler.compile(request.get("type"), request.get("storyboard"), images)
        if args.prompt_only:
            _atomic_write(output, result.prompt + "\n")
            print(
                json.dumps(
                    {"profile": result.profile, "prompt_file": str(output), "mode": "prompt-only"},
                    ensure_ascii=False,
                )
            )
            return 0
        if args.upload:
            with ComfyUIClient(comfy_url, api_key=os.getenv("COMFYUI_API_KEY")) as comfy:
                values = result.to_values(comfy, values=settings)
        else:
            values = result.to_values(values=settings)
        _atomic_write(output, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps({"profile": result.profile, "values_file": str(output)}, ensure_ascii=False)
        )
        return 0
    except (H3Error, ComfyUIError, OSError, ValueError, TypeError) as exc:
        if (
            output is not None
            and output not in protected
            and not output.name.lower().startswith(".env")
        ):
            _quarantine_previous_result(output)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
