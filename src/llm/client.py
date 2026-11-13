from __future__ import annotations

import os

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class OpenAILLM:
    """Small SDK adapter supporting OpenAI-compatible chat endpoints and Responses."""

    def __init__(self, config: LLMConfig, client=None):
        if not config.model:
            raise ValueError("Set LLM_MODEL or llm.model before inference")
        self.config = config
        self._owns_client = client is None
        self._closed = False
        self.last_request_id = None
        if config.api not in {"chat_completions", "responses"}:
            raise ValueError("llm.api must be chat_completions or responses")
        if client is None:
            from openai import OpenAI

            key = os.getenv(config.api_key_env)
            if not key:
                raise ValueError(f"Set {config.api_key_env} before inference")
            options = dict(api_key=key, timeout=config.timeout, max_retries=config.max_retries)
            if config.base_url:
                options["base_url"] = config.base_url
            client = OpenAI(**options)
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client and not self._closed:
            self.client.close()
        self._closed = True

    def complete(self, prompt: str, *, correction: str = "") -> str:
        if self._closed:
            raise LLMError("LLM client is closed")
        request = dict(self.config.extra)
        if any(key in request for key in ("model", "messages", "input", "stream")):
            raise ValueError("llm.extra cannot override model/messages/input/stream")
        if correction:
            prompt += (
                "\n\n上次输出未通过程序校验，请按原始输入重新输出完整结果。校验错误：\n"
                + correction
            )
        if self.config.api == "responses":
            response = self.client.responses.create(
                model=self.config.model, input=prompt, **request
            )
            self.last_request_id = getattr(response, "_request_id", None)
            if response.status != "completed":
                raise LLMError(f"Responses request did not complete: {response.status}")
            text = response.output_text
        else:
            response = self.client.chat.completions.create(
                model=self.config.model, messages=[{"role": "user", "content": prompt}], **request
            )
            self.last_request_id = getattr(response, "_request_id", None)
            if len(response.choices) != 1:
                raise LLMError("Expected one completion choice")
            choice = response.choices[0]
            if choice.finish_reason != "stop":
                raise LLMError(f"Incomplete completion: finish_reason={choice.finish_reason}")
            if getattr(choice.message, "refusal", None):
                raise LLMError("The model refused this inference node")
            text = choice.message.content
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Model returned no text")
        return text
