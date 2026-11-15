"""Visual directing into a human-readable table; no ShotPlan schema or export LLM."""

from __future__ import annotations

import base64
import copy
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from h3_client import ReferenceImage

from .prompts import SYSTEM_PROMPT


class DirectorError(ValueError):
    """Input or provider response is unusable."""


def _setting(name: str) -> str | None:
    for prefix in ("DIRECTOR_LLM_", "SHOT_LLM_", "H3_LLM_", "OPENAI_"):
        value = os.getenv(prefix + name)
        if value:
            return value
    return None


def _values(value: Mapping[str, Any] | None) -> dict[str, int]:
    if value is not None and not isinstance(value, Mapping):
        raise DirectorError("values must be an object")
    result = dict(value or {})
    if set(result) - {"seed", "width", "height"}:
        raise DirectorError("values supports seed/width/height; length comes from duration_seconds")
    for key, item in result.items():
        if not isinstance(item, int) or isinstance(item, bool) or (key != "seed" and item <= 0):
            raise DirectorError(f"invalid values.{key}")
    if ("width" in result) != ("height" in result):
        raise DirectorError("width and height must be supplied together")
    return result


@dataclass
class DirectorResult:
    table_text: str
    type: str
    source_text: str
    reference_images: tuple[ReferenceImage, ...]
    duration_seconds: int
    reference_roles: list[dict[str, Any]] | None = None
    constraints: list[str] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "generated"

    @property
    def report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            **copy.deepcopy(self.metadata),
            "scope": "director text generated; shot_client performs table validation",
        }

    def to_shot_request(
        self,
        *,
        values: Mapping[str, Any] | None = None,
        table_file: str | Path | None = None,
    ) -> dict[str, Any]:
        if self.status != "generated":
            raise DirectorError("director requested clarification; no table can be processed")
        request: dict[str, Any] = {
            "type": self.type,
            "source_text": self.source_text,
            "duration_seconds": self.duration_seconds,
            "reference_images": [
                {
                    "path": str(image.path),
                    **({"name": image.name} if image.name else {}),
                    **({"remote_name": image.remote_name} if image.remote_name else {}),
                }
                for image in self.reference_images
            ],
        }
        if table_file is None:
            request["storyboard_table"] = self.table_text
        elif not str(table_file).strip():
            raise DirectorError("table_file must not be empty")
        else:
            request["table_file"] = str(table_file)
        if self.reference_roles is not None:
            request["reference_roles"] = copy.deepcopy(self.reference_roles)
        if self.constraints:
            request["constraints"] = list(self.constraints)
        if self.preferences:
            request["preferences"] = copy.deepcopy(self.preferences)
        if values is not None:
            request["values"] = _values(values)
        return request


