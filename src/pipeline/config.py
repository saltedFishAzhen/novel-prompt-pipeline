from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv as _load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novel_prompt_pipeline.resources import default_workspace
from tts import TTSConfig

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
    image_backend: Literal["codex", "comfyui"] = "codex"
    # Retained only for reading older Codex configurations; not an active backend.
    image_profile: str | None = None
    video_profile: str = "ref2va"
    i2va_profile: str = "i2va"
    bindings_file: str | None = None
    workflows_dir: str | None = None
    # Keep dictionaries as the public interface; the engine indexes each item.
    reference_audios: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    asset_width: int = 1536
    asset_height: int = 864
    prop_size: int = 1024
    character_face_references: dict[str, str] = Field(default_factory=dict)
    character_face_reference_scopes: dict[str, list[Literal["hair", "face_shape", "full_face"]]] = (
        Field(default_factory=dict)
    )
    scene_reverse_references: dict[str, str] = Field(default_factory=dict)
    frame_width: int = 576
    frame_height: int = 1024
    timeout: float = Field(default=1800, gt=0, allow_inf_nan=False)
    poll_interval: float = Field(default=2, gt=0, allow_inf_nan=False)
    seed: int = Field(default=1, ge=0)

    @field_validator("image_backend")
    @classmethod
    def image_backend_is_codex(cls, value: str) -> str:
        if value != "codex":
            raise ValueError(
                "ComfyUI image generation has been retired; use the Codex image backend"
            )
        return value

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

    @field_validator("character_face_reference_scopes")
    @classmethod
    def validate_character_face_scopes(cls, values: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, scope in values.items():
            if (
                not name.strip()
                or not scope
                or len(scope) != len(set(scope))
                or ("full_face" in scope and "face_shape" in scope)
            ):
                raise ValueError(
                    "Character reference scope must be nonempty, distinct and unambiguous"
                )
        return values

    @field_validator("scene_reverse_references")
    @classmethod
    def validate_scene_reverse_references(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not value.strip() for key, value in values.items()):
            raise ValueError("Scene reverse reference names and paths must be nonempty")
        return values

    @field_validator("reference_audios")
    @classmethod
    def validate_audio(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Preserve absent optional fields so old run identity hashes remain usable.
        return [
            _AudioReference.model_validate(value).model_dump(exclude_unset=True) for value in values
        ]


class ProductionConfig(_StrictModel):
    episodes: int | None = Field(default=None, ge=1, le=1000)
    workers: int = Field(default=3, ge=1, le=8)
    max_repairs: int = Field(default=2, ge=0, le=5)
    episodes_per_batch: int = Field(default=3, ge=1, le=3)
    aspect_ratio: Literal["16:9", "9:16"] = "9:16"
    minutes_per_episode: float = Field(default=3.0, gt=0, le=60)


class Config(_StrictModel):
    production: ProductionConfig = Field(default_factory=ProductionConfig)
    asset_library: str = Field(
        default_factory=lambda: str(default_workspace() / "runs" / "library" / "assets.sqlite3")
    )
    project_id: str = "default"
    character_design_briefs: dict[str, str] = Field(default_factory=dict)
    media: MediaConfig = Field(default_factory=MediaConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    comfyui: ComfyUIConfig | None = None
    tts: TTSConfig | None = None

    @field_validator("project_id", "asset_library")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be empty")
        return value

    @field_validator("character_design_briefs")
    @classmethod
    def validate_character_design_briefs(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not value.strip() for key, value in values.items()):
            raise ValueError("Character design brief names and instructions must not be empty")
        return values

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be an object")
        config = cls.model_validate(data)
        if path:
            directory = Path(path).resolve().parent
            for name in ("asset_library",):
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
            for name, value in config.media.scene_reverse_references.items():
                if not Path(value).is_absolute():
                    config.media.scene_reverse_references[name] = str((directory / value).resolve())
        return config

    def public_dict(self) -> dict:
        # Lists can be mutated in place after initialization; revalidate them.
        # Secrets are only in environment variables and never become fields.
        result = self.__class__.model_validate(self.model_dump()).model_dump(mode="json")
        if not result["character_design_briefs"]:
            result.pop("character_design_briefs")
        # Even the default style affects media generation and must remain in
        # the run identity, including the resolved text stored by the pipeline.
        if result["media"]["prop_size"] == 1024:
            result["media"].pop("prop_size")
        if result["media"]["image_backend"] == "codex":
            result["media"].pop("image_profile", None)
        if not result["media"]["character_face_references"]:
            result["media"].pop("character_face_references")
        if not result["media"]["character_face_reference_scopes"]:
            result["media"].pop("character_face_reference_scopes")
        if not result["media"]["scene_reverse_references"]:
            result["media"].pop("scene_reverse_references")
        if result["comfyui"] is None:
            result.pop("comfyui")
        if result["tts"] is None:
            result.pop("tts")
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
