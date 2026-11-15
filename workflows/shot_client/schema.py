"""Typed, lossless representation of a director's Markdown shot table."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DialogueEvent(_Model):
    speaker: str = Field(min_length=1)
    language: str | None = None
    text: str = Field(min_length=1)
    start_ms: int | None = None
    end_ms: int | None = None
    group_id: str | None = None
    part_index: int | None = None
    part_total: int | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("dialogue needs both a start and an end time, or neither")
        if self.start_ms is not None and self.end_ms is not None:
            if self.start_ms < 0 or self.end_ms <= self.start_ms:
                raise ValueError("dialogue time must be nonnegative and end after it starts")
        if self.group_id is not None:
            if self.part_total is None or self.part_index is None or self.part_total < 2:
                raise ValueError("split dialogue needs 1..total parts with total >= 2")
            if not 1 <= self.part_index <= self.part_total:
                raise ValueError("split dialogue part index is outside 1..total")
            if self.start_ms is None:
                raise ValueError("split dialogue requires an explicit absolute time range")
        elif self.part_index is not None or self.part_total is not None:
            raise ValueError("dialogue parts require a group id")
        return self


class ShotRow(_Model):
    number: int = Field(ge=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    reference_images: list[int]
    reference_text: str
    time_text: str
    shot_size: str
    camera_angle: str
    camera_movement: str
    blocking: str
    emotion_subtext: str
    dialogue_text: str
    dialogue_events: list[DialogueEvent]
    dialogue_body_relation: str
    scene_layers: str
    continuity: str
    director_notes: str
    transition: Literal["opening", "cut", "cross_dissolve", "fade", "wipe"]
    transition_duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("shot must end after it starts")
        if any(number < 1 for number in self.reference_images):
            raise ValueError("reference image numbers start at 1")
        if (self.transition in {"opening", "cut"}) != (self.transition_duration_ms == 0):
            raise ValueError("only a soft transition has a duration")
        if self.transition == "opening" and self.number != 1:
            raise ValueError("only the first shot can be the opening")
        for event in self.dialogue_events:
            if event.start_ms is None:
                continue
            if not self.start_ms <= event.start_ms < event.end_ms <= self.end_ms:
                raise ValueError(f"dialogue time falls outside shot {self.number}")
        return self


class ShotTable(_Model):
    type: Literal["i2va", "ref2va"]
    duration_ms: int = Field(gt=0)
    rows: list[ShotRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous(self) -> Self:
        expected = 0
        for index, row in enumerate(self.rows, 1):
            if row.number != index:
                raise ValueError("shot numbers must be consecutive starting at 1")
            if row.start_ms != expected:
                raise ValueError(f"shot {index}: times must cover the timeline without gaps")
            expected = row.end_ms
        if expected != self.duration_ms:
            raise ValueError("shots must end exactly at the table duration")
        return self
