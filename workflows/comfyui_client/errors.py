"""Exception types shared by the ComfyUI client layers."""

from __future__ import annotations

from typing import Any


class ComfyUIError(RuntimeError):
    """ComfyUI request error with endpoint and optional job context."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status: int | None = None,
        prompt_id: str | None = None,
        payload: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.status = status
        self.prompt_id = prompt_id
        self.payload = payload
        details = []
        if prompt_id:
            details.append(f"prompt_id={prompt_id}")
        if status is not None:
            details.append(f"HTTP {status}")
        if endpoint:
            details.append(endpoint)
        super().__init__(f"{message} ({', '.join(details)})" if details else message)


class ComfyUITimeoutError(ComfyUIError):
    """A submitted prompt did not finish within the wait budget.

    The job is still owned by the server: call ``wait()`` again with a larger
    budget to keep waiting, or ``cancel()`` to drop it from the queue.
    """

    def __init__(
        self,
        message: str,
        *,
        wait_seconds: float | None = None,
        budget_seconds: float | None = None,
        endpoint: str | None = None,
        prompt_id: str | None = None,
    ) -> None:
        #: Wall-clock seconds actually spent waiting.
        self.wait_seconds = wait_seconds
        #: The budget the caller (or ``job_timeout``) allowed.
        self.budget_seconds = budget_seconds
        super().__init__(message, endpoint=endpoint, prompt_id=prompt_id)


class WorkflowExecutionError(ComfyUIError):
    """A prompt was accepted but ComfyUI reported that its execution failed."""


class BindingError(ValueError):
    """A requested runtime value cannot be applied to this graph/profile."""
