"""Vision LLM compilation, with explicit media binding for comfyui_client."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from comfyui_client import BindingError, ComfyUIClient, ComfyUIError, build_graph
from comfyui_client.validation import validate_remote_path

from .prompts import I2VA_FIELDS, I2VA_HEADER, REF2VA_FIELDS, SYSTEM_PROMPT

PROFILES = ("i2va", "ref2va")
#: Per-image ceiling for base64 vision input. Larger files only burn memory and
#: are refused by most hosted providers anyway.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
_CUT_AT = r" At (\d{2}):([0-5]\d)\.(\d{3}),"
_CUT_TIME = re.compile(r"\[Shot \d+\]" + _CUT_AT)


class H3Error(ValueError):
    """Input, model response or workflow binding cannot be compiled faithfully."""


def _bind(profile: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """Build the profile graph, reporting every binding problem as ``H3Error``."""
    try:
        return build_graph(profile, values)
    except BindingError as exc:
        raise H3Error(f"invalid workflow values for {profile}: {exc}") from None


def _canonical_prompt(value: Any) -> str:
    """Normalise representation only: line endings, heading case, marker numbers."""
    if not isinstance(value, str) or not value.strip():
        raise H3Error("LLM must return a non-empty prompt string")
    prompt = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    field_pattern = "|".join(sorted(set(I2VA_FIELDS) | set(REF2VA_FIELDS)))
    prompt = re.sub(
        rf"(?im)^({field_pattern})(?=:[ \t]*)", lambda match: match.group(0).lower(), prompt
    )
    # Zero-padding is representation only; one spelling keeps the prompt and the
    # picture slots from drifting apart.
    return re.sub(r"<(Picture|Subject)\s+0*(\d+)>", r"<\1 \2>", prompt)


@dataclass(frozen=True)
class ReferenceImage:
    """Local vision input, optional storyboard alias and existing ComfyUI name.

    ``remote_name`` is relative to the target ComfyUI input directory, not a URL
    or local path. Omit it when :meth:`H3Compilation.to_values` will upload files.
    """

    path: str | Path
    name: str | None = None
    remote_name: str | None = None

    def __post_init__(self) -> None:
        try:
            resolved = Path(self.path).expanduser().resolve()
        except (TypeError, ValueError, OSError) as exc:
            raise H3Error(f"invalid reference image path: {exc}") from None
        object.__setattr__(self, "path", resolved)
        if self.name is not None and (not isinstance(self.name, str) or not self.name.strip()):
            raise H3Error("image name must be a non-empty alias")
        if self.remote_name is not None:
            # A remote name is relative to the ComfyUI input directory and is
            # validated by the same rules the binding layer applies later; it is
            # never reinterpreted as a local path.
            try:
                remote = validate_remote_path(self.remote_name, "remote_name")
            except ValueError as exc:
                raise H3Error(f"invalid remote_name: {exc}") from None
            object.__setattr__(self, "remote_name", remote)


ImageInput = ReferenceImage | str | Path


def _image(value: ImageInput) -> ReferenceImage:
    if isinstance(value, ReferenceImage):
        return value
    if isinstance(value, (str, Path)):
        return ReferenceImage(value)
    raise H3Error("images must be ReferenceImage objects or local file paths")


def _data_url(image: ReferenceImage) -> str:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    content_type = mime.get(image.path.suffix.lower())
    if not content_type:
        raise H3Error(f"unsupported image format: {image.path.suffix}; use PNG, JPEG or WebP")
    limit = MAX_IMAGE_BYTES // (1024 * 1024)
    try:
        if image.path.stat().st_size > MAX_IMAGE_BYTES:
            raise H3Error(f"reference image is larger than {limit} MB: {image.path}")
        data = image.path.read_bytes()
    except OSError as exc:
        raise H3Error(f"cannot read reference image {image.path}: {exc}") from None
    if not data:
        raise H3Error(f"empty reference image: {image.path}")
    return f"data:{content_type};base64," + base64.b64encode(data).decode("ascii")


def _remote_names(images: Sequence[ReferenceImage]) -> dict[Path, str]:
    """Map local files to their ComfyUI names, rejecting conflicting names."""
    known: dict[Path, str] = {}
    for image in images:
        if not image.remote_name:
            continue
        previous = known.setdefault(image.path, image.remote_name)
        if previous != image.remote_name:
            raise H3Error("the same local image has conflicting remote_name values")
    return known


def _upload(client: ComfyUIClient, path: Path) -> str:
    try:
        return client.upload_file(path)
    except (ComfyUIError, OSError, ValueError) as exc:
        raise H3Error(f"cannot upload {path}: {exc}") from None


@dataclass(frozen=True)
class H3Compilation:
    """Validated compilation result; safe to upload, bind and serialise."""

    profile: str
    prompt: str
    reference_images: tuple[ReferenceImage, ...]

    def __post_init__(self) -> None:
        # Exported type, so re-check instead of trusting the caller or `compile`.
        if not isinstance(self.profile, str) or self.profile not in PROFILES:
            raise H3Error("compilation profile must be i2va or ref2va")
        if isinstance(self.reference_images, (str, bytes)) or not isinstance(
            self.reference_images, Sequence
        ):
            raise H3Error("reference_images must be an ordered list of images")
        images = tuple(self.reference_images)
        if not images or not all(isinstance(image, ReferenceImage) for image in images):
            raise H3Error("reference_images must contain 1-9 ReferenceImage objects")
        if len(images) > 9 or (self.profile == "i2va" and len(images) != 1):
            raise H3Error("i2va requires exactly 1 image; ref2va supports 1-9 reference images")
        _remote_names(images)
        prompt = _canonical_prompt(self.prompt)
        _validate_prompt(self.profile, prompt, len(images))
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "reference_images", images)

    def to_values(
        self,
        comfyui_client: ComfyUIClient | None = None,
        *,
        values: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return exact ``build_graph(profile, values)`` / CLI ``--values`` input.

        Upload images missing remote_name when a ComfyUI client is supplied. This
        does not submit or generate a video. Existing remote names are trusted to
        belong to this server. Each local file is uploaded once per call.
        """
        result = dict(values or {})
        reserved = {"prompt", "first_frame", "reference_images", "reference_audios"} & result.keys()
        if reserved:
            raise H3Error("values cannot override compiled fields: " + ", ".join(sorted(reserved)))
        # Validate generation settings before uploading any image. The placeholder
        # media field must match the profile: i2va binds first_frame, ref2va binds
        # reference slots.
        placeholder = (
            {"reference_images": ["validation.png"]}
            if self.profile == "ref2va"
            else {"first_frame": "validation.png"}
        )
        _bind(self.profile, {**result, **placeholder, "prompt": self.prompt})
        self._check_cut_times(result.get("length"))
        images = self.reference_images
        known = _remote_names(images)
        missing = [image for image in images if image.path not in known]
        if missing:
            if comfyui_client is None:
                raise H3Error(
                    "set remote_name for each image or pass a ComfyUIClient to upload images"
                )
            for image in missing:
                known[image.path] = _upload(comfyui_client, image.path)
        result["prompt"] = self.prompt
        if self.profile == "ref2va":
            # Reference order is picture order: <Picture 1> is ref_image_0.
            result["reference_images"] = [known[image.path] for image in images]
        else:
            # i2va's first_frame is the real opening frame.
            result["first_frame"] = known[images[0].path]
        _bind(self.profile, result)
        return result

    def _check_cut_times(self, length: Any) -> None:
        cuts = [
            int(minutes) * 60 + int(seconds) + int(millis) / 1000
            for minutes, seconds, millis in _CUT_TIME.findall(self.prompt)
        ]
        if not cuts:
            return
        if length is None:
            # The workflow default duration would silently truncate a multi-shot
            # storyboard, so the runtime must be stated instead of assumed.
            raise H3Error(
                "multi-shot prompts require an explicit length in seconds so cut "
                "times can be checked against the real duration"
            )
        if any(cut >= float(length) for cut in cuts):
            raise H3Error("shot cut times must fall within the requested length in seconds")


