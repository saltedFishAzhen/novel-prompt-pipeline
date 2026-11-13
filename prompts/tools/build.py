"""Compatibility entry point for the installed trusted compiler."""
from pathlib import Path
from prompt_project.asset_compiler import main

if __name__ == "__main__":
    raise SystemExit(main(root=Path(__file__).resolve().parent.parent))
