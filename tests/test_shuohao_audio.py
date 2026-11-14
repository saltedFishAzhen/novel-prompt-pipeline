"""Offline source-bound audio bridge checks using the real shuohao validators."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pipeline.postproduction.plan import check_plan, dialogue_source
from pipeline.shuohao.engine import FILES, STAGES

FIXTURES = Path(__file__).parent / "fixtures" / "shuohao"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")


@pytest.fixture
def run(tmp_path):
    source = FIXTURES / "source.txt"
    shutil.copyfile(source, tmp_path / "source.txt")
    stages = {}
    for stage in STAGES:
        name = FILES[stage]
        path = tmp_path / name
        shutil.copyfile(FIXTURES / "mini" / name, path)
        stages[stage] = {
            "status": "ready",
            "artifact": name,
            "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    state = {
        "workflow": "shuohao-v1",
        "stages": stages,
        "artifacts": FILES,
        "identity": {
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "resources_sha256": "resources",
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def make_plan(source):
    line = source["segments"][0]["cues"][0]
    return {
        "schema_version": 1,
        "tts_model_digest": "a" * 64,
        "voices": {
            identity: {"voice_version": "v1", "instruction": "speak"}
            for identity in source["identities"]
        },
        "cues": [
            {
                "cue_id": "a",
                "panel_index": 1,
                **line,
                "delivery": "onscreen",
                "start_ms": 0,
                "end_ms": 1000,
            }
        ],
    }


def test_valid_source_and_exact_speaker(run):
    source = dialogue_source(run)
    assert len(source["segments"]) == 2
    segment = source["segments"][0]
    assert segment["duration_seconds"] == 12.5
    assert all(
        segment["text"][c["source_start"] : c["source_end"]] == c["text"] for c in segment["cues"]
    )
    checked = check_plan(run, make_plan(source))
    assert checked["plan"]["prompt_version"] == "resources"
    assert isinstance(json.loads((run / "script.json").read_text(encoding="utf-8")), dict)


@pytest.mark.parametrize("mutation", ["speaker", "text", "window", "action"])
def test_reject_invalid_cues(run, mutation):
    source = dialogue_source(run)
    plan = make_plan(source)
    cue = plan["cues"][0]
    if mutation == "speaker":
        cue["speaker_identity_id"] = next(
            i for i in source["identities"] if i != cue["speaker_identity_id"]
        )
    elif mutation == "text":
        cue["text"] += "wrong"
    elif mutation == "window":
        cue["end_ms"] = 12501
    else:
        cue.update(text=source["segments"][0]["text"].split("\n")[0], source_start=0)
        cue["source_end"] = len(cue["text"])
    with pytest.raises(ValueError):
        check_plan(run, plan)


def test_reject_tampered_artifact(run):
    source = dialogue_source(run)
    with (run / "cast.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="artifact changed"):
        check_plan(run, make_plan(source))
