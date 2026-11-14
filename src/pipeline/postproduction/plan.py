"""Validate source-bound cues and persist separately versioned Breeze takes."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..storage import RunLock, fingerprint, write_json


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Voice(Strict):
    voice_version: str = Field(min_length=1, max_length=128)
    instruction: str = ""
    reference_audio: str | None = None
    reference_text: str = ""
    cfg_scale: float = 1.0
    seed: int = Field(default=42, ge=0, lt=2**32)

    @model_validator(mode="after")
    def validate_reference(self):
        if bool(self.reference_audio) != bool(self.reference_text.strip()):
            raise ValueError("Reference audio and its full transcript must be supplied together")
        if not self.instruction.strip() and not self.reference_audio:
            raise ValueError("A voice instruction or a reference recording is required")
        if not math.isfinite(self.cfg_scale) or self.cfg_scale <= 0:
            raise ValueError("cfg_scale must be positive and finite")
        if not self.voice_version.strip():
            raise ValueError("voice_version must not be blank")
        return self


class Cue(Strict):
    cue_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    panel_index: int = Field(ge=0)
    speaker_identity_id: str = Field(min_length=1, max_length=128)
    delivery: Literal["onscreen", "offscreen", "narration"]
    text: str = Field(min_length=1, max_length=2000)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @field_validator("text", "speaker_identity_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Cue text and speaker must be nonblank")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.source_end <= self.source_start or self.end_ms <= self.start_ms:
            raise ValueError("Cue source and timing intervals must be nonempty")
        return self


class DialoguePlan(Strict):
    schema_version: Literal[1]
    tts_model_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    voices: dict[str, Voice]
    cues: list[Cue] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_cues(self):
        if len({cue.cue_id for cue in self.cues}) != len(self.cues):
            raise ValueError("cue_id values must be unique")
        return self


def unlinked(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Audio paths cannot contain symbolic links or junctions")
    return path


def sha256(path: Path) -> str:
    unlinked(path)
    if not path.is_file():
        raise ValueError("Required audio input is not an existing regular file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_json(path: Path) -> Any:
    unlinked(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Audio JSON input exceeds size limit")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate JSON key")
                value[key] = item
            return value

        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Audio JSON input is invalid") from exc


def wav_info(path: Path) -> dict:
    digest = sha256(path)
    try:
        with wave.open(str(path), "rb") as wav:
            rate, count, channels, width = (
                wav.getframerate(),
                wav.getnframes(),
                wav.getnchannels(),
                wav.getsampwidth(),
            )
            if (
                rate <= 0
                or count <= 0
                or channels <= 0
                or width <= 0
                or wav.getcomptype() != "NONE"
            ):
                raise ValueError("WAV must contain nonempty PCM samples")
            expected = count * channels * width
            if expected > 1024**3:
                raise ValueError("WAV exceeds size limit")
            remaining = expected
            while remaining:
                chunk = wav.readframes(min(count, 65536))
                if not chunk:
                    raise ValueError("WAV sample data is truncated")
                remaining -= len(chunk)
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("Audio output is not a readable PCM WAV") from exc
    return {
        "sha256": digest,
        "sample_rate": rate,
        "samples": count,
        "channels": channels,
        "sample_width": width,
    }


def _shuohao_chapter(run: Path, state: dict):
    from ..shuohao import upstream
    from ..shuohao.engine import FILES, STAGES
    from ..shuohao.media import _segments

    identity = state.get("identity", {})
    if sha256(run / "source.txt") != identity.get("source_sha256"):
        raise ValueError("Production source snapshot changed")
    if not isinstance(identity.get("resources_sha256"), str) or not identity["resources_sha256"]:
        raise ValueError("Production resource identity is missing")
    links = {
        "outline": {},
        "characters": {"book": run / "source.txt"},
        "art": {"cast": run / FILES["characters"]},
        "script": {"outline": run / FILES["outline"], "art": run / FILES["art"]},
        "staging": {"script": run / FILES["script"], "art": run / FILES["art"]},
        "storyboard": {
            "script": run / FILES["script"],
            "outline": run / FILES["outline"],
            "cast": run / FILES["characters"],
            "art": run / FILES["art"],
        },
    }
    docs = {}
    for stage in STAGES:
        record = state.get("stages", {}).get(stage, {})
        path = run / FILES[stage]
        if (
            record.get("status") != "ready"
            or record.get("artifact") != FILES[stage]
            or state.get("artifacts", {}).get(stage) != FILES[stage]
            or sha256(path) != record.get("output_sha256")
        ):
            raise ValueError(f"Production artifact changed or incomplete: {FILES[stage]}")
        docs[stage] = load_json(path)
        if not isinstance(docs[stage], dict):
            raise ValueError("Production stage must be a JSON object")
    for stage in STAGES:
        upstream.validate(stage, run / FILES[stage], links[stage], cwd=run)

    identities = {row["id"]: row.copy() for row in docs["outline"]["characters"]}
    derived = dict(
        state, character_identities=identities, prompt_version=identity["resources_sha256"]
    )
    panels, script = [], []
    for index, segment, scene in _segments(docs["storyboard"], docs["script"]):
        beat_indices = list(
            dict.fromkeys(
                beat
                for cut in segment["cuts"]
                for beat in range(cut["beats"][0], cut["beats"][1] + 1)
            )
        )
        if any(i < 1 or i > len(scene["flow"]) for i in beat_indices):
            raise ValueError("Storyboard beat is outside its script scene")
        parts, cues, offset = [], [], 0
        for i in beat_indices:
            beat = scene["flow"][i - 1]
            value = beat.get("line", beat.get("action"))
            if not isinstance(value, str) or not value:
                raise ValueError("Script beat has no source text")
            if parts:
                offset += 1
            start = offset
            parts.append(value)
            offset += len(value)
            if "line" in beat:
                speaker = "narrator" if beat["speaker"] == "VO" else beat["speaker"]
                if speaker != "narrator" and speaker not in identities:
                    raise ValueError("Script speaker is absent from outline")
                cues.append(
                    {
                        "speaker_identity_id": speaker,
                        "text": value,
                        "source_start": start,
                        "source_end": offset,
                    }
                )
        duration = sum(cut["seconds"] for cut in segment["cuts"])
        panels.append(
            {"panel_index": index, "duration_seconds": duration, "segment_id": segment["id"]}
        )
        script.append({"panel_index": index, "text": "\n".join(parts), "cues": cues})
    return derived, panels, script


def dialogue_source(run: Path) -> dict:
    """Return validated, source-bound dialogue for manual timing and voice selection."""
    run = unlinked(run)
    state, panels, script = _chapter(run)
    if state.get("workflow") != "shuohao-v1":
        raise ValueError("Dialogue source export requires a shuohao run")
    return {
        "schema_version": 1,
        "identities": state["character_identities"],
        "segments": [
            dict(panel, text=row["text"], cues=row["cues"]) for panel, row in zip(panels, script)
        ],
    }


def _chapter(run: Path):
    state = load_json(run / "state.json")
    if state.get("workflow") == "shuohao-v1":
        return _shuohao_chapter(run, state)
    panels = state.get("panels")
    summary = state.get("chapter_prompt_review", {})
    if (
        not isinstance(panels, list)
        or not panels
        or summary.get("status") != "approved"
        or summary.get("panels_sha256") != fingerprint(panels)
    ):
        raise ValueError("Audio requires a completed, approved chapter prompt snapshot")
    review = load_json(run / "chapter_prompt_review.json")
    node = state.get("nodes", {}).get(review.get("node_key"), {})
    if (
        review.get("summary") != summary
        or review.get("review", {}).get("verdict") != "approve"
        or node.get("node_id") != "content_review"
        or node.get("value") != review.get("review")
    ):
        raise ValueError("Chapter review record is invalid")
    script = load_json(run / "script.json")
    if not isinstance(script, list) or len({row.get("panel_index") for row in script}) != len(
        script
    ):
        raise ValueError("Numbered chapter script is invalid")
    pinned = state.get("identity", {}).get("approved_script")
    if pinned is not None:
        approved = load_json(run / "approved_script.json")
        if approved != script or pinned != {
            "mode": "approved-v1",
            "sha256": fingerprint(approved),
            "count": len(approved),
        }:
            raise ValueError("Approved script does not match its pinned snapshot")
    else:
        script_node = state.get("nodes", {}).get("script")
        if not isinstance(script_node, dict) or script_node.get("value") != script:
            raise ValueError("Script does not match its recorded inference")
    return state, panels, script


def check_plan(run: Path, data: dict, *, reference_paths: dict[str, Path] | None = None) -> dict:
    """Validate cues against exact script slices, approved shots and speaker identities."""
    run = unlinked(run)
    plan = DialoguePlan.model_validate(data)
    state, panels, script = _chapter(run)
    panel_map = {row["panel_index"]: row for row in panels}
    script_map = {row["panel_index"]: row["text"] for row in script}
    source_rows = {row["panel_index"]: row for row in script}
    identities = state.get("character_identities", {})
    voice_data = {}
    for identity, voice in plan.voices.items():
        if identity not in identities and identity != "narrator":
            raise ValueError(f"Unknown speaker identity: {identity}")
        info = voice.model_dump()
        path = (reference_paths or {}).get(identity)
        if bool(path) != bool(voice.reference_audio):
            raise ValueError("Each reference recording must have a resolved local path")
        info["reference_audio_sha256"] = sha256(path) if path else None
        # Public metadata does not expose the original absolute reference path.
        info.pop("reference_audio")
        voice_data[identity] = info
    for cue in plan.cues:
        if cue.speaker_identity_id not in voice_data:
            raise ValueError(f"No voice configured for cue: {cue.cue_id}")
        if cue.speaker_identity_id == "narrator" and cue.delivery != "narration":
            raise ValueError("Narrator cues require narration delivery")
        panel = panel_map.get(cue.panel_index)
        text = script_map.get(cue.panel_index)
        if (
            panel is None
            or not isinstance(text, str)
            or text[cue.source_start : cue.source_end] != cue.text
        ):
            raise ValueError(f"Cue is not an exact numbered-script slice: {cue.cue_id}")
        if state.get("workflow") == "shuohao-v1" and not any(
            line["speaker_identity_id"] == cue.speaker_identity_id
            and line["source_start"] <= cue.source_start < cue.source_end <= line["source_end"]
            for line in source_rows[cue.panel_index]["cues"]
        ):
            raise ValueError(f"Cue does not match a spoken line and speaker: {cue.cue_id}")
        seconds = panel.get("duration_seconds")
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds <= 0
            or cue.end_ms > seconds * 1000
        ):
            raise ValueError(f"Cue window exceeds its approved shot: {cue.cue_id}")
    normalized = {
        "schema_version": 1,
        "tts_model_digest": plan.tts_model_digest,
        "source_sha256": state["identity"]["source_sha256"],
        "prompt_version": state["prompt_version"],
        "panels_sha256": fingerprint(panels),
        "script_sha256": fingerprint(script),
        "voices": voice_data,
        "cues": [cue.model_dump() for cue in plan.cues],
    }
    return {"plan_id": fingerprint(normalized), "plan": normalized}


def prepare_dialogue(
    run: Path,
    data: dict,
    *,
    reference_paths: dict[str, Path],
    synthesize: Callable[[Path, Path], dict],
    retry_uncertain: bool = False,
) -> dict:
    """Synthesize cues once; interrupted remote submissions require explicit retry."""
    run = unlinked(run)
    with RunLock(run):
        checked = check_plan(run, data, reference_paths=reference_paths)
        plan_id, plan = checked["plan_id"], checked["plan"]
        root = unlinked(run / "audio")
        plan_dir = unlinked(root / "plans" / plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plan_dir / "dialogue.json"
        if plan_file.exists() and load_json(plan_file) != plan:
            raise ValueError("Frozen dialogue plan changed")
        if not plan_file.exists():
            write_json(plan_file, plan)
        result = {"schema_version": 1, "plan_id": plan_id, "status": "synthesizing", "cues": []}
        report_file = plan_dir / "takes.json"
        write_json(report_file, result)
        for cue in plan["cues"]:
            speaker = cue["speaker_identity_id"]
            voice = plan["voices"][speaker]
            payload = {
                "text": cue["text"],
                "instruction": voice["instruction"],
                "reference_text": voice["reference_text"],
                "cfg_scale": voice["cfg_scale"],
                "seed": voice["seed"],
            }
            take_key = fingerprint(
                {
                    "speaker_identity_id": speaker,
                    "voice_version": voice["voice_version"],
                    "reference_audio_sha256": voice["reference_audio_sha256"],
                    "tts_model_digest": plan["tts_model_digest"],
                    "request": payload,
                }
            )
            take_dir = unlinked(root / "takes" / take_key)
            take_dir.mkdir(parents=True, exist_ok=True)
            reference = reference_paths.get(speaker)
            if reference is not None:
                copied = take_dir / "reference.wav"
                unlinked(copied)
                if not copied.exists():
                    # Exclusive creation refuses even a dangling link inserted after the check.
                    with reference.open("rb") as incoming, copied.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                if (
                    sha256(copied) != voice["reference_audio_sha256"]
                    or sha256(reference) != voice["reference_audio_sha256"]
                ):
                    raise ValueError("Voice reference changed during preparation")
                payload["reference_audio"] = "reference.wav"
            request_file = take_dir / "request.json"
            if request_file.exists() and load_json(request_file) != payload:
                raise ValueError("TTS request snapshot changed")
            if not request_file.exists():
                write_json(request_file, payload)
            wav = take_dir / "take.wav"
            take_state_file = take_dir / "take.json"
            take = load_json(take_state_file) if take_state_file.exists() else None
            if take and take.get("status") == "complete":
                if (
                    take.get("audio") != wav_info(wav)
                    or take.get("model", {}).get("model_digest") != plan["tts_model_digest"]
                ):
                    raise ValueError("Saved dialogue take changed")
            else:
                if (
                    take
                    and take.get("status") in {"synthesizing", "uncertain"}
                    and not retry_uncertain
                    and not (wav.is_file() and wav.with_suffix(".voice.json").is_file())
                ):
                    raise RuntimeError(
                        f"TTS outcome uncertain for {cue['cue_id']}; inspect before an explicit retry"
                    )
                write_json(
                    take_state_file,
                    {"schema_version": 1, "status": "synthesizing", "take_id": take_key},
                )
                try:
                    response = synthesize(request_file, wav)
                    info = wav_info(wav)
                    if response.get("audio", {}).get("sha256") != info["sha256"]:
                        raise ValueError("TTS result hash differs from the saved WAV")
                    if response.get("model", {}).get("model_digest") != plan["tts_model_digest"]:
                        raise ValueError("Breeze model digest does not match the pinned plan")
                    take = {
                        "schema_version": 1,
                        "status": "complete",
                        "take_id": take_key,
                        "request_sha256": fingerprint(payload),
                        "speaker_identity_id": speaker,
                        "voice_version": voice["voice_version"],
                        "model": response.get("model"),
                        "audio": info,
                    }
                    write_json(take_state_file, take)
                except Exception:
                    write_json(
                        take_state_file,
                        {"schema_version": 1, "status": "uncertain", "take_id": take_key},
                    )
                    raise
            # Compare in samples without rounding away an overrun.
            audio = take["audio"]
            required_ms = (audio["samples"] * 1000 + audio["sample_rate"] - 1) // audio[
                "sample_rate"
            ]
            result["cues"].append(
                {
                    "cue_id": cue["cue_id"],
                    "panel_index": cue["panel_index"],
                    "take_id": take_key,
                    "audio_sha256": audio["sha256"],
                    "duration_ms_ceil": required_ms,
                    "start_ms": cue["start_ms"],
                    "end_ms": cue["end_ms"],
                    "fits": required_ms <= cue["end_ms"] - cue["start_ms"],
                }
            )
            write_json(report_file, result)
        result["status"] = (
            "ready_for_video"
            if all(row["fits"] for row in result["cues"])
            else "needs_timing_adjustment"
        )
        result["review"] = "awaiting_listening_review"
        write_json(report_file, result)
        return {
            "plan_id": plan_id,
            "status": result["status"],
            "cue_count": len(result["cues"]),
            "overflow_cues": [row["cue_id"] for row in result["cues"] if not row["fits"]],
        }
