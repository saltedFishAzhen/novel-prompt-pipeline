"""Validate a director Markdown table and export a request for h3_client.

Exit codes: 0 ready, 2 the table is invalid and needs a revision, 1 the request,
an image or the output directory cannot be used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from h3_client import ReferenceImage

from .client import ShotClient, ShotError


def _read_image(value: Any, root: Path) -> ReferenceImage:
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict) or set(value) - {"path", "name", "remote_name"}:
        raise ShotError("each image must be a path or an object with path/name/remote_name")
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ShotError("image path must be a non-empty string")
    local = Path(path).expanduser()
    return ReferenceImage(
        local if local.is_absolute() else root / local,
        name=value.get("name"),
        remote_name=value.get("remote_name"),
    )


def _write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise ShotError(f"temporary output is a symbolic link: {temporary}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _clear_outputs(
    root: Path,
    destinations: dict[str, Path],
    protected: set[Path],
    *,
    malformed_text: str | None = None,
) -> None:
    """Invalidate stale output while retaining known or possibly named inputs."""
    blocked: list[Path] = []
    safe: list[Path] = []
    for destination in destinations.values():
        temporary = destination.with_name(destination.name + ".tmp")
        maybe_named = (
            malformed_text is not None and destination.name.casefold() in malformed_text.casefold()
        )
        if (
            destination.is_symlink()
            or temporary.is_symlink()
            or destination.resolve() in protected
            or temporary.resolve() in protected
            or maybe_named
            or destination.exists()
            and not destination.is_file()
        ):
            blocked.append(destination)
        else:
            safe.append(destination)
    root.mkdir(parents=True, exist_ok=True)
    for destination in safe:
        destination.unlink(missing_ok=True)
    if blocked:
        raise ShotError(
            "output collides with an input, symbolic link, or non-file: "
            + ", ".join(map(str, blocked))
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, prog="python -m shot_client")
    parser.add_argument("--input", required=True, help="processing request JSON")
    parser.add_argument(
        "--output-dir", required=True, help="directory for validated table, report and H3 request"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.input).expanduser().resolve()
    root = Path(args.output_dir).expanduser().resolve()
    destinations = {
        name: root / name
        for name in ("shot.table.json", "shot.report.json", "storyboard.txt", "h3.request.json")
    }
    protected = {source}
    prepared = False
    try:
        try:
            raw_text = source.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            _clear_outputs(root, destinations, protected)
            prepared = True
            raise
        try:
            request = json.loads(raw_text)
        except json.JSONDecodeError:
            _clear_outputs(root, destinations, protected, malformed_text=raw_text)
            prepared = True
            raise
        if not isinstance(request, dict):
            _clear_outputs(root, destinations, protected, malformed_text=raw_text)
            prepared = True
            raise ShotError("input must contain a JSON object")
        unknown = set(request) - {
            "type",
            "source_text",
            "duration_seconds",
            "reference_images",
            "reference_roles",
            "constraints",
            "preferences",
            "values",
            "storyboard_table",
            "table_file",
        }
        table_file = request.get("table_file")
        invalid_table_file = False
        if table_file is not None:
            if not isinstance(table_file, str) or not table_file.strip():
                invalid_table_file = True
                table_path = None
            else:
                table_path = Path(table_file).expanduser()
                table_path = (
                    table_path if table_path.is_absolute() else source.parent / table_path
                ).resolve()
                protected.add(table_path)
        else:
            table_path = None
        raw_images = request.get("reference_images")
        if isinstance(raw_images, list):
            for item in raw_images:
                value = (
                    item
                    if isinstance(item, str)
                    else item.get("path")
                    if isinstance(item, dict)
                    else None
                )
                if isinstance(value, str) and value.strip():
                    local = Path(value).expanduser()
                    protected.add(
                        (local if local.is_absolute() else source.parent / local).resolve()
                    )
        _clear_outputs(root, destinations, protected)
        prepared = True
        if unknown:
            raise ShotError("unknown request fields: " + ", ".join(sorted(unknown)))
        if invalid_table_file:
            raise ShotError("table_file must be a non-empty path")
        if ("storyboard_table" in request) == (table_path is not None):
            raise ShotError("provide exactly one of storyboard_table or table_file")
        table = (
            table_path.read_text(encoding="utf-8-sig")
            if table_path
            else request["storyboard_table"]
        )
        if not isinstance(raw_images, list):
            raise ShotError("reference_images must be an ordered array")
        images = [_read_image(value, source.parent) for value in raw_images]
        result = ShotClient().process(
            table,
            type=request.get("type"),
            source_text=request.get("source_text"),
            reference_images=images,
            duration_seconds=request.get("duration_seconds"),
            reference_roles=request.get("reference_roles"),
            constraints=request.get("constraints"),
            preferences=request.get("preferences"),
        )
        if result.status != "ready":
            _write(
                destinations["shot.report.json"],
                json.dumps(result.report, ensure_ascii=False, indent=2) + "\n",
            )
            print(json.dumps(result.report, ensure_ascii=False), file=sys.stderr)
            # 2 mirrors director-plan: only the table needs a revision. A broken
            # image, role or preference is an input problem and stays 1.
            table_issue = bool(result.issues) and all(
                issue.get("path") == "table" for issue in result.issues
            )
            return 2 if table_issue else 1
        h3_request = result.to_h3_request(values=request.get("values"))
        _write(
            destinations["shot.table.json"],
            json.dumps(result.table.model_dump(), ensure_ascii=False, indent=2) + "\n",
        )
        _write(destinations["storyboard.txt"], h3_request["storyboard"] + "\n")
        _write(
            destinations["shot.report.json"],
            json.dumps(result.report, ensure_ascii=False, indent=2) + "\n",
        )
        _write(
            destinations["h3.request.json"],
            json.dumps(h3_request, ensure_ascii=False, indent=2) + "\n",
        )
        print(
            json.dumps(
                {"status": "ready", "h3_request": str(destinations["h3.request.json"])},
                ensure_ascii=False,
            )
        )
        return 0
    except (ShotError, ValueError, TypeError, OSError) as exc:
        if prepared:
            # Do not leave a partial newly written artifact usable after failure.
            for name in ("shot.table.json", "storyboard.txt", "h3.request.json"):
                destinations[name].unlink(missing_ok=True)
            try:
                report = {"status": "invalid", "issues": [{"path": "input", "message": str(exc)}]}
                _write(
                    destinations["shot.report.json"],
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                )
            except (OSError, ShotError):
                pass
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
