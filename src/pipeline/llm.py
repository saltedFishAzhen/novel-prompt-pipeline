"""Compatibility import; the implementation lives in the shared llm layer."""

from llm import LLMError, OpenAILLM

__all__ = ["LLMError", "OpenAILLM"]
