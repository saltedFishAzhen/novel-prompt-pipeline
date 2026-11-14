"""Offline integration checks against the pinned shuohao Node validators."""

import json
import shutil
from pathlib import Path

import pytest

from pipeline.shuohao import upstream

FIXTURES = Path(__file__).parent / "fixtures" / "shuohao"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize(
    ("stage", "artifact", "links"),
    [
        ("outline", "outline", {}),
        ("characters", "cast", {"book": "source.txt"}),
        ("art", "art", {"cast": "cast.json"}),
        ("script", "script", {"outline": "outline.json", "art": "art.json"}),
        ("staging", "staging", {"script": "script.json", "art": "art.json"}),
        (
            "storyboard",
            "storyboard",
            {
                "script": "script.json",
                "outline": "outline.json",
                "cast": "cast.json",
                "art": "art.json",
            },
        ),
    ],
)
def test_pinned_examples_pass_actual_validators(stage, artifact, links, tmp_path):
    upstream.validate(
        stage,
        (FIXTURES / f"{artifact}.json").resolve(),
        {key: (FIXTURES / name).resolve() for key, name in links.items()},
        cwd=tmp_path,
    )


def test_examples_are_not_a_complete_production_storyboard():
    # Upstream's storyboard validator accepts this partial illustrative example.
    # Production must independently verify episode coverage and staging handoff.
    assert {ep["ep"] for ep in load("outline")["episodes"]} == set(range(1, 7))
    assert {ep["ep"] for ep in load("storyboard")["episodes"]} == {1}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_storyboard_example_does_not_copy_staging_opening(tmp_path):
    blocking = upstream.tool(
        "staging", ["blocking", str((FIXTURES / "staging.json").resolve())], cwd=tmp_path
    )
    first_opening = blocking.splitlines()[0].split("\t", 1)[1]
    assert load("storyboard")["episodes"][0]["segments"][0]["blocking"] != first_opening


def production_config(tmp_path):
    from pipeline.config import Config

    return Config(
        asset_library=str(tmp_path / "assets.sqlite"),
        production={
            "minutes_per_episode": 0.2,
            "aspect_ratio": "16:9",
            "episodes_per_batch": 2,
            "max_repairs": 1,
        },
    )


def submit_fixture(run, task, *, defect=None):
    from pipeline.shuohao.tasks import complete_text_task

    stage = task["request"]["stage"]
    name = "cast" if stage == "characters" else stage
    data = json.loads((FIXTURES / "mini" / f"{name}.json").read_text(encoding="utf-8"))
    episodes = task["request"]["episode_range"]
    if episodes:
        data["episodes"] = [episode for episode in data["episodes"] if episode["ep"] in episodes]
    if defect == "missing_episode":
        data["episodes"] = []
    elif defect == "wrong_blocking":
        data["episodes"][0]["segments"][0]["blocking"] = "不一致的走位"
    draft = run / f"fixture-{task['task_id']}.json"
    draft.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    complete_text_task(run, task["task_id"], draft, agent_id="fixture-agent")


def drive(pipeline, source, output, stop_after, *, defect=None):
    from pipeline.shuohao.tasks import list_text_tasks

    injected = False
    repaired = False
    for _ in range(15):
        result = pipeline.run(
            source,
            output,
            resume=(output / "state.json").exists(),
            render=stop_after == "frames",
            stop_after=stop_after,
        )
        if result["status"] != "awaiting_text":
            return result, injected, repaired
        tasks = [task for task in list_text_tasks(output) if task["status"] == "pending"]
        assert tasks
        for task in tasks:
            stage = task["request"]["stage"]
            chosen = None
            if stage == "storyboard" and defect:
                if not injected:
                    chosen = defect
                    injected = True
                else:
                    prompt = Path(task["prompt_file"]).read_text(encoding="utf-8")
                    assert "校验反馈" in prompt
                    assert "validation_error" in prompt
                    repaired = True
            submit_fixture(output, task, defect=chosen)
    pytest.fail("Production did not complete within the expected number of handoffs")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_resume_rejects_changed_source_and_artifact(tmp_path):
    from pipeline.shuohao.engine import ProductionPipeline

    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(production_config(tmp_path)) as pipeline:
        result, _, _ = drive(pipeline, source, output, "outline")
        assert result["status"] == "ready"
        with pytest.raises(ValueError, match="inputs differ"):
            pipeline.run(source + " changed", output, resume=True, stop_after="outline")
        artifact = output / "outline.json"
        artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with pytest.raises(ValueError, match="artifact changed"):
            pipeline.run(source, output, resume=True, stop_after="outline")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_resume_rejects_modified_source_snapshot(tmp_path):
    from pipeline.shuohao.engine import ProductionPipeline

    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(production_config(tmp_path)) as pipeline:
        drive(pipeline, source, output, "outline")
        (output / "source.txt").write_text("changed", encoding="utf-8")
        with pytest.raises(ValueError, match="Source snapshot changed"):
            pipeline.run(source, output, resume=True, stop_after="outline")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_resume_rejects_modified_video_workflow(tmp_path):
    from pipeline.shuohao.engine import ProductionPipeline

    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(production_config(tmp_path)) as pipeline:
        drive(pipeline, source, output, "outline")
        pipeline.i2va_template._graph["122"]["inputs"]["自定义宽"] += 64
        with pytest.raises(ValueError, match="Run inputs differ"):
            pipeline.run(source, output, resume=True, stop_after="outline")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("defect", ["missing_episode", "wrong_blocking"])
def test_storyboard_handoff_defects_receive_feedback_and_repair(tmp_path, defect):
    from pipeline.shuohao.engine import ProductionPipeline

    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(production_config(tmp_path)) as pipeline:
        result, injected, repaired = drive(pipeline, source, output, "prompts", defect=defect)
    assert result["status"] == "prompts_ready"
    assert injected and repaired
    storyboard = json.loads((output / "storyboard.json").read_text(encoding="utf-8"))
    assert [episode["ep"] for episode in storyboard["episodes"]] == [1, 2]
    assert storyboard["episodes"][0]["segments"][0]["blocking"] != "不一致的走位"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_frames_checkpoint_stops_for_codex_assets_without_comfy(tmp_path):
    from pipeline.images.tasks import list_image_tasks
    from pipeline.shuohao.engine import ProductionPipeline

    class NoComfy:
        def __getattr__(self, name):
            raise AssertionError(f"Unexpected ComfyUI access: {name}")

    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(production_config(tmp_path), comfy=NoComfy()) as pipeline:
        result, _, _ = drive(pipeline, source, output, "frames")
    assert result["status"] == "awaiting_image"
    tasks = list_image_tasks(output)
    assert tasks
    assert {task["request"]["kind"] for task in tasks} == {"character", "scene", "prop"}
    assert not list(output.rglob("*.png"))
