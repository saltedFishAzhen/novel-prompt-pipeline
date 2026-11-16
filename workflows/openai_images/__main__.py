"""CLI: python -m openai_images (local-only unless --execute)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .client import DEFAULT_MODEL, ImageRequest, OpenAIImagesClient, local_check
from .errors import ImageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openai-images")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="inspect local configuration without an API request")
    for name in ("generate", "edit"):
        command = commands.add_parser(name)
        prompts = command.add_mutually_exclusive_group(required=True)
        prompts.add_argument("--prompt")
        prompts.add_argument("--prompt-file", type=Path, help="UTF-8 text file")
        command.add_argument(
            "--image",
            type=Path,
            action="append",
            default=[],
            help="reference image; repeat for multiple inputs (edit only)",
        )
        command.add_argument(
            "--model", default=os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_MODEL
        )
        command.add_argument("--size", default="auto")
        command.add_argument("--quality", default="auto")
        command.add_argument("--output-format", choices=["png", "jpeg", "webp"], default="png")
        command.add_argument("--n", type=int, default=1)
        command.add_argument("--request-timeout", type=float, default=300.0)
        command.add_argument("--output-dir", type=Path, default=Path("runs/openai-images"))
        mode = command.add_mutually_exclusive_group()
        mode.add_argument(
            "--execute", action="store_true", help="authorize one billable API request"
        )
        mode.add_argument("--dry-run", action="store_true", help="local validation only (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            result = local_check()
        else:
            prompt = args.prompt
            if args.prompt_file is not None:
                try:
                    prompt = args.prompt_file.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError):
                    raise ImageError("Cannot read prompt file as UTF-8.") from None
            request = ImageRequest(
                prompt=prompt,
                operation=args.command,
                images=tuple(args.image),
                model=args.model,
                size=args.size,
                quality=args.quality,
                output_format=args.output_format,
                n=args.n,
            )
            result = OpenAIImagesClient(request_timeout=args.request_timeout).run(
                request, args.output_dir, execute=args.execute
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ImageError as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
