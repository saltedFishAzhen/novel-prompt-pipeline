"""Compatibility entry point backed by the unified project CLI."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from pipeline.cli import main as project_main

    return project_main(["prompts", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
