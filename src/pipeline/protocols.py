from __future__ import annotations

import json
import re

from .schemas import Continuity, RewriteResult, ShotPlan, Summary

START = "_::~OUTPUT_START::~_"
END = "_::~OUTPUT_END::~_"
RECORD = "_::~RECORD::~_"
FIELD = "_::~FIELD::~_"
SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
I2VA_SECTIONS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
I2VA_ANCHOR = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)


def json_value(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def single_line(value) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(x in value for x in ("\n", "\r", "\\n"))
    ):
        raise ValueError("Expected a nonempty single-line text")
    if any(x in value for x in (START, END, FIELD, RECORD)):
        raise ValueError("Protocol marker collided with narrative text; preprocess input")
    return value


def unwrap(text: str) -> str:
    text = text.strip()
    if not text.startswith(START + "\n") or not text.endswith("\n" + END):
        raise ValueError("Missing complete output start/end markers")
    result = text[len(START) : -len(END)].strip()
    if START in result or END in result:
        raise ValueError("Duplicate output wrapper")
    return result


def script(text: str, first_index: int = 0) -> list[dict]:
    text = text.replace("\r\n", "\n")
    body = unwrap(text)
    records = re.split(r"\n" + re.escape(RECORD) + r"\n", body)
    return [
        {"panel_index": first_index + i, "text": single_line(record)}
        for i, record in enumerate(records)
    ]


def rewrites(text: str, panels: list[dict]) -> list[dict]:
    result = RewriteResult.model_validate(json_value(text)).model_dump()["results"]
    for item in result:
        single_line(item["text"])
    if [x["panel_index"] for x in result] != [x["panel_index"] for x in panels]:
        raise ValueError("Rewrite panel indices/order do not exactly match input")
    return result


def shot_plan(text: str, panels: list[dict]) -> list[dict]:
    choices = ShotPlan.model_validate(json_value(text)).model_dump()["shots"]
    if [item["panel_index"] for item in choices] != [item["panel_index"] for item in panels]:
        raise ValueError("Shot type decisions must preserve every panel index and input order")
    if any(not item["reason"].strip() for item in choices):
        raise ValueError("Every shot type decision requires a nonempty reason")
    return choices


def _video(
    text: str,
    min_seconds: int,
    max_seconds: int,
    picture_capacity: int,
    audio_capacity: int,
    mode: str = "ref2va",
) -> int:
    if mode == "i2va":
        if not text.startswith(I2VA_ANCHOR + "\n"):
            raise ValueError("I2VA video must begin with the required first-frame anchor")
        sections = re.findall(r"(?m)^([a-z_]+):", text)
        if sections != list(I2VA_SECTIONS):
            raise ValueError("I2VA video must contain the three I2VA sections in order")
    elif mode == "ref2va":
        sections = re.findall(r"(?m)^([a-z_]+):\s*$", text)
        if sections != list(SECTIONS):
            raise ValueError("Video must contain all six Ref2VA sections in order")
    else:
        raise ValueError(f"Unknown video mode: {mode}")
    duration = re.search(r"(?:^|\n)时长：(\d+)秒\s*$", text)
    if not duration or not min_seconds <= int(duration[1]) <= max_seconds:
        raise ValueError(f"Duration must end video_prompt and be in {min_seconds}..{max_seconds}s")
    seconds = int(duration[1])
    cuts = [
        int(m) * 60 + int(s) + int(ms) / 1000
        for m, s, ms in re.findall(r"At (\d{2}):(\d{2})\.(\d{3})", text)
    ]
    if cuts != sorted(set(cuts)) or any(t <= 0 or t >= seconds for t in cuts):
        raise ValueError("Cut timestamps must increase and be inside the duration")
    pictures = {int(x) for x in re.findall(r"<Picture (\d+)>", text)}
    if 1 not in pictures or any(x < 1 or x > picture_capacity for x in pictures):
        raise ValueError(
            "Video picture reference exceeds the actual workflow slots or misses first frame"
        )
    audio_refs = {int(x) for x in re.findall(r"<Audio (\d+)>", text)}
    if any(x < 1 or x > audio_capacity for x in audio_refs):
        raise ValueError("Audio reference is not configured for this run")
    if re.search(r"<Video \d+>", text):
        raise ValueError("No video-reference inputs are configured for this pipeline")
    return seconds


def storyboards(
    text: str,
    panels: list[dict],
    *,
    min_seconds=4,
    max_seconds=15,
    image_capacity=0,
    video_capacity=1,
    audio_capacity=0,
    selected=None,
    mode="ref2va",
) -> list[dict]:
    text = text.replace("\r\n", "\n").strip()
    result = []
    for row in re.split(r"\n" + re.escape(RECORD) + r"\n", text):
        parts = row.split(FIELD)
        if len(parts) != 3 or not re.fullmatch(r"\d+", parts[0].strip()):
            raise ValueError("Storyboard record must have index, prompt, video_prompt")
        index, prompt, video = int(parts[0]), parts[1].strip(), parts[2].strip()
        if not prompt or any(x in prompt + video for x in (START, END, FIELD, RECORD, "===")):
            raise ValueError("Empty image prompt or mixed/colliding output protocols")
        images = {int(x) for x in re.findall(r"\bimage (\d+)\b", prompt, re.I)}
        panel_image_capacity, panel_video_capacity = image_capacity, video_capacity
        if selected is not None:
            group = selected[index]
            panel_image_capacity = min(
                image_capacity, sum(a.get("first_frame", True) for a in group)
            )
            panel_video_capacity = 1 + min(max(0, video_capacity - 1), len(group))
        if any(x < 1 or x > panel_image_capacity for x in images):
            raise ValueError("Image reference exceeds actual workflow slots")
        duration = _video(
            video, min_seconds, max_seconds, panel_video_capacity, audio_capacity, mode
        )
        asset_ids = []
        first_frame_ids = []
        if selected is not None:
            assets = selected[index]
            for asset in assets:
                if (asset.get("first_frame", True) and asset["name"] not in prompt) or asset[
                    "name"
                ] not in video:
                    raise ValueError(f"Missing exact asset name: {asset['name']}")
            asset_ids = [a["asset_id"] for a in assets]
            first_frame_ids = [a["asset_id"] for a in assets if a.get("first_frame", True)]
        result.append(
            dict(
                panel_index=index,
                prompt=prompt,
                video_prompt=video,
                duration_seconds=duration,
                asset_ids=asset_ids,
                first_frame_asset_ids=first_frame_ids,
            )
        )
    if [x["panel_index"] for x in result] != [x["panel_index"] for x in panels]:
        raise ValueError(
            "Storyboard indices/order do not exactly match input; output may be truncated"
        )
    return result


def summary(text: str) -> dict:
    return Summary.model_validate(json_value(text)).model_dump()


def continuity(text: str) -> dict:
    return Continuity.model_validate(json_value(text)).model_dump()
