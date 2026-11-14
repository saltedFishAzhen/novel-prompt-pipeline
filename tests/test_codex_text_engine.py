"""Exercise native-agent handoffs with the real document quality gates."""

import json
import shutil
from pathlib import Path

import pytest

from pipeline.config import Config
from pipeline.shuohao.engine import ProductionPipeline
from pipeline.shuohao.tasks import complete_text_task, list_text_tasks

FIXTURES = Path(__file__).parent / "fixtures" / "shuohao"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")


def submit_fixture(root, task):
    stage = task["request"]["stage"]
    name = "cast" if stage == "characters" else stage
    data = json.loads((FIXTURES / "mini" / f"{name}.json").read_text(encoding="utf-8"))
    eps = task["request"]["episode_range"]
    if eps:
        data["episodes"] = [ep for ep in data["episodes"] if ep["ep"] in eps]
    draft = root / f"draft-{task['task_id']}.json"
    draft.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    complete_text_task(root, task["task_id"], draft, agent_id="fixture-agent")


def test_native_tasks_complete_all_stages_without_sdk(tmp_path):
    config = Config(
        production={"minutes_per_episode": 0.2, "aspect_ratio": "16:9", "episodes_per_batch": 1}
    )
    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    output = tmp_path / "run"
    with ProductionPipeline(config) as pipeline:
        stages = []
        for _ in range(8):
            result = pipeline.run(source, output, resume=output.joinpath("state.json").exists())
            if result["status"] == "prompts_ready":
                break
            assert result["status"] == "awaiting_text"
            tasks = [task for task in list_text_tasks(output) if task["status"] == "pending"]
            assert {task["task_id"] for task in tasks} == set(result["pending_text_tasks"])
            stages.append(sorted(task["request"]["stage"] for task in tasks))
            for task in tasks:
                submit_fixture(output, task)
        assert result["status"] == "prompts_ready"
        assert stages == [
            ["outline"],
            ["art", "characters"],
            ["script", "script"],
            ["staging"],
            ["storyboard", "storyboard"],
        ]
        assert result["pending_text_tasks"] == []
        assert len(list_text_tasks(output)) == 8
        assert (output / "dialogue-source.json").is_file()
        assert pipeline.run(source, output, resume=True)["status"] == "prompts_ready"


def test_native_repairs_remain_bounded_across_resumes(tmp_path, monkeypatch):
    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    config = Config(production={"max_repairs": 2})
    output = tmp_path / "run"
    draft = tmp_path / "invalid.json"
    draft.write_text("{}", encoding="utf-8")
    with ProductionPipeline(config) as pipeline:

        def reject(stage, path):
            raise ValueError("Identical validation feedback")

        monkeypatch.setattr(pipeline, "_validate", reject)
        result = pipeline.run(source, output, stop_after="outline")
        ids = set()
        for attempt in range(3):
            assert result["status"] == "awaiting_text"
            assert result["pending_text_tasks"][0] not in ids
            ids.add(result["pending_text_tasks"][0])
            complete_text_task(output, result["pending_text_tasks"][0], draft, agent_id="test")
            if attempt < 2:
                result = pipeline.run(source, output, resume=True, stop_after="outline")
        for _ in range(2):
            with pytest.raises(ValueError, match="repair budget exhausted"):
                pipeline.run(source, output, resume=True, stop_after="outline")
        assert len(list_text_tasks(output)) == 3


def test_production_requires_only_native_text_tasks():
    config = Config()
    with ProductionPipeline(config) as pipeline:
        assert "llm" not in pipeline._identity("source")["config"]
        assert not hasattr(config, "llm")
        assert pipeline.dry_run("source")["text_backend"] == "codex"


def test_cross_stage_art_repair_keeps_its_task_across_resume(tmp_path, monkeypatch):
    source = (FIXTURES / "source.txt").read_text(encoding="utf-8")
    config = Config(production={"minutes_per_episode": 0.2, "aspect_ratio": "16:9"})
    output = tmp_path / "run"
    with ProductionPipeline(config) as pipeline:
        original = pipeline._validate

        def validate(stage, path):
            data = original(stage, path)
            if stage == "art" and "characters" in pipeline._documents:
                if "修正" not in data["scenes"][0]["image"]["promptLocal"]:
                    raise ValueError("Art must match the committed characters")
            return data

        monkeypatch.setattr(pipeline, "_validate", validate)
        pipeline.run(source, output, stop_after="art")
        submit_fixture(output, list_text_tasks(output)[0])
        pipeline.run(source, output, resume=True, stop_after="art")
        for task in list_text_tasks(output):
            if task["status"] == "pending":
                submit_fixture(output, task)
        result = pipeline.run(source, output, resume=True, stop_after="art")
        assert result["status"] == "awaiting_text"
        assert len(result["pending_text_tasks"]) == 1
        task_id = result["pending_text_tasks"][0]
        fixed = json.loads((FIXTURES / "mini" / "art.json").read_text(encoding="utf-8"))
        fixed["scenes"][0]["image"]["promptLocal"] += "修正"
        draft = output / "fixed-art.json"
        draft.write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
        complete_text_task(output, task_id, draft, agent_id="repair-agent")
        assert pipeline.run(source, output, resume=True, stop_after="art")["status"] == "ready"
        assert len(list_text_tasks(output)) == 4
        assert pipeline.run(source, output, resume=True, stop_after="art")["status"] == "ready"
