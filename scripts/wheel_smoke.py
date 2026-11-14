"""Install the wheel and exercise packaged production resources outside the checkout.

No image or video network request is made. Node.js is required for the
six bundled upstream validators.
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

STAGES = ("outline", "characters", "art", "script", "staging", "storyboard")
REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "src" / "pipeline" / "shuohao" / "vendor"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheel = max(
        args.wheel_dir.resolve().glob("novel_prompt_pipeline-*.whl"),
        key=lambda path: path.stat().st_mtime,
    )
    vendor_files = {
        path.relative_to(VENDOR).as_posix(): path.read_bytes()
        for path in VENDOR.rglob("*")
        if path.is_file()
    }
    assert {"LICENSE", "NOTICE", "UPSTREAM_COMMIT"} <= vendor_files.keys()
    for stage in STAGES:
        assert f"novel-{stage}/SKILL.md" in vendor_files
        assert f"novel-{stage}/scripts/novel-{stage}.mjs" in vendor_files
        assert f"novel-{stage}/references/schema.md" in vendor_files
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for name, content in vendor_files.items():
            assert archive.read(f"pipeline/shuohao/vendor/{name}") == content
        assert "pipeline/shuohao/engine.py" in names
        assert not any(name.endswith("/.env") or "pipeline.local.json" in name for name in names)
        assert not any(
            name.startswith(("prompt_project/", "novel_prompt_pipeline/_resources/prompts/"))
            for name in names
        )
    sdists = list(args.wheel_dir.resolve().glob("novel_prompt_pipeline-*.tar.gz"))
    if sdists:
        with tarfile.open(max(sdists, key=lambda path: path.stat().st_mtime)) as source:
            members = {name.partition("/")[2]: name for name in source.getnames()}
            for name, content in vendor_files.items():
                member = source.extractfile(members[f"src/pipeline/shuohao/vendor/{name}"])
                assert member is not None and member.read() == content
            assert not any(name.startswith("prompts/") for name in members)
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
        workspace = base / "workspace"
        subprocess.run(
            [str(python), "-m", "pipeline", "init", str(workspace)],
            cwd=base,
            env=variables,
            check=True,
        )
        assert not (workspace / "prompts").exists()
        assert not (workspace / "runs").exists()
        assert (workspace / "workflows" / "workflow_bindings.json").is_file()
        assert (workspace / "configs" / "pipeline.local.json").is_file()
        (workspace / "source.txt").write_text("A short story.", encoding="utf-8")
        for stage in ("outline", "characters", "art", "script", "staging", "prompts"):
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pipeline",
                    "--workspace",
                    str(workspace),
                    "run",
                    "--input",
                    "source.txt",
                    "--output",
                    "runs/dry",
                    "--dry-run",
                    "--stop-after",
                    stage,
                ],
                cwd=base,
                env=variables,
                check=True,
            )
        assert not (workspace / "runs").exists()
        code = """
import pathlib, sys
import pipeline
from pipeline.shuohao import upstream
assert pathlib.Path(pipeline.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
for stage in ("outline", "characters", "art", "script", "staging", "storyboard"):
    assert f"novel-{stage}.mjs" in upstream.tool(stage, ["--help"], cwd=pathlib.Path.cwd())
    assert "references/schema.md" in upstream.instructions(stage)
pathlib.Path("bad.json").write_text("{", encoding="utf-8")
try:
    upstream.validate("outline", pathlib.Path("bad.json"), {}, cwd=pathlib.Path.cwd())
except upstream.UpstreamValidationError as exc:
    assert exc.stderr
else:
    raise AssertionError("Invalid JSON passed validation")
"""
        subprocess.run([str(python), "-c", code], cwd=base, env=variables, check=True)
    print("Wheel installation, six upstream tools and external workspace smoke checks passed")


if __name__ == "__main__":
    main()
