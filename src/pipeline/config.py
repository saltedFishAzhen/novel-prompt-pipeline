from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv as _load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llm.config import LLMConfig
from novel_prompt_pipeline.resources import default_workspace, prompt_root

from .styles import StyleConfig

REPO_ROOT = default_workspace()


def load_dotenv(path: Path) -> None:
    """Load literal dotenv values without replacing existing environment values."""
    if path.is_file():
        _load_dotenv(dotenv_path=path, override=False, interpolate=False, encoding="utf-8-sig")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", hide_input_in_errors=True, validate_assignment=True
    )


class _AudioReference(_StrictModel):
    path: str
    usage: str
    remote: bool = False
    server: str | None = None

    @field_validator("path", "usage")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Audio reference path and usage must be nonempty")
        return value

    @model_validator(mode="after")
    def remote_server_only(self) -> _AudioReference:
        if self.server is not None and (not self.server.strip() or not self.remote):
            raise ValueError("Audio server is only valid for a remote audio reference")
        return self


class ComfyServerConfig(_StrictModel):
    """Names of environment variables; credentials are never stored here."""

    url_env: str | None = None
    api_key_env: str | None = None

    @field_validator("url_env", "api_key_env")
    @classmethod
    def validate_environment_name(cls, value: str | None) -> str | None:
        import re

        if value not in (None, "") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("Server environment variable names must be valid identifiers")
        return value


class ComfyUIConfig(_StrictModel):
    servers: dict[str, ComfyServerConfig] = Field(default_factory=dict)
    default_server: str | None = None
    image_servers: list[str] = Field(default_factory=list)
    ref2va_servers: list[str] = Field(default_factory=list)
    i2va_servers: list[str] = Field(default_factory=list)
    balance: Literal["round_robin"] = "round_robin"


class MediaConfig(_StrictModel):
    image_profile: str = "qwen"
    video_profile: str = "ref2va"
    i2va_profile: str = "i2va"
    bindings_file: str | None = None
    workflows_dir: str | None = None
    # Keep dictionaries as the public interface; the engine indexes each item.
    reference_audios: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    asset_width: int = 1024
    asset_height: int = 576
    prop_size: int = 1024
    character_face_references: dict[str, str] = Field(default_factory=dict)
    frame_width: int = 576
    frame_height: int = 1024
    timeout: float = Field(default=1800, gt=0, allow_inf_nan=False)
    poll_interval: float = Field(default=2, gt=0, allow_inf_nan=False)
    seed: int = Field(default=1, ge=0)

    @field_validator("asset_width", "asset_height", "frame_width", "frame_height")
    @classmethod
    def dimensions_are_multiples_of_32(cls, value: int) -> int:
        if value <= 0 or value % 32:
            raise ValueError("Media dimensions must be positive integer multiples of 32")
        return value

    @field_validator("prop_size")
    @classmethod
    def prop_size_is_multiple_of_64(cls, value: int) -> int:
        if value <= 0 or value % 64:
            raise ValueError("prop_size must be a positive multiple of 64")
        return value

    @field_validator("character_face_references")
    @classmethod
    def validate_character_faces(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not value.strip() for key, value in values.items()):
            raise ValueError("Character reference names and paths must be nonempty")
        return values

    @field_validator("reference_audios")
    @classmethod
    def validate_audio(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Preserve absent optional fields so old run identity hashes remain usable.
        return [
            _AudioReference.model_validate(value).model_dump(exclude_unset=True) for value in values
        ]


class Config(_StrictModel):
    prompt_project: str = Field(default_factory=lambda: str(prompt_root()))
    asset_library: str = Field(
        default_factory=lambda: str(default_workspace() / "runs" / "library" / "assets.sqlite3")
    )
    project_id: str = "default"
    reuse_existing_assets: bool = True
    prompt_version: str | None = None
    batch_size: int = Field(default=4, ge=1)
    first_panel_index: int = Field(default=0, ge=0)
    rewrite: bool = True
    summarize: bool = True
    strict_asset_validation: bool = True
    validation_retries: int = Field(default=1, ge=0)
    generation_mode: Literal["auto", "i2va", "ref2va"] = "auto"
    min_seconds: int = Field(default=4, ge=1)
    max_seconds: int = Field(default=15, ge=1)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    comfyui: ComfyUIConfig | None = None

    @field_validator("project_id", "prompt_project", "asset_library")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be empty")
        return value

    @model_validator(mode="after")
    def validate_durations(self) -> Config:
        if self.max_seconds < self.min_seconds:
            raise ValueError("Invalid duration range")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be an object")
        llm = data.get("llm", {})
        if not isinstance(llm, dict):
            raise ValueError("llm configuration must be an object")
        data["llm"] = LLMConfig.from_env(llm)
        config = cls.model_validate(data)
        if path:
            directory = Path(path).resolve().parent
            for name in ("prompt_project", "asset_library"):
                value = getattr(config, name)
                if not Path(value).is_absolute():
                    setattr(config, name, str((directory / value).resolve()))
            for name in ("bindings_file", "workflows_dir"):
                value = getattr(config.media, name)
                if value and not Path(value).is_absolute():
                    setattr(config.media, name, str((directory / value).resolve()))
            for audio in config.media.reference_audios:
                if not audio.get("remote") and not Path(audio["path"]).is_absolute():
                    audio["path"] = str((directory / audio["path"]).resolve())
            for name, value in config.media.character_face_references.items():
                if not Path(value).is_absolute():
                    config.media.character_face_references[name] = str(
                        (directory / value).resolve()
                    )
        return config

    def public_dict(self) -> dict:
        # Lists can be mutated in place after initialization; revalidate them.
        # Secrets are only in environment variables and never become fields.
        result = self.__class__.model_validate(self.model_dump()).model_dump(mode="json")
        # Even the default style affects media generation and must remain in
        # the run identity, including the resolved text stored by the pipeline.
        if result["media"]["prop_size"] == 1024:
            result["media"].pop("prop_size")
        if not result["media"]["character_face_references"]:
            result["media"].pop("character_face_references")
        if result["comfyui"] is None:
            result.pop("comfyui")
        return result


def verify_face_photo(path: str | Path) -> Path:
    """Reject missing, unsupported, or invalid user-supplied portrait files."""
    from PIL import Image, UnidentifiedImageError

    path = Path(path).expanduser().resolve()
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Portrait must be a PNG, JPEG, or WebP image")
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Portrait must be a PNG, JPEG, or WebP image")
            image.verify()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("Portrait image cannot be read") from exc
    return path
