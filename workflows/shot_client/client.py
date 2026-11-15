"""Programmatic validation and H3 export of a director's Markdown table."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from h3_client import ReferenceImage

from .render import render_storyboard
from .schema import ShotTable
from .validation import TableValidationError, parse_table


class ShotError(ValueError):
    """The table cannot be safely processed or exported."""


def _images(values: Sequence[ReferenceImage | str | Path], type: str) -> tuple[ReferenceImage, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ShotError("reference_images must be an ordered list of local images")
    result = []
    for value in values:
        if isinstance(value, ReferenceImage):
            image = value
        elif isinstance(value, (str, Path)):
            image = ReferenceImage(value)
        else:
            raise ShotError("each reference image must be a ReferenceImage or local path")
        if image.path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ShotError(f"unsupported image format: {image.path.suffix}")
        if not image.path.is_file() or image.path.stat().st_size == 0:
            raise ShotError(f"reference image is missing or empty: {image.path}")
        result.append(image)
    if not 1 <= len(result) <= 9 or type == "i2va" and len(result) != 1:
        raise ShotError("i2va requires one image; ref2va requires 1-9 images")
    aliases = [image.name or f"image{i}" for i, image in enumerate(result, 1)]
    if len(aliases) != len(set(aliases)):
        raise ShotError("reference image aliases must be unique")
    return tuple(result)


def _roles(
    roles: Sequence[Mapping[str, Any]] | None, image_count: int
) -> list[dict[str, Any]] | None:
    if roles is None:
        return None
    if isinstance(roles, (str, bytes)) or not isinstance(roles, Sequence) or not roles:
        raise ShotError("reference_roles must be a non-empty list")
    result = []
    seen = set()
    for role in roles:
        if not isinstance(role, Mapping) or set(role) - {"picture", "subject", "use", "exclude"}:
            raise ShotError("reference role supports picture/subject/use/exclude")
        number, subject, use, exclude = (
            role.get("picture"),
            role.get("subject"),
            role.get("use"),
            role.get("exclude", []),
        )
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 1 <= number <= image_count
        ):
            raise ShotError("reference role refers to an unavailable image")
        if not isinstance(subject, str) or not subject.strip():
            raise ShotError("reference role subject must be a non-empty string")
        for name, items in (("use", use), ("exclude", exclude)):
            if (
                not isinstance(items, list)
                or any(not isinstance(item, str) or not item.strip() for item in items)
                or name == "use"
                and not items
            ):
                raise ShotError(f"reference role {name} must be a list of non-empty strings")
        if (number, subject) in seen:
            raise ShotError("duplicate reference role")
        seen.add((number, subject))
        result.append(copy.deepcopy(dict(role)))
    return result


def _preferences(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ShotError("preferences must be an object")
    result = copy.deepcopy(dict(value or {}))
    if set(result) - {"shot_count", "default_transition", "allow_transitions"}:
        raise ShotError("preferences supports shot_count/default_transition/allow_transitions")
    allowed = {"cut", "cross_dissolve", "fade", "wipe"}
    if result.get("default_transition", "cut") not in allowed:
        raise ShotError("unsupported default_transition")
    transitions = result.get("allow_transitions", [])
    if not isinstance(transitions, list) or any(
        not isinstance(item, str) or item not in allowed for item in transitions
    ):
        raise ShotError("allow_transitions must list supported transition names")
    return result


def _generation_values(values: Mapping[str, Any] | None) -> dict[str, int]:
    if values is not None and not isinstance(values, Mapping):
        raise ShotError("values must be an object")
    result = dict(values or {})
    if set(result) - {"seed", "width", "height"}:
        raise ShotError("values supports seed/width/height; length comes from duration_seconds")
    for key, value in result.items():
        if not isinstance(value, int) or isinstance(value, bool) or key != "seed" and value <= 0:
            raise ShotError(f"invalid values.{key}")
    if ("width" in result) != ("height" in result):
        raise ShotError("width and height must be provided together")
    return result


@dataclass
class ShotResult:
    status: str
    table: ShotTable | None
    issues: list[dict[str, str]]
    type: str
    source_text: str
    reference_images: tuple[ReferenceImage, ...]
    duration_seconds: int
    table_text: str
    reference_roles: list[dict[str, Any]] | None = None
    constraints: list[str] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)

    @property
    def report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": copy.deepcopy(self.issues),
            "validation_scope": (
                "deterministic table structure, times, explicit reference numbers, quoted dialogue "
                "and selected transition/camera contradictions; free-text semantics require review"
            ),
            "shot_count": len(self.table.rows) if self.table else 0,
        }

    def to_h3_request(self, *, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Revalidate the original table and return existing H3 CLI input fields."""
        if self.status != "ready" or self.table is None:
            raise ShotError(f"cannot export a {self.status} table")
        settings = _generation_values(values)
        images = _images(self.reference_images, self.type)
        roles = _roles(self.reference_roles, len(images))
        prefs = _preferences(self.preferences)
        try:
            table = parse_table(
                self.table_text,
                type=self.type,
                source_text=self.source_text,
                duration_seconds=self.duration_seconds,
                image_count=len(images),
                preferences=prefs,
            )
        except TableValidationError as exc:
            raise ShotError(f"table is no longer valid: {exc}") from None
        if table != self.table:
            raise ShotError("table was modified after validation; process it again")
        entries = []
        for image in images:
            entry = {"path": str(image.path)}
            if image.name is not None:
                entry["name"] = image.name
            if image.remote_name is not None:
                entry["remote_name"] = image.remote_name
            entries.append(entry)
        return {
            "type": self.type,
            "storyboard": render_storyboard(
                table, reference_roles=roles, constraints=self.constraints
            ),
            "reference_images": entries,
            "values": {**settings, "length": self.duration_seconds},
        }


