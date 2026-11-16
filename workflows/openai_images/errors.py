"""Sanitized errors safe for logs and command-line output."""


class ImageError(Exception):
    """A local validation, API, or output error without raw service details."""


class RequestFailure(ImageError):
    """Structured outcome; only explicit rate-limit rejection is retryable."""

    def __init__(
        self,
        message,
        *,
        retryable=False,
        status_code=None,
        retry_after=None,
        category="outcome_unknown",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.category = category


def request_failure(exc):
    # Import lazily so dry-run never requires the SDK. Never log exception bodies,
    # headers, URLs or raw exception text (which can contain prompts/credentials).
    from openai import APIStatusError

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        code = exc.code
        retryable = status == 429 and code == "rate_limit_exceeded"
        delay = None
        if retryable:
            try:
                import math

                delay = float(exc.response.headers.get("retry-after", "nan"))
                if not math.isfinite(delay) or delay < 0:
                    delay = None
            except (ValueError, TypeError):
                delay = None
        category = (
            "rate_limited"
            if retryable
            else ("rejected" if status in {400, 401, 403, 404, 422, 429} else "outcome_unknown")
        )
        return RequestFailure(
            f"Image API HTTP {status}: {category}.",
            retryable=retryable,
            status_code=status,
            retry_after=delay,
            category=category,
        )
    return RequestFailure("Image request outcome is unknown; automatic retry disabled.")
