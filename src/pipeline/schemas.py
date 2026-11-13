"""Strict structural contracts for inference outputs.

The pipeline still applies its own source-fidelity, identity and ordering
rules after parsing a model response.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class RewriteItem(StrictModel):
    panel_index: int
    text: str


class RewriteResult(StrictModel):
    results: list[RewriteItem]


class AssetDescription(StrictModel):
    name: str
    aliases: str
    description: str


class AssetBinding(StrictModel):
    asset_ids: list[str]
    # A missing field preserves the legacy meaning: all assets occur in frame 1.
    first_frame_asset_ids: list[str] = Field(default_factory=list)


class Summary(StrictModel):
    story: str
    chapter: str


class Continuity(StrictModel):
    summary: str


class ShotChoice(StrictModel):
    panel_index: int = Field(ge=0)
    generation_mode: Literal["i2va", "ref2va"]
    reason: str = Field(min_length=1)


class ShotPlan(StrictModel):
    shots: list[ShotChoice]
