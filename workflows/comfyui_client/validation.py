"""Path and identifier validation shared by the transport and binding layers."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

#: ``?``/``#`` would break the ``/view`` query string and remote loader names.
_FORBIDDEN_CHARACTERS = frozenset("?#")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u0080-\uffff]+")
#: Zero-width and bidirectional overrides can disguise an executable extension.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
MAX_REMOTE_PATH = 512
#: Local file names must stay well below the 255-byte component limit of common
#: filesystems, because the node id is prepended to them.
MAX_LOCAL_FILENAME = 120


def validate_remote_path(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    single_component: bool = False,
) -> str:
    """Return a normalized ComfyUI input-relative name, or raise ``ValueError``.

    One rule set covers both caller-supplied names (profile values) and
    server-supplied names (upload/history responses), so a value accepted in
    one place cannot be rejected in another. Accepted names are relative, free
    of control characters, ``?``, ``#`` and drive letters, and contain no
    empty, ``.`` or ``..`` segment.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{field} must be a safe relative path")
    if len(value) > MAX_REMOTE_PATH:
        raise ValueError(f"{field} must be at most {MAX_REMOTE_PATH} characters")
    if value != value.strip():
        raise ValueError(f"{field} must not have leading or trailing whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must be a safe relative path")
    if any(char in value for char in _FORBIDDEN_CHARACTERS):
        raise ValueError(f"{field} must be a safe relative path")
    if PureWindowsPath(value).drive:
        raise ValueError(f"{field} must be a safe relative path")
    normalized = value.replace("\\", "/")
    if PurePosixPath(normalized).is_absolute():
        raise ValueError(f"{field} must be a relative path")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} must be a safe relative path")
    if single_component and len(parts) != 1:
        raise ValueError(f"{field} must be a single file name")
    return normalized


def validate_prompt_id(value: Any) -> str:
    """Validate a prompt id before it is placed in a URL path."""
    try:
        prompt_id = validate_remote_path(value, "prompt_id", single_component=True)
    except ValueError as exc:
        raise ValueError("prompt_id must be a non-empty path-safe id") from exc
    if "%" in prompt_id:
        raise ValueError("prompt_id must be a non-empty path-safe id")
    return prompt_id


def safe_filename(
    value: Any, *, fallback: str = "output.bin", max_length: int = MAX_LOCAL_FILENAME
) -> str:
    """Reduce an arbitrary server-supplied string to one local file name.

    The result is one path component, free of separators, control characters,
    invisible formatting and a prepended VCS/dot prefix. It is capped at
    ``max_length`` characters (stem first, suffix preserved) so a hostile or
    careless server name cannot make ``open()``/``os.replace()`` fail.
    """
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FILENAME.sub("_", name)
    name = _INVISIBLE.sub("", name).strip("._")
    name = _truncate(name, max_length).strip("._")
    return name or fallback


def _truncate(name: str, max_length: int) -> str:
    """Shorten ``name`` to ``max_length`` characters, keeping its suffix."""
    if max_length <= 0 or len(name) <= max_length:
        return name
    suffix = Path(name).suffix
    if len(suffix) >= max_length:
        return name[:max_length]
    stem = name[: len(name) - len(suffix)] if suffix else name
    return stem[: max(1, max_length - len(suffix))] + suffix


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