class H3Client:
    """OpenAI SDK Chat Completions client for a vision-capable compiler LLM.

    H3_LLM_MODEL/BASE_URL/API_KEY take precedence over OPENAI_MODEL/BASE_URL/API_KEY.
    The model here is the compiler LLM, not the H3 video-generation model.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 120.0,
        sdk_client: OpenAI | None = None,
    ) -> None:
        self.model = model or os.getenv("H3_LLM_MODEL") or os.getenv("OPENAI_MODEL")
        if not self.model or not self.model.strip():
            raise H3Error("set H3_LLM_MODEL or pass model (a vision-capable compiler LLM)")
        self.reasoning_effort = reasoning_effort or os.getenv("H3_LLM_REASONING_EFFORT") or "low"
        if self.reasoning_effort not in {"low", "medium", "high"}:
            raise H3Error("reasoning_effort must be low, medium or high")
        self._owns_client = sdk_client is None
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            token = api_key or os.getenv("H3_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
            if not token:
                raise H3Error(
                    "set H3_LLM_API_KEY or pass api_key (use a dummy key for a keyless server)"
                )
            self._sdk = OpenAI(
                base_url=base_url or os.getenv("H3_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
                api_key=token,
                timeout=timeout,
                max_retries=0,
            )

    def __enter__(self) -> H3Client:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._sdk.close()

    def compile(
        self,
        type: str,
        storyboard: str,
        reference_images: Sequence[ImageInput],
    ) -> H3Compilation:
        """Compile without uploading media or contacting the video server.

        I2VA takes exactly one image, its opening frame. REF2VA takes 1-9
        ordinary image references. Frame-anchor roles come only from storyboard.
        """
        if not isinstance(type, str) or type not in PROFILES:
            raise H3Error("type must be i2va or ref2va")
        if not isinstance(storyboard, str) or not storyboard.strip():
            raise H3Error("storyboard must be non-empty text")
        if isinstance(reference_images, (str, bytes)) or not isinstance(reference_images, Sequence):
            raise H3Error("reference_images must be an ordered list of images")
        images = tuple(_image(value) for value in reference_images)
        if (type == "i2va" and len(images) != 1) or not 1 <= len(images) <= 9:
            raise H3Error("i2va requires exactly 1 image; ref2va supports 1-9 reference images")
        aliases = [image.name or f"image{i}" for i, image in enumerate(images, 1)]
        if len(aliases) != len(set(aliases)):
            raise H3Error("reference image aliases must be unique")
        for number in re.findall(r"<Picture\s+(\d+)>", storyboard):
            if not 1 <= int(number) <= len(images):
                raise H3Error(f"storyboard references unavailable <Picture {number}>")

        manifest = {
            "type": type,
            "storyboard": storyboard,
            "reference_images": [
                {"label": f"<Picture {i}>", "name": image.name or f"image{i}"}
                for i, image in enumerate(images, 1)
            ],
            "first_frame": "<Picture 1>" if type == "i2va" else None,
        }
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(manifest, ensure_ascii=False)}
        ]
        for i, image in enumerate(images, 1):
            content.extend(
                [
                    {"type": "text", "text": f"<Picture {i}>; alias: {image.name or f'image{i}'}"},
                    {"type": "image_url", "image_url": {"url": _data_url(image)}},
                ]
            )
        try:
            # Do not force response_format or sampling parameters: compatible
            # endpoints differ in support. The system message requests JSON.
            response = self._sdk.chat.completions.create(
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
        except OpenAIError as exc:
            # Some gateways echo request credentials in error bodies.
            status = getattr(exc, "status_code", None)
            raise H3Error(
                f"LLM request failed: {exc.__class__.__name__} (status={status})"
            ) from None
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise H3Error("LLM did not finish normally; refusing a truncated or filtered prompt")
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise H3Error("LLM refused the compilation")
        content_reply = message.content
        if not isinstance(content_reply, str):
            # Some compatible servers answer with content parts instead of text.
            raise H3Error("LLM must return a JSON object with a prompt string")
        try:
            payload = json.loads(content_reply)
        except json.JSONDecodeError as exc:
            raise H3Error("LLM must return a JSON object with a prompt string") from exc
        if isinstance(payload, dict) and set(payload) == {"error"}:
            raise H3Error(f"LLM could not compile faithfully: {payload['error']}")
        if not isinstance(payload, dict) or set(payload) != {"prompt"}:
            raise H3Error("LLM must return exactly one prompt field")
        return H3Compilation(type, payload["prompt"], images)


def _validate_prompt(profile: str, prompt: Any, image_count: int) -> None:
    if not isinstance(prompt, str) or not prompt.strip() or "```" in prompt:
        raise H3Error("compiled prompt must be non-empty H3 text without Markdown fences")
    prompt = prompt.strip()
    fields = I2VA_FIELDS if profile == "i2va" else REF2VA_FIELDS
    all_fields = set(I2VA_FIELDS) | set(REF2VA_FIELDS)
    headings = list(re.finditer(r"(?m)^(\w+):[ \t]*", prompt))
    actual = [match.group(1) for match in headings if match.group(1) in all_fields]
    if actual != list(fields):
        raise H3Error(
            f"invalid {profile} sections; expected: {', '.join(fields)}; "
            f"found: {', '.join(actual) or 'none'}; "
            "body lines must not start with a section name"
        )
    if profile == "i2va":
        if not prompt.startswith(I2VA_HEADER + "\n\n" + fields[0] + ":"):
            raise H3Error(
                "i2va prompt must start with the official first-frame instruction and blank line"
            )
    elif not prompt.startswith(fields[0] + ":"):
        raise H3Error("ref2va prompt must start with subject_definitions:")
    matches = [match for match in headings if match.group(1) in fields]
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        text = prompt[match.end() : end].strip()
        if not text:
            raise H3Error(f"empty H3 section: {match.group(1)}")
        sections[match.group(1)] = text
    description = sections[fields[0] if profile == "i2va" else "detailed_description"]
    shots = list(re.finditer(r"\[Shot (\d+)\]", description))
    if not shots or [int(shot.group(1)) for shot in shots] != list(range(1, len(shots) + 1)):
        raise H3Error("H3 description requires consecutive [Shot N] markers starting with [Shot 1]")
    # A cut declaration opens a paragraph; an inline marker silently changes how
    # the shot list is read. Style prose may still precede [Shot 1].
    for shot in shots[1:]:
        if not description[: shot.start()].endswith("\n"):
            raise H3Error(f"[Shot {shot.group(1)}] must start a new line")
    if re.match(r"\s*At \d", description[shots[0].end() :]):
        raise H3Error("[Shot 1] must not have a cut timestamp")
    # H3 continuity/end controls can occur inside speech. Keep rejecting
    # arbitrary nested tags while accepting the literal guide control tokens.
    dialogue = list(
        re.finditer(
            r"<d>\[[^\]\n]+\]\s*(?P<body>(?:[^<>]|<scenetrans>|<cutoff>)+)</d>",
            description,
        )
    )
    if len(dialogue) != description.count("<d>") or len(dialogue) != description.count("</d>"):
        raise H3Error("dialogue requires balanced <d>[Language] content</d> blocks")
    previous_end = 0
    seen_speakers: set[int] = set()
    subject_speakers: dict[int, int] = {}
    speaker_subjects: dict[int, int] = {}
    for event in dialogue:
        body = re.sub(r"<scenetrans>|<cutoff>", "", event.group("body")).strip()
        if not body:
            raise H3Error("dialogue must contain spoken text, not only control tokens")
        lead = description[previous_end : event.start()]
        speakers = list(re.finditer(r"\(S\d+(?:\s*,\s*S\d+)*\)", lead))
        if not speakers:
            raise H3Error(
                "each spoken event requires an explicit (Sx) speaker before its <d> block"
            )
        numbers = [int(number) for number in re.findall(r"S(\d+)", speakers[-1].group())]
        if len(numbers) != len(set(numbers)):
            raise H3Error("a compound speaker ID must not repeat the same speaker")
        for number in numbers:
            if number < 1:
                raise H3Error("speaker IDs must be positive, starting from S1")
            if number not in seen_speakers:
                if number != len(seen_speakers) + 1:
                    raise H3Error("speaker IDs must be assigned in order of first vocal event")
                seen_speakers.add(number)
        if profile == "ref2va":
            # Only unambiguous one-to-one tags can be checked safely. Group
            # voices and free-form narrator descriptions cannot identify a
            # unique referenced subject without semantic inference.
            for pair in re.finditer(r"<Subject\s+(\d+)>\s*\(S(\d+)\)", lead):
                subject, speaker = map(int, pair.groups())
                if subject in subject_speakers and subject_speakers[subject] != speaker:
                    raise H3Error(f"<Subject {subject}> changes its speaker ID")
                if speaker in speaker_subjects and speaker_subjects[speaker] != subject:
                    raise H3Error(f"S{speaker} changes its referenced subject")
                subject_speakers[subject] = speaker
                speaker_subjects[speaker] = subject
        previous_end = event.end()
    last_time = 0.0
    for shot in shots[1:]:
        stamp = re.match(_CUT_AT, description[shot.end() :])
        if stamp is None:
            raise H3Error("each subsequent shot requires At MM:SS.mmm, cut time")
        minutes, seconds, millis = map(int, stamp.groups())
        current = minutes * 60 + seconds + millis / 1000
        if current <= last_time:
            raise H3Error("shot cut times must strictly increase")
        last_time = current
    for number in re.findall(r"<Picture\s+(\d+)>", prompt):
        if not 1 <= int(number) <= image_count:
            raise H3Error(f"compiled prompt references unavailable <Picture {number}>")
    if re.search(r"<(?:Video|Audio)\s+\d+>", prompt):
        raise H3Error("compiled prompt invented an unavailable video/audio asset")
    if profile == "i2va" and re.search(r"<Subject\s+\d+>", prompt):
        raise H3Error("i2va must use the base guide rather than full-reference Subject labels")
    if profile == "ref2va":
        used = set(re.findall(r"<Subject\s+(\d+)>", prompt))
        defined = set(re.findall(r"(?m)^<Subject\s+(\d+)>", sections["subject_definitions"]))
        if not used.issubset(defined):
            raise H3Error("compiled prompt references an undefined Subject label")
        if not re.match(
            r"\[(?:reference generation|keyframe completion)(?: \+ (?:reference generation|keyframe completion))*\]",
            sections["summary"],
        ):
            raise H3Error("ref2va summary must begin with a supported reference task prefix")