class DirectorClient:
    """OpenAI SDK vision director, using the existing .env settings and low by default."""

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 180.0,
        sdk_client: OpenAI | None = None,
    ) -> None:
        self.model = model or _setting("MODEL")
        if not isinstance(self.model, str) or not self.model.strip():
            raise DirectorError("set DIRECTOR_LLM_MODEL / H3_LLM_MODEL or pass model")
        self.reasoning_effort = reasoning_effort or _setting("REASONING_EFFORT") or "low"
        if self.reasoning_effort not in {"low", "medium", "high"}:
            raise DirectorError("reasoning_effort must be low, medium or high")
        self._owns_client = sdk_client is None
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            token = api_key or _setting("API_KEY")
            if not token:
                raise DirectorError("set DIRECTOR_LLM_API_KEY / H3_LLM_API_KEY or pass api_key")
            self._sdk = OpenAI(
                base_url=base_url or _setting("BASE_URL"),
                api_key=token,
                timeout=timeout,
                max_retries=0,
            )

    def __enter__(self) -> DirectorClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._sdk.close()

    def direct(
        self,
        type: str,
        source_text: str,
        reference_images: Sequence[ReferenceImage | str | Path],
        duration_seconds: int,
        *,
        reference_roles: Sequence[Mapping[str, Any]] | None = None,
        constraints: Sequence[str] | None = None,
        preferences: Mapping[str, Any] | None = None,
    ) -> DirectorResult:
        if type not in ("i2va", "ref2va"):
            raise DirectorError("type must be i2va or ref2va")
        if not isinstance(source_text, str) or not source_text.strip():
            raise DirectorError("source_text must be non-empty")
        if (
            not isinstance(duration_seconds, int)
            or isinstance(duration_seconds, bool)
            or duration_seconds <= 0
        ):
            raise DirectorError("duration_seconds must be a positive integer")
        if isinstance(reference_images, (str, bytes)) or not isinstance(reference_images, Sequence):
            raise DirectorError("reference_images must be an ordered list")
        images = []
        for value in reference_images:
            if isinstance(value, ReferenceImage):
                images.append(value)
            elif isinstance(value, (str, Path)):
                images.append(ReferenceImage(value))
            else:
                raise DirectorError("images must be ReferenceImage objects or local paths")
        if not 1 <= len(images) <= 9 or (type == "i2va" and len(images) != 1):
            raise DirectorError("i2va requires 1 image; ref2va supports 1-9 images")
        aliases = [image.name or f"image{i}" for i, image in enumerate(images, 1)]
        if len(aliases) != len(set(aliases)):
            raise DirectorError("image aliases must be unique")
        if constraints is not None and (
            isinstance(constraints, (str, bytes))
            or not isinstance(constraints, Sequence)
            or any(not isinstance(v, str) or not v.strip() for v in constraints)
        ):
            raise DirectorError("constraints must be a list of non-empty strings")
        if preferences is not None and not isinstance(preferences, Mapping):
            raise DirectorError("preferences must be an object")
        prefs = copy.deepcopy(dict(preferences or {}))
        count = prefs.get("shot_count")
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count <= 0
        ):
            raise DirectorError("preferences.shot_count must be positive")
        roles = None
        if reference_roles is not None:
            if isinstance(reference_roles, (str, bytes)) or not isinstance(
                reference_roles, Sequence
            ):
                raise DirectorError("reference_roles must be a list")
            roles = []
            for item in reference_roles:
                if not isinstance(item, Mapping) or set(item) - {
                    "picture",
                    "subject",
                    "use",
                    "exclude",
                }:
                    raise DirectorError("invalid reference role fields")
                role = dict(item)
                picture = role.get("picture")
                if (
                    not isinstance(picture, int)
                    or isinstance(picture, bool)
                    or not 1 <= picture <= len(images)
                ):
                    raise DirectorError("reference role uses unavailable Picture number")
                if not isinstance(role.get("subject"), str) or not role["subject"].strip():
                    raise DirectorError("reference role requires a subject")
                for key in ("use", "exclude"):
                    items = role.get(key, [])
                    if not isinstance(items, list) or any(
                        not isinstance(v, str) or not v.strip() for v in items
                    ):
                        raise DirectorError(f"reference_roles.{key} must be a string list")
                    role[key] = list(items)
                if not role["use"]:
                    raise DirectorError("reference role must state what to use")
                roles.append(role)
        request = {
            "type": type,
            "source_text": source_text,
            "duration_seconds": duration_seconds,
            "reference_images": [
                {"label": f"图{i}", "name": aliases[i - 1]} for i in range(1, len(images) + 1)
            ],
            "reference_roles": roles,
            "constraints": list(constraints or []),
            "preferences": prefs,
        }
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(request, ensure_ascii=False)}
        ]
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        for i, image in enumerate(images, 1):
            kind = mime.get(image.path.suffix.lower())
            if not kind:
                raise DirectorError("images must be PNG, JPEG or WebP")
            data = image.path.read_bytes()
            if not data:
                raise DirectorError("reference image is empty")
            content.extend(
                [
                    {"type": "text", "text": f"图{i}；别名：{aliases[i - 1]}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{kind};base64," + base64.b64encode(data).decode("ascii")
                        },
                    },
                ]
            )
        started = time.monotonic()
        try:
            response = self._sdk.chat.completions.create(
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
        except OpenAIError as exc:
            raise DirectorError(
                f"director request failed: {exc.__class__.__name__} (status={getattr(exc, 'status_code', None)})"
            ) from None
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise DirectorError("director did not finish normally; refusing a partial table")
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise DirectorError("director refused the request")
        if not isinstance(message.content, str) or not message.content.strip():
            raise DirectorError("director returned no text")
        text = message.content.strip()
        status = (
            "needs_input" if text.startswith(("需要补充信息：", "需要补充信息:")) else "generated"
        )
        usage = getattr(response, "usage", None)
        return DirectorResult(
            table_text=text,
            type=type,
            source_text=source_text,
            reference_images=tuple(images),
            duration_seconds=duration_seconds,
            reference_roles=roles,
            constraints=list(constraints or []),
            preferences=prefs,
            status=status,
            metadata={
                "model": getattr(response, "model", self.model),
                "reasoning_effort": self.reasoning_effort,
                "seconds": round(time.monotonic() - started, 3),
                "usage": usage.model_dump() if usage else None,
            },
        )
