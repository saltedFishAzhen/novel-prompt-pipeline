"""Compatibility entry point for trusted asset validators."""
from pathlib import Path
import sys

from prompt_project.validation import asset_validation_report


def main():
    if len(sys.argv) < 3:
        print("Usage: validate_asset_full.py <mode> <output_file> [source_file]")
        return 2
    mode, output = sys.argv[1:3]
    if mode not in {"scene", "character", "prop"}:
        print("mode must be scene, character, or prop")
        return 2
    source = Path(sys.argv[3]).read_text(encoding="utf-8") if len(sys.argv) > 3 else None
    errors, warnings = asset_validation_report(Path(output).read_text(encoding="utf-8"), mode, source)
    for warning in warnings:
        print(warning)
    if errors:
        print("\n".join(errors))
        return 1
    print("Asset validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
