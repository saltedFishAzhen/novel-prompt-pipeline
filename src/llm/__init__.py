"""Shared LLM layer for prompt evaluation and production inference."""

from .client import LLMError, OpenAILLM
from .config import LLMConfig

__all__ = ["LLMConfig", "LLMError", "OpenAILLM"]
