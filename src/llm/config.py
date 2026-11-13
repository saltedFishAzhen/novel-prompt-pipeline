"""Shared LLM configuration and environment mapping for all consumers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", env_ignore_empty=True, hide_input_in_errors=True, strict=True
    )

    model: str = Field(default="", validation_alias="LLM_MODEL")
    base_url: str = Field(default="", validation_alias="OPENAI_BASE_URL")


_SENSITIVE_KEYS = frozenset(
    {"api_key", "authorization", "password", "client_secret", "access_token", "token", "x_api_key"}
)


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("Credentials must be provided through environment variables")
            _reject_secrets(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secrets(nested)


class LLMConfig(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", hide_input_in_errors=True, validate_assignment=True
    )

    model: str = ""
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    api: Literal["chat_completions", "responses"] = "chat_completions"
    timeout: float = Field(default=180, gt=0, allow_inf_nan=False)
    max_retries: int = Field(default=2, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> LLMConfig:
        """Load shared environment settings, overridden by nonempty config values.

        Existing project configs use empty model/base_url strings as placeholders
        for environment values. All other supplied fields retain their values.
        """
        data = dict(overrides or {})
        environment = _EnvironmentSettings()
        if "model" not in data or data["model"] == "":
            data["model"] = environment.model
        if "base_url" not in data or data["base_url"] == "":
            data["base_url"] = environment.base_url
        return cls.model_validate(data)

    @field_validator("api_key_env")
    @classmethod
    def valid_api_key_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must name an environment variable")
        return value

    @field_validator("base_url")
    @classmethod
    def no_credentials_in_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Credentials must not be embedded in base_url")
            if any(
                k.casefold().replace("-", "_") in _SENSITIVE_KEYS
                for k, _ in parse_qsl(parsed.query)
            ):
                raise ValueError("Credentials must not be embedded in base_url")
        return value

    @field_validator("extra")
    @classmethod
    def no_credentials_in_extra(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_secrets(value)
        return value