class ShotClient:
    """Pure local Markdown table processor; does not call an LLM or load .env."""

    def __enter__(self) -> ShotClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def process(
        self,
        table_text: str,
        *,
        type: str,
        source_text: str,
        reference_images: Sequence[ReferenceImage | str | Path],
        duration_seconds: int,
        reference_roles: Sequence[Mapping[str, Any]] | None = None,
        constraints: Sequence[str] | None = None,
        preferences: Mapping[str, Any] | None = None,
    ) -> ShotResult:
        images: tuple[ReferenceImage, ...] = ()
        roles = None
        prefs: dict[str, Any] = {}
        hard_constraints: list[str] = []
        # The reported path names the request field that failed, so a caller knows
        # whether to fix the table or the surrounding input.
        stage = "input"
        try:
            if not isinstance(type, str) or type not in {"i2va", "ref2va"}:
                raise ShotError("type must be i2va or ref2va")
            if not isinstance(source_text, str) or not source_text.strip():
                raise ShotError("source_text must be non-empty")
            if (
                not isinstance(duration_seconds, int)
                or isinstance(duration_seconds, bool)
                or duration_seconds <= 0
            ):
                raise ShotError("duration_seconds must be a positive integer")
            stage = "reference_images"
            images = _images(reference_images, type)
            stage = "reference_roles"
            roles = _roles(reference_roles, len(images))
            stage = "preferences"
            prefs = _preferences(preferences)
            stage = "constraints"
            if constraints is not None:
                if (
                    isinstance(constraints, (str, bytes))
                    or not isinstance(constraints, Sequence)
                    or any(not isinstance(item, str) or not item.strip() for item in constraints)
                ):
                    raise ShotError("constraints must be a list of non-empty strings")
                hard_constraints = list(constraints)
            stage = "table"
            table = parse_table(
                table_text,
                type=type,
                source_text=source_text,
                duration_seconds=duration_seconds,
                image_count=len(images),
                preferences=prefs,
            )
            return ShotResult(
                "ready",
                table,
                [],
                type,
                source_text,
                images,
                duration_seconds,
                table_text,
                roles,
                hard_constraints,
                prefs,
            )
        except (TableValidationError, ShotError, ValueError, TypeError, OSError) as exc:
            return ShotResult(
                "invalid",
                None,
                [{"path": stage, "message": str(exc)}],
                type,
                source_text,
                images,
                duration_seconds,
                table_text,
                roles,
                hard_constraints,
                prefs,
            )
