"""Synchronous image generation/editing; no network unless execute=True."""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import json
import math
import os
import re
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ImageError, RequestFailure, request_failure

BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2.6-sunburst"
MAX_INPUT_BYTES = 50 * 1024 * 1024
FORMATS = {"png", "jpeg", "webp"}


def image_format(header: bytes) -> str | None:
    """Recognize container signatures, not a full image decoder."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    operation: str = "generate"
    images: tuple[Path, ...] = ()
    model: str = DEFAULT_MODEL
    size: str = "auto"
    quality: str = "auto"
    output_format: str = "png"
    n: int = 1

    def validate(self) -> None:
        if self.operation not in {"generate", "edit"}:
            raise ImageError("Operation must be generate or edit.")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ImageError("A nonempty prompt is required.")
        if not re.fullmatch(r"gpt-image-[A-Za-z0-9._-]+", self.model):
            raise ImageError("Use a GPT Image model name; legacy DALL-E is not supported.")
        if self.output_format not in FORMATS:
            raise ImageError("Output format must be png, jpeg, or webp.")
        if self.quality not in {"auto", "low", "medium", "high", "xhigh", "max"}:
            raise ImageError("Unsupported quality.")
        if self.model in {"gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2"}:
            if self.quality in {"xhigh", "max"}:
                raise ImageError("This model supports quality up to high.")
        if type(self.n) is not int or not 1 <= self.n <= 10:
            raise ImageError("n must be between 1 and 10.")
        if self.model in {"gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5"}:
            if self.size not in {"auto", "1024x1024", "1536x1024", "1024x1536"}:
                raise ImageError("GPT Image 1.x requires a standard supported size.")
        if self.size != "auto":
            match = re.fullmatch(r"([1-9][0-9]{0,3})x([1-9][0-9]{0,3})", self.size)
            if not match:
                raise ImageError("Size must be auto or WIDTHxHEIGHT.")
            w, h = map(int, match.groups())
            if w % 16 or h % 16 or max(w, h) > 3840 or not 1 / 3 <= w / h <= 3:
                raise ImageError("Size edges must be multiples of 16, <=3840, ratio 1:3 to 3:1.")
            if not 655360 <= w * h <= 8294400:
                raise ImageError("Size must contain 655360 to 8294400 pixels.")
        if self.operation == "generate" and self.images:
            raise ImageError("Reference images require the edit command.")
        if self.operation == "edit" and not 1 <= len(self.images) <= 16:
            raise ImageError("Edit requires 1 to 16 reference images.")
        for path in self.images:
            try:
                if not path.is_file() or not 0 < path.stat().st_size < MAX_INPUT_BYTES:
                    raise ImageError("Reference must be a regular image file smaller than 50 MiB.")
                with path.open("rb") as stream:
                    kind = image_format(stream.read(12))
                expected = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
                if kind is None or expected.get(path.suffix.lower()) != kind:
                    raise ImageError(
                        "Reference must have matching PNG/JPEG/WebP signature and suffix."
                    )
            except OSError:
                raise ImageError("Cannot read a reference image.") from None

    def summary(self) -> dict[str, Any]:
        # Do not persist user prompt, local reference paths, or any credential.
        return {
            "operation": self.operation,
            "model": self.model,
            "size": self.size,
            "quality": self.quality,
            "output_format": self.output_format,
            "n": self.n,
            "reference_count": len(self.images),
            "prompt_sha256": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
        }


def local_check() -> dict[str, Any]:
    return {
        "status": "local_only",
        "base_url": BASE_URL,
        "sdk_installed": importlib.util.find_spec("openai") is not None,
        "api_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "credentials_verified": False,
        "network_requested": False,
    }


def _create_sdk(api_key: str, timeout: float) -> Any:
    try:
        from openai import OpenAI
    except ImportError:
        raise ImageError("Install the optional openai SDK before using --execute.") from None
    import httpx

    # Prevent environment endpoint/proxy overrides and automatic redirect forwarding.
    transport = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)
    try:
        return OpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            max_retries=0,
            timeout=timeout,
            http_client=transport,
        )
    except Exception:
        transport.close()
        raise ImageError("Could not initialize the official OpenAI client.") from None


class OpenAIImagesClient:
    def __init__(self, *, request_timeout: float = 300.0):
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ImageError("Request timeout must be finite and greater than zero.")
        self.request_timeout = request_timeout

    def run(
        self,
        request: ImageRequest,
        output_dir: str | Path = "runs/openai-images",
        *,
        execute: bool = False,
    ) -> dict[str, Any]:
        request.validate()
        summary = request.summary()
        if not execute:
            return {"status": "dry_run", "network_requested": False, **summary}
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ImageError("Set OPENAI_API_KEY before using --execute.")
        client = _create_sdk(key, self.request_timeout)
        run_dir = None
        record = {"status": "pending", **summary, "outputs": []}
        try:
            root = Path(output_dir).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            run_dir = Path(tempfile.mkdtemp(prefix="image-", dir=root))
            self._record(run_dir, record)
            kwargs = {
                "prompt": request.prompt,
                "model": request.model,
                "size": request.size,
                "quality": request.quality,
                "output_format": request.output_format,
                "n": request.n,
            }
            with ExitStack() as stack:
                if request.operation == "edit":
                    kwargs["image"] = [stack.enter_context(p.open("rb")) for p in request.images]
                try:
                    if request.operation == "edit":
                        response = client.images.edit(**kwargs)
                    else:
                        response = client.images.generate(**kwargs)
                except Exception as exc:
                    raise request_failure(exc) from None
            data = getattr(response, "data", None)
            if not data or len(data) != request.n:
                raise ImageError("API returned an unexpected image count; no automatic retry.")
            decoded = []
            for item in data:
                try:
                    raw = base64.b64decode(item.b64_json, validate=True)
                except (ValueError, TypeError, AttributeError, binascii.Error):
                    raise ImageError("API returned invalid base64 image data.") from None
                if image_format(raw[:12]) != request.output_format:
                    raise ImageError("API image signature does not match the requested format.")
                decoded.append(raw)
            for i, raw in enumerate(decoded, 1):
                path = run_dir / f"image-{i:02d}.{request.output_format}"
                with path.open("xb") as stream:
                    stream.write(raw)
                record["outputs"].append(
                    {
                        "file": path.name,
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            record["status"] = "completed"
            self._record(run_dir, record)
            return {**record, "run_dir": str(run_dir)}
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ImageError)
                else (
                    "Image request or output save failed. It may have been billed; "
                    "no automatic retry was made. Check the local run record before retrying."
                )
            )
            if run_dir is not None:
                record.update(status="failed", error=message)
                try:
                    self._record(run_dir, record)
                except OSError:
                    pass
            if isinstance(exc, RequestFailure):
                raise exc from None
            raise ImageError(message) from None
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _record(directory: Path, record: dict[str, Any]) -> None:
        # Only our newly reserved run directory is ever updated.
        (directory / "run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
