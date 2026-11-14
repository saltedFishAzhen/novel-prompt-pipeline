"""Offline, auditable separation and timed replacement of generated video audio."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..storage import RunLock, fingerprint, write_json
from .plan import _chapter, load_json, sha256, unlinked, wav_info


def _run(
    args: list[str], *, env: dict[str, str] | None = None, timeout: int = 3600
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not run {Path(args[0]).name}") from exc
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed; inspect local inputs and dependencies")
    return result


def check_environment() -> dict:
    python = os.environ.get("AUDIO_SEPARATOR_PYTHON", sys.executable)
    try:
        version = _run(
            [
                python,
                "-c",
                "from importlib.metadata import version; print(version('audio-separator'))",
            ],
            timeout=30,
        ).stdout.strip()
    except RuntimeError:
        version = None
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "audio_separator_version": version,
        "offline_only": True,
    }


def probe(path: Path) -> dict:
    return json.loads(
        _run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
        ).stdout
    )


def _duration(media: dict, kind: str) -> float:
    streams = [s for s in media.get("streams", []) if s.get("codec_type") == kind]
    if not streams:
        raise ValueError(f"Input is missing a {kind} stream")
    stream = streams[0]
    start = float(stream.get("start_time", 0) or 0)
    duration = float(stream.get("duration") or media.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0 or abs(start) > 0.01:
        raise ValueError("Stream duration or start time cannot be safely aligned")
    return duration


class OfflineSeparator:
    def identity(self) -> dict[str, str | None]:
        return {
            "provider": "audio-separator",
            "version": check_environment()["audio_separator_version"],
        }

    def separate(self, audio: Path, output: Path, model: dict[str, Any]) -> dict:
        files = {model["model_file"].name: model["sha256"]}
        for item in model.get("companions", []):
            path = item["file"]
            if path.parent != model["model_file"].parent or path.name in files:
                raise ValueError("Model companions must be unique files in the model directory")
            files[path.name] = item["sha256"]
        for name, digest in files.items():
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or sha256(model["model_file"].parent / name) != digest
            ):
                raise ValueError("Model file hash mismatch")
        allowed = output / "allowed.json"
        write_json(allowed, files)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(
                s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "BREEZE_", "LLM_", "COMFYUI_")
            )
        }
        env["AUDIO_SEPARATOR_MODEL_DIR"] = str(model["model_file"].parent)
        python = os.environ.get("AUDIO_SEPARATOR_PYTHON", sys.executable)
        _run(
            [
                python,
                "-m",
                "pipeline.postproduction.worker",
                "--model",
                str(model["model_file"]),
                "--allowed",
                str(allowed),
                "--audio",
                str(audio),
                "--output",
                str(output),
            ],
            env=env,
        )
        result = load_json(output / "worker-result.json")
        if (
            result.get("vocals") != "vocal_candidate.wav"
            or result.get("background") != "background_candidate.wav"
        ):
            raise ValueError("Separator returned unexpected stem paths")
        return {
            "vocals": output / result["vocals"],
            "background": output / result["background"],
            "provider": "audio-separator",
            "version": result["version"],
        }


def render_shot(
    run: Path, plan_id: str, panel_index: int, video: Path, model: dict, *, separator=None
) -> dict:
    """Render one shot; failures never delete input or overwrite completed renders."""
    run, video = unlinked(run), unlinked(video)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", plan_id)
        or type(panel_index) is not int
        or panel_index < 0
    ):
        raise ValueError("Invalid plan or panel identifier")
    if not model.get("license") or not isinstance(model["license"], str):
        raise ValueError("The separation model license/source must be recorded")
    model_file = unlinked(model["model_file"])
    if sha256(model_file) != model["sha256"]:
        raise ValueError("Separation model hash mismatch")
    video_hash = sha256(video)
    plan_dir = unlinked(run / "audio" / "plans" / plan_id)
    with RunLock(run):
        plan = load_json(plan_dir / "dialogue.json")
        if fingerprint(plan) != plan_id:
            raise ValueError("Frozen dialogue plan has changed")
        state, panels, script = _chapter(run)
        if (
            fingerprint(panels) != plan["panels_sha256"]
            or fingerprint(script) != plan["script_sha256"]
            or state["identity"]["source_sha256"] != plan["source_sha256"]
        ):
            raise ValueError("Chapter snapshot changed after dialogue preparation")
        report = load_json(plan_dir / "takes.json")
        rows = {r["cue_id"]: r for r in report["cues"]}
        cues = [c for c in plan["cues"] if c["panel_index"] == panel_index]
        if not cues:
            raise ValueError("The shot has no prepared dialogue cues")
        takes = []
        for cue in cues:
            row = rows.get(cue["cue_id"])
            if not row or not re.fullmatch(r"[0-9a-f]{64}", row["take_id"]):
                raise ValueError("Shot dialogue is not prepared")
            voice = plan["voices"][cue["speaker_identity_id"]]
            payload = {
                "text": cue["text"],
                "instruction": voice["instruction"],
                "reference_text": voice["reference_text"],
                "cfg_scale": voice["cfg_scale"],
                "seed": voice["seed"],
            }
            expected_take = fingerprint(
                {
                    "speaker_identity_id": cue["speaker_identity_id"],
                    "voice_version": voice["voice_version"],
                    "reference_audio_sha256": voice["reference_audio_sha256"],
                    "tts_model_digest": plan["tts_model_digest"],
                    "request": payload,
                }
            )
            if row["take_id"] != expected_take:
                raise ValueError("Prepared take does not match the frozen dialogue")
            take_dir = unlinked(run / "audio" / "takes" / row["take_id"])
            take = load_json(take_dir / "take.json")
            info = wav_info(take_dir / "take.wav")
            if (
                take.get("status") != "complete"
                or take.get("audio") != info
                or row["audio_sha256"] != info["sha256"]
                or take.get("speaker_identity_id") != cue["speaker_identity_id"]
                or take.get("model", {}).get("model_digest") != plan["tts_model_digest"]
            ):
                raise ValueError("Saved take does not match its cue or pinned model")
            if info["samples"] * 1000 > (cue["end_ms"] - cue["start_ms"]) * info["sample_rate"]:
                raise ValueError(f"Dialogue exceeds its allotted window: {cue['cue_id']}")
            takes.append((cue, take_dir / "take.wav", info))
        media = probe(video)
        length = _duration(media, "video")
        _duration(media, "audio")
        if any(cue["end_ms"] > math.floor(length * 1000) for cue, _, _ in takes):
            raise ValueError("Actual video is shorter than the dialogue timeline")
        model_identity = {
            "sha256": model["sha256"],
            "license": model["license"],
            "companions": [
                {"sha256": item["sha256"], "name": item["file"].name}
                for item in model.get("companions", [])
            ],
        }
        provider = separator or OfflineSeparator()
        provider_identity = (
            provider.identity()
            if hasattr(provider, "identity")
            else {"provider": type(provider).__name__, "version": "injected"}
        )
        if provider_identity.get("version") is None:
            raise RuntimeError(
                "audio-separator is not installed in the configured Python environment"
            )
        for item in model.get("companions", []):
            if sha256(item["file"]) != item["sha256"]:
                raise ValueError("Model companion hash mismatch")
        render_id = fingerprint(
            {
                "plan_id": plan_id,
                "panel_index": panel_index,
                "video_sha256": video_hash,
                "model": model_identity,
                "separator": provider_identity,
                "takes": [info["sha256"] for _, _, info in takes],
            }
        )
        out = unlinked(run / "audio" / "renders" / render_id)
        manifest = out / "manifest.json"
        if manifest.exists():
            result = load_json(manifest)
            for name, digest in result["files"].items():
                if Path(name).name != name:
                    raise ValueError("Invalid saved render path")
                if sha256(out / name) != digest:
                    raise ValueError("A completed render was modified")
            return result
        out.mkdir(parents=True, exist_ok=True)
        for name in (
            "original.wav",
            "background_candidate.wav",
            "vocal_candidate.wav",
            "allowed.json",
            "worker-result.json",
            "mix.wav",
            "redubbed.mp4",
            "manifest.json",
        ):
            unlinked(out / name)
        original = out / "original.wav"
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(video),
                "-map",
                "0:a:0",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s24le",
                str(original),
            ]
        )
        separated = provider.separate(original, out, {**model, "model_file": model_file})
        if (
            isinstance(provider, OfflineSeparator)
            and separated.get("version") != provider_identity["version"]
        ):
            raise ValueError("Separator version changed during rendering")
        background, vocals = unlinked(separated["background"]), unlinked(separated["vocals"])
        if (
            background.parent != out
            or vocals.parent != out
            or background == vocals
            or not background.is_file()
            or not vocals.is_file()
        ):
            raise ValueError(
                "Separator must produce distinct local background and vocal candidates"
            )
        background_duration = _duration(probe(background), "audio")
        original_duration = _duration(probe(original), "audio")
        if abs(background_duration - original_duration) > 0.05 or original_duration > length + 0.05:
            raise ValueError("Separated stems do not safely match the original timeline")
        samples = round(length * 48000)
        filters = [
            f"[0:a]aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo,apad=whole_len={samples},atrim=end_sample={samples}[bg]"
        ]
        for i, (cue, _, _) in enumerate(takes, 1):
            filters.append(
                f"[{i}:a]aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo,adelay={cue['start_ms']}|{cue['start_ms']}[d{i}]"
            )
        inputs = "[bg]" + "".join(f"[d{i}]" for i in range(1, len(takes) + 1))
        filters.append(
            f"{inputs}amix=inputs={len(takes) + 1}:duration=longest:normalize=0:dropout_transition=0,atrim=end_sample={samples}[mix]"
        )
        master = out / "mix.wav"
        command = ["ffmpeg", "-v", "error", "-y", "-i", str(background)]
        for _, path, _ in takes:
            command += ["-i", str(path)]
        command += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[mix]",
            "-c:a",
            "pcm_f32le",
            str(master),
        ]
        _run(command)
        stats = _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(master),
                "-af",
                "astats=metadata=0:reset=0",
                "-f",
                "null",
                "-",
            ]
        )
        peaks = re.findall(r"Peak level dB:\s*(-?\d+(?:\.\d+)?|-inf)", stats.stderr)
        peak = max((float(p) for p in peaks), default=float("inf"))
        if peak > 0:
            raise ValueError("Mix peaks exceed 0 dBFS; adjust source levels before encoding")
        output = out / "redubbed.mp4"
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(video),
                "-i",
                str(master),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map_metadata",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        if abs(_duration(probe(output), "video") - length) > 0.05:
            raise ValueError("Output video duration differs from source")
        names = [original.name, background.name, vocals.name, master.name, output.name]
        result = {
            "schema_version": 1,
            "render_id": render_id,
            "plan_id": plan_id,
            "panel_index": panel_index,
            "video_sha256": video_hash,
            "model": model_identity,
            "separator": {"provider": separated["provider"], "version": separated["version"]},
            "files": {name: sha256(out / name) for name in names},
            "background_padding_seconds": max(0.0, length - background_duration),
            "peak_dbfs": peak if math.isfinite(peak) else None,
            "status": "awaiting_human_review",
            "review": [
                "separation quality",
                "background retention",
                "dialogue intelligibility",
                "mix levels",
                "lip synchronization",
            ],
        }
        write_json(manifest, result)
        return result
