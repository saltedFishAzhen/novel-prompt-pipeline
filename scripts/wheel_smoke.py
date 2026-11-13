"""Install the built wheel and exercise it outside the source checkout.

The child environment inherits only already-installed dependencies. Project
modules must come from the wheel, verified by their resolved module paths.
No LLM or ComfyUI network request is made.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheel = max(
        args.wheel_dir.resolve().glob("novel_prompt_pipeline-*.whl"),
        key=lambda p: p.stat().st_mtime,
    )
    sdists = list(args.wheel_dir.resolve().glob("novel_prompt_pipeline-*.tar.gz"))
    if sdists:
        with tarfile.open(max(sdists, key=lambda p: p.stat().st_mtime)) as source:
            paths = {name.partition("/")[2] for name in source.getnames()}
            for required in (
                "prompts/README.md",
                "prompts/AGENTS.md",
                "prompts/docs/director-round24.md",
                "prompts/docs/drama-round25.md",
                "prompts/docs/unified-node-review.md",
                "prompts/evaluations/rounds/round24/cases.jsonl",
                "prompts/evaluations/rounds/round25/cases.jsonl",
                "prompts/evaluations/rounds/round26/final/run.json",
            ):
                assert required in paths, (
                    f"Source distribution is missing linked evidence: {required}"
                )
            assert not any(name.startswith("prompts/src/styles/") for name in paths)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "novel_prompt_pipeline/_resources/prompts/current.json" in names
        assert "prompt_project/evaluate.py" in names
        assert "novel_prompt_pipeline/_resources/prompts/evaluations/suites/default.jsonl" in names
        assert "pipeline/database/migrations/versions/0002_file_selections.py" in names
        assert not any(name.endswith("/.env") or "pipeline.local.json" in name for name in names)
        assert not any("/prompts/src/styles/" in name for name in names)
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        env = base / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(env)
        python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        variables = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        variables.pop("PYTHONPATH", None)
        variables.pop("NOVEL_PIPELINE_HOME", None)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)],
            cwd=base,
            env=variables,
            check=True,
        )
        code = (
            "import sys, pathlib, pipeline, llm, comfyui_client, prompt_project; "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()) "
            "for m in (pipeline,llm,comfyui_client,prompt_project)); print('Wheel modules verified')"
        )
        subprocess.run([str(python), "-c", code], cwd=base, env=variables, check=True)
        workspace = base / "workspace"
        commands = [
            ["init", str(workspace)],
            ["--workspace", str(workspace), "prompts", "check"],
            ["--workspace", str(workspace), "prompts", "publish", "--offline"],
            ["--workspace", str(workspace), "comfy", "check", "--profile", "qwen"],
            ["--workspace", str(workspace), "comfy", "check", "--profile", "ref2va"],
            ["--workspace", str(workspace), "comfy", "check", "--profile", "i2va"],
            ["--workspace", str(workspace), "db", "info"],
            [
                "--workspace",
                str(workspace),
                "run",
                "--input",
                "prompts/tests/fixtures/scene_src.txt",
                "--output",
                "runs/dry",
                "--dry-run",
            ],
        ]
        for command in commands:
            subprocess.run(
                [str(python), "-m", "pipeline", *command], cwd=base, env=variables, check=True
            )
        assert not (workspace / "prompts/src/styles").exists()
        assert not (workspace / "runs" / "dry").exists()
    print("Wheel installation and external workspace smoke checks passed")


if __name__ == "__main__":
    main()
