"""Generate a director table and a request for the local shot processor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

from .client import DirectorClient, DirectorError, ReferenceImage, _values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="director-plan", description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    source = Path(args.input).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    protected = {source, Path(args.env_file).expanduser().resolve(), Path(".env").resolve()}

    def safe_target(path: Path) -> bool:
        return (
            not path.is_symlink()
            and (not path.exists() or path.is_file())
            and path.resolve() not in protected
        )

    def write(path: Path, content: str) -> None:
        if not safe_target(path):
            raise DirectorError("output would overwrite an input or symbolic link")
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=output)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    try:
        load_dotenv(args.env_file, override=False)
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        # Collect recognizable inputs before rejecting any other request fields.
        if isinstance(raw, dict) and isinstance(raw.get("reference_images"), list):
            for item in raw["reference_images"]:
                path = (
                    item
                    if isinstance(item, str)
                    else item.get("path")
                    if isinstance(item, dict)
                    else None
                )
                if isinstance(path, str) and path.strip():
                    local = Path(path).expanduser()
                    protected.add(
                        (local if local.is_absolute() else source.parent / local).resolve()
                    )
        if not isinstance(raw, dict) or set(raw) - {
            "type",
            "source_text",
            "reference_images",
            "duration_seconds",
            "reference_roles",
            "constraints",
            "preferences",
            "values",
        }:
            raise DirectorError("input contains unsupported director fields")
        if not isinstance(raw.get("reference_images"), list):
            raise DirectorError("reference_images must be an ordered array")
        images = []
        for item in raw["reference_images"]:
            if isinstance(item, str):
                item = {"path": item}
            if (
                not isinstance(item, dict)
                or set(item) - {"path", "name", "remote_name"}
                or not isinstance(item.get("path"), str)
                or not item["path"].strip()
            ):
                raise DirectorError("invalid image object")
            path = Path(item["path"]).expanduser()
            image = ReferenceImage(
                path if path.is_absolute() else source.parent / path,
                name=item.get("name"),
                remote_name=item.get("remote_name"),
            )
            images.append(image)
            protected.add(image.path)
        values = _values(raw.get("values"))
        names = ("director.md", "director.report.json", "shot.request.json")
        if any(not safe_target(output / name) for name in names):
            raise DirectorError(
                "outputs must not overwrite inputs, configuration or symbolic links"
            )
        with DirectorClient(
            model=args.model,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
        ) as director:
            result = director.direct(
                raw.get("type"),
                raw.get("source_text"),
                images,
                raw.get("duration_seconds"),
                reference_roles=raw.get("reference_roles"),
                constraints=raw.get("constraints"),
                preferences=raw.get("preferences"),
            )
        output.mkdir(parents=True, exist_ok=True)
        write(output / "director.md", result.table_text + "\n")
        write(
            output / "director.report.json",
            json.dumps(result.report, ensure_ascii=False, indent=2) + "\n",
        )
        if result.status == "needs_input":
            (output / "shot.request.json").unlink(missing_ok=True)
            print(
                json.dumps(
                    {"status": result.status, "report": str(output / "director.report.json")},
                    ensure_ascii=False,
                )
            )
            return 2
        request = result.to_shot_request(values=values, table_file="director.md")
        write(
            output / "shot.request.json", json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "status": "generated",
                    "seconds": result.report["seconds"],
                    "table_file": str(output / "director.md"),
                    "shot_request": str(output / "shot.request.json"),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (ValueError, TypeError, OSError) as exc:
        for name in ("director.md", "shot.request.json"):
            with suppress(OSError):
                if safe_target(output / name):
                    (output / name).unlink(missing_ok=True)
        with suppress(OSError):
            if safe_target(output / "director.report.json"):
                output.mkdir(parents=True, exist_ok=True)
                write(
                    output / "director.report.json",
                    json.dumps(
                        {"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2
                    )
                    + "\n",
                )
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
