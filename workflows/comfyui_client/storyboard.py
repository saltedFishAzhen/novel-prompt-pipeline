"""Read a novel-storyboard H3 production package."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .bindings import build_graph

#: Aspect selectors exposed by the H3 workflow's ``ResolutionSelector``.
_ASPECT_RATIOS = (
    ("1:1", 1, 1),
    ("2:3", 2, 3),
    ("3:2", 3, 2),
    ("3:4", 3, 4),
    ("4:3", 4, 3),
    ("9:16", 9, 16),
    ("16:9", 16, 9),
    ("21:9", 21, 9),
)
_PICTURE_PATTERN = re.compile(r"<Picture\s+(\d+)")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


@dataclass(frozen=True)
class StoryboardSegment:
    name: str
    seconds: float
    prompt: str
    pictures: tuple[Path, ...]
    aspect_ratio: str


def load_storyboard_bundle(manifest_path: str | Path) -> list[StoryboardSegment]:
    """Load every segment of an exported H3 package, validating all files.

    Exporter paths are relative either to the output directory itself or to its
    parent (when ``--out`` names a package directory); a parent-relative path
    carries the package directory name as its first component.
    """
    manifest = Path(manifest_path).expanduser().resolve(strict=True)
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read storyboard manifest {manifest}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("storyboard manifest must be a non-empty array")

    root = manifest.parent.resolve()
    segments: list[StoryboardSegment] = []
    names: set[str] = set()
    for index, item in enumerate(raw):
        field = f"manifest[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        name = item.get("segment")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"{field}.segment must be a safe segment name")
        if name in names:
            raise ValueError(f"duplicate storyboard segment: {name}")
        names.add(name)
        seconds = item.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValueError(f"{field}.seconds must be a positive number")

        prompt = _read_prompt(_package_file(item.get("prompt"), root, f"{name}.prompt"))
        pictures_raw = item.get("pictures")
        if not isinstance(pictures_raw, list) or not pictures_raw:
            raise ValueError(f"{field}.pictures must be a non-empty array")
        picture_numbers = sorted({int(number) for number in _PICTURE_PATTERN.findall(prompt)})
        if picture_numbers != list(range(1, len(pictures_raw) + 1)):
            raise ValueError(
                f"{name} prompt picture references {picture_numbers} do not match "
                f"its {len(pictures_raw)} pictures"
            )
        pictures = tuple(
            _package_file(path, root, f"{name}.pictures[{picture_index}]")
            for picture_index, path in enumerate(pictures_raw)
        )
        if any(path.suffix.lower() not in _IMAGE_SUFFIXES for path in pictures):
            raise ValueError(f"{name} contains a non-image picture path")
        sizes = [_image_size(path) for path in pictures]
        for picture_index, (image_width, image_height) in enumerate(sizes[1:], start=2):
            if abs(image_width / image_height - sizes[0][0] / sizes[0][1]) > 0.03:
                raise ValueError(
                    f"{name} picture {picture_index} aspect ratio does not match picture 1"
                )

        aspect_ratio = _aspect_ratio(*sizes[0])
        # Bind dummy remote names now, before any upload, so the graph and the
        # segment are both known to be usable.
        build_graph(
            {
                "prompt": prompt,
                "length": seconds,
                "aspect_ratio": aspect_ratio,
                "reference_images": [
                    f"preflight/{name}/f{index}.png" for index in range(1, len(pictures) + 1)
                ],
            }
        )
        segments.append(
            StoryboardSegment(name, float(seconds), prompt, pictures, aspect_ratio)
        )
    return segments


def _aspect_ratio(width: int, height: int) -> str:
    """Choose the H3 aspect selector that matches Picture 1."""
    source_ratio = width / height
    error, selected = min(
        (
            abs(source_ratio - horizontal / vertical) / (horizontal / vertical),
            ratio,
        )
        for ratio, horizontal, vertical in _ASPECT_RATIOS
    )
    if error > 0.03:
        raise ValueError(
            f"reference aspect ratio {width}:{height} does not match a supported H3 ratio"
        )
    return selected


def _image_size(path: Path) -> tuple[int, int]:
    """Read raster dimensions from PNG, JPEG or WebP without extra dependencies."""
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_size(data)
    else:
        raise ValueError(f"unsupported or invalid image file: {path}")
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid image dimensions in {path}")
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += length
    raise ValueError("could not read JPEG dimensions")


def _webp_size(data: bytes) -> tuple[int, int]:
    index = 12
    while index + 8 <= len(data):
        kind = data[index : index + 4]
        length = int.from_bytes(data[index + 4 : index + 8], "little")
        chunk = data[index + 8 : index + 8 + length]
        if len(chunk) != length:
            break
        if kind == b"VP8X" and length >= 10:
            return (
                int.from_bytes(chunk[4:7], "little") + 1,
                int.from_bytes(chunk[7:10], "little") + 1,
            )
        if kind == b"VP8 " and length >= 10:
            marker = chunk.find(b"\x9d\x01\x2a")
            if marker >= 0 and marker + 7 <= len(chunk):
                return (
                    int.from_bytes(chunk[marker + 3 : marker + 5], "little") & 0x3FFF,
                    int.from_bytes(chunk[marker + 5 : marker + 7], "little") & 0x3FFF,
                )
        if kind == b"VP8L" and length >= 5 and chunk[0] == 0x2F:
            bits = int.from_bytes(chunk[1:5], "little")
            return (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        index += 8 + length + (length & 1)
    raise ValueError("could not read WebP dimensions")


def _package_file(value: Any, root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError(f"{field} must stay inside the storyboard package: {value!r}")
    parts = posix.parts
    if parts and parts[0] == root.name:
        parts = parts[1:]
    candidate = (root / Path(*parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"{field} must stay inside the storyboard package: {value!r}") from None
    if not candidate.is_file():
        raise ValueError(f"{field} does not exist in the storyboard package: {value}")
    return candidate


def _read_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    # prompt.md begins with a human-facing title and picture list. The H3
    # prompt starts after the first standalone horizontal rule.
    lines = text.splitlines()
    divider = next((index for index, line in enumerate(lines) if line.strip() == "---"), None)
    if divider is None:
        raise ValueError(f"prompt file has no H3 prompt separator: {path}")
    prompt = "\n".join(lines[divider + 1 :]).strip()
    if not prompt:
        raise ValueError(f"prompt file contains no H3 prompt: {path}")
    return prompt
