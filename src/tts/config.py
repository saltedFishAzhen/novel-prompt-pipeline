"""Configuration for a separately deployed Breeze TTS 2 service."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TTSConfig(BaseModel):
    """Only environment variable names, never a service URL or credentials."""

    model_config = ConfigDict(
        strict=True, extra="forbid", hide_input_in_errors=True, validate_assignment=True
    )

    url_env: str = "BREEZE_TTS_URL"
    api_key_env: str = "BREEZE_TTS_API_KEY"
    timeout: float = Field(default=180.0, gt=0, allow_inf_nan=False)
    max_audio_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)

    @field_validator("url_env", "api_key_env")
    @classmethod
    def valid_environment_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("TTS environment settings must name environment variables")
        return value
