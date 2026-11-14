"""Offline dialogue and media pipeline tests; no TTS or separator models required."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from pipeline.postproduction import check_plan, prepare_dialogue, render_shot
from pipeline.postproduction.plan import sha256
from pipeline.postproduction.worker import pinned_yaml_path
from pipeline.service import AppService, WorkspacePathError
from pipeline.storage import fingerprint, write_json


@pytest.fixture
def job(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    panels = [{"panel_index": 1, "duration_seconds": 2}]
    script = [{"panel_index": 1, "text": "你好。"}]
    review = {"verdict": "approve", "findings": []}
    summary = {"status": "approved", "panels_sha256": fingerprint(panels)}
    state = {
        "panels": panels,
        "chapter_prompt_review": summary,
        "nodes": {
            "review": {"node_id": "content_review", "value": review},
            "script": {"value": script},
        },
        "character_identities": {"hero": {}},
        "identity": {"source_sha256": "a" * 64},
        "prompt_version": "v1",
    }
    write_json(run / "state.json", state)
    write_json(run / "script.json", script)
    write_json(
        run / "chapter_prompt_review.json",
        {"summary": summary, "review": review, "node_key": "review"},
    )
    plan = {
        "schema_version": 1,
        "tts_model_digest": "b" * 64,
        "voices": {"hero": {"voice_version": "v1", "instruction": "neutral"}},
        "cues": [
            {
                "cue_id": "c1",
                "panel_index": 1,
                "speaker_identity_id": "hero",
                "delivery": "onscreen",
                "text": "你好。",
                "source_start": 0,
                "source_end": 3,
                "start_ms": 250,
                "end_ms": 1250,
            }
        ],
    }
    return run, plan


def make_synth(duration_ms=500):
    calls = []

    def synth(request, target):
        calls.append(request)
        with wave.open(str(target), "wb") as out:
            out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            out.writeframes(b"\x00\x00" * (24 * duration_ms))
        return {"audio": {"sha256": sha256(target)}, "model": {"model_digest": "b" * 64}}

    return calls, synth


def test_freezes_source_and_reuses_takes(job):
    run, plan = job
    calls, synth = make_synth()
    first = prepare_dialogue(run, plan, reference_paths={}, synthesize=synth)
    second = prepare_dialogue(run, plan, reference_paths={}, synthesize=synth)
    assert first == second
    assert first["status"] == "ready_for_video"
    assert len(calls) == 1
    plan["cues"][0]["text"] = "再见。"
    with pytest.raises(ValueError, match="exact"):
        check_plan(run, plan)


def test_overflow_is_reported_without_trimming(job):
    run, plan = job
    calls, synth = make_synth(1100)
    result = prepare_dialogue(run, plan, reference_paths={}, synthesize=synth)
    assert result["status"] == "needs_timing_adjustment"
    assert result["overflow_cues"] == ["c1"]
    assert len(calls) == 1


def test_uncertain_submission_requires_explicit_retry(job):
    run, plan = job

    def fail(*_):
        raise RuntimeError("network interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        prepare_dialogue(run, plan, reference_paths={}, synthesize=fail)
    with pytest.raises(RuntimeError, match="uncertain"):
        prepare_dialogue(run, plan, reference_paths={}, synthesize=fail)
    _, synth = make_synth()
    assert (
        prepare_dialogue(run, plan, reference_paths={}, synthesize=synth, retry_uncertain=True)[
            "status"
        ]
        == "ready_for_video"
    )


def test_service_workspace_restriction_and_preparation(job, tmp_path, monkeypatch):
    run, plan = job
    plan_file = tmp_path / "plan.json"
    write_json(plan_file, plan)
    service = AppService(tmp_path, restricted=True)
    calls, synth = make_synth()
    monkeypatch.setattr(
        service,
        "tts_synthesize",
        lambda values, output: synth(tmp_path / values, tmp_path / output),
    )
    checked = service.postproduction_check("run", "plan.json")
    prepared = service.postproduction_prepare("run", "plan.json")
    assert checked["plan_id"] == prepared["plan_id"]
    assert len(calls) == 1
    with pytest.raises(WorkspacePathError):
        service.postproduction_check("../outside", "plan.json")


def test_dangling_reference_link_rejected_before_copy(job, tmp_path, monkeypatch):
    run, plan = job
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference fixture")
    plan["voices"]["hero"].update(reference_audio="reference.wav", reference_text="参考音全文")
    real_is_symlink = Path.is_symlink

    def is_symlink(path):
        if path.name == "reference.wav" and "takes" in path.parts:
            return True
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    calls, synth = make_synth()
    with pytest.raises(ValueError, match="symbolic links"):
        prepare_dialogue(run, plan, reference_paths={"hero": reference}, synthesize=synth)
    assert not calls
    assert list((run / "audio" / "takes").rglob("reference.wav")) == []


def test_yaml_is_loaded_only_from_hashed_model_directory(tmp_path, monkeypatch):
    root, cwd = tmp_path / "models", tmp_path / "cwd"
    root.mkdir()
    cwd.mkdir()
    model_config = root / "config.yaml"
    model_config.write_text("approved: true", encoding="utf-8")
    (cwd / "config.yaml").write_text("approved: false", encoding="utf-8")
    monkeypatch.chdir(cwd)
    allow = {"config.yaml": sha256(model_config)}
    assert pinned_yaml_path(root, "config.yaml", allow) == model_config
    with pytest.raises(ValueError, match="allowed"):
        pinned_yaml_path(root, "../cwd/config.yaml", allow)
    model_config.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        pinned_yaml_path(root, "config.yaml", allow)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is required"
)
def test_redub_synthetic_video_with_injected_separator(job, tmp_path):
    run, plan = job
    _, synth = make_synth()
    prepared = prepare_dialogue(run, plan, reference_paths={}, synthesize=synth)
    video = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fixture")

    class FixtureSeparator:
        def separate(self, audio, output, _model):
            background = output / "background_candidate.wav"
            vocals = output / "vocal_candidate.wav"
            shutil.copyfile(audio, background)
            shutil.copyfile(audio, vocals)
            return {
                "background": background,
                "vocals": vocals,
                "provider": "fixture",
                "version": "test",
            }

    config = {
        "model_file": model,
        "sha256": sha256(model),
        "license": "test fixture",
        "companions": [],
    }
    result = render_shot(run, prepared["plan_id"], 1, video, config, separator=FixtureSeparator())
    assert result["status"] == "awaiting_human_review"
    assert "redubbed.mp4" in result["files"]
    assert (
        render_shot(run, prepared["plan_id"], 1, video, config, separator=FixtureSeparator())
        == result
    )
