"""Run the pinned shuohao skill tools in a caller-owned working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor"
_STAGES = frozenset({"outline", "characters", "art", "script", "staging", "storyboard"})
_REFERENCES = {
    "outline": ("schema", "volume-pass", "outline-pass", "episode-pass"),
    "characters": ("schema", "roster-pass", "profile-pass", "sheet"),
    "art": ("schema", "scene-pass", "prop-pass", "sheet"),
    "script": ("schema", "script-pass"),
    "staging": ("schema", "read-pass", "staging-pass"),
    "storyboard": (
        "schema",
        "storyboard-pass",
        "shot-writing",
        "h3-prompt",
        "seedance-prompt",
        "frame",
    ),
}
_LINKS = {
    "outline": set(),
    "characters": {"book"},
    "art": {"cast"},
    "script": {"outline", "art"},
    "staging": {"script", "art"},
    "storyboard": {"script", "outline", "cast", "art", "shots"},
}


class UpstreamError(RuntimeError):
    """An upstream tool could not run or exited unsuccessfully."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class UpstreamValidationError(UpstreamError):
    """The upstream validator returned a nonzero exit status."""


def skill_root(stage: str) -> Path:
    if stage not in _STAGES:
        raise ValueError(f"Unknown shuohao stage: {stage}")
    return _VENDOR / f"novel-{stage}"


def instructions(stage: str) -> str:
    root = skill_root(stage)
    files = [
        root / "SKILL.md",
        *(root / "references" / f"{name}.md" for name in _REFERENCES[stage]),
    ]
    return "\n\n".join(
        f"# {path.relative_to(root).as_posix()}\n\n{path.read_text(encoding='utf-8')}"
        for path in files
    )


def tool(stage: str, arguments: list[str], *, cwd: Path, timeout: float = 60) -> str:
    script = skill_root(stage) / "scripts" / f"novel-{stage}.mjs"
    try:
        result = subprocess.run(
            ["node", str(script), *arguments],
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise UpstreamError(f"Cannot start Node or access working directory: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError(f"shuohao {stage} timed out after {timeout} seconds") from exc
    if result.returncode:
        error = (
            UpstreamValidationError if arguments and arguments[0] == "validate" else UpstreamError
        )
        raise error(
            f"shuohao {stage} exited with status {result.returncode}",
            stdout=result.stdout[-32000:],
            stderr=result.stderr[-32000:],
        )
    return result.stdout


def validate(stage: str, path: Path, links: dict[str, Path], *, cwd: Path) -> str:
    skill_root(stage)
    unknown = links.keys() - _LINKS[stage]
    if unknown:
        raise ValueError(f"Unsupported {stage} validation links: {sorted(unknown)}")
    args = ["validate", str(path)]
    if stage == "characters":
        if "book" not in links:
            raise ValueError("characters validation requires the book text")
        args.append(str(links["book"]))
    else:
        for name, value in links.items():
            args.extend((f"--{name}", str(value)))
    return tool(stage, args, cwd=cwd)
