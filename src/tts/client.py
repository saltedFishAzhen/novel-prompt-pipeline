"""Client for the Breeze TTS 2 FastAPI streaming speech endpoint.

The upstream service returns raw, mono s16le PCM, not a WAV file. This client
checks its response metadata and publishes a WAV container without clobbering
an existing result. No synthesis request is automatically repeated.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import TTSConfig

_SAMPLE_RATE = 24_000
_CHANNELS = 1
_SAMPLE_WIDTH = 2
_MAX_METADATA_BYTES = 1024 * 1024


class TTSError(RuntimeError):
    """Sanitized service error, with busy/loading statuses marked retryable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        self.status_code = status
        self.retryable = status in {409, 503}
        super().__init__(message if status is None else f"{message} (HTTP {status})")


def _reject_links(path: Path) -> Path:
    """Check every supplied path component before normalizing the final path."""
    expanded = path.expanduser().absolute()
    for component in (expanded, *expanded.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("TTS paths must not contain symbolic links or junctions")
    return Path(os.path.abspath(expanded))


def _reference_bytes(path: str | Path, limit: int) -> bytes:
    reference = _reject_links(Path(path))
    if reference.suffix.lower() != ".wav":
        raise ValueError("TTS reference must be a local PCM WAV file")
    try:
        with reference.open("rb") as source:
            data = source.read(limit + 1)
    except OSError as exc:
        raise ValueError("TTS reference WAV cannot be read") from exc
    if len(data) > limit:
        raise ValueError("TTS reference WAV exceeds the configured size limit")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            if source.getcomptype() != "NONE" or not (1 <= channels <= 8 and 1 <= width <= 4):
                raise ValueError("TTS reference must contain uncompressed PCM")
            if rate <= 0 or frames <= 0:
                raise ValueError("TTS reference WAV must contain audio")
            if len(source.readframes(frames)) != frames * channels * width:
                raise ValueError("TTS reference WAV is truncated")
    except (EOFError, wave.Error) as exc:
        raise ValueError("TTS reference must be a readable PCM WAV file") from exc
    return data


def _publish_new(temp: Path, output: Path) -> None:
    """Publish atomically without replacing another file, on POSIX or Windows."""
    _reject_links(output)
    try:
        if os.name == "nt":
            # Windows rename is atomic and raises if the destination exists.
            os.rename(temp, output)
        else:
            # link() atomically creates a new name and never replaces an existing file.
            os.link(temp, output)
            temp.unlink()
    except FileExistsError as exc:
        raise FileExistsError("TTS output already exists; choose a new output file") from exc


class BreezeTTSClient:
    """Synchronous, bounded client for one independently hosted Breeze endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 180.0,
        max_audio_bytes: int = 64 * 1024 * 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            not isinstance(base_url, str)
            or not base_url.strip()
            or any(character in base_url for character in "\r\n\t")
        ):
            raise ValueError("Breeze TTS base URL is required")
        try:
            parsed = urlsplit(base_url.strip())
            # Validate the port and HTTPX's URL parser without exposing the URL.
            _ = parsed.port
            httpx.URL(base_url.strip())
        except (ValueError, httpx.InvalidURL):
            raise ValueError("Breeze TTS base URL is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Breeze TTS base URL must be an HTTP(S) URL without credentials")
        if api_key is not None and (
            not isinstance(api_key, str) or "\n" in api_key or "\r" in api_key
        ):
            raise ValueError("Breeze TTS API key is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("TTS timeout must be a positive finite number")
        if (
            isinstance(max_audio_bytes, bool)
            or not isinstance(max_audio_bytes, int)
            or not 0 < max_audio_bytes <= 1024 * 1024 * 1024
        ):
            raise ValueError(
                "TTS maximum audio size must be a positive integer no greater than 1 GiB"
            )
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key
        self.max_audio_bytes = max_audio_bytes
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self._closed = False

    @classmethod
    def from_config(
        cls, config: TTSConfig, *, transport: httpx.BaseTransport | None = None
    ) -> BreezeTTSClient:
        base_url = os.getenv(config.url_env, "")
        if not base_url:
            raise ValueError(f"Set {config.url_env} to the Breeze TTS service URL")
        return cls(
            base_url,
            api_key=os.getenv(config.api_key_env) or None,
            timeout=config.timeout,
            max_audio_bytes=config.max_audio_bytes,
            transport=transport,
        )

    def __enter__(self) -> BreezeTTSClient:
        if self._closed:
            raise RuntimeError("TTS client is closed")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._http.close()
            self._closed = True

    def _headers(self) -> dict[str, str]:
        if self._closed:
            raise RuntimeError("TTS client is closed")
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    @staticmethod
    def _status(response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise TTSError("Breeze TTS service returned an error", status=response.status_code)

    def _metadata(self, path: str) -> dict:
        try:
            with self._http.stream(
                "GET", self._base_url + path, headers=self._headers()
            ) as response:
                self._status(response)
                body = bytearray()
                for part in response.iter_bytes(chunk_size=65536):
                    body.extend(part)
                    if len(body) > _MAX_METADATA_BYTES:
                        raise TTSError("Breeze TTS metadata response is too large")
            value = json.loads(body)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise TTSError("Breeze TTS request failed") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TTSError("Breeze TTS metadata response is not valid JSON") from None
        if not isinstance(value, dict):
            raise TTSError("Breeze TTS metadata response must be a JSON object")
        return value

    def health(self) -> dict:
        value = self._metadata("/health")
        if value.get("status") != "ok" or value.get("sample_rate") != _SAMPLE_RATE:
            raise TTSError("Breeze TTS health response is invalid or not ready")
        return {"status": "ok", "sample_rate": _SAMPLE_RATE}

    def model_info(self) -> dict:
        value = self._metadata("/v1/model")
        digest = value.get("model_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise TTSError("Breeze TTS model identity response is invalid")
        result = {"model_digest": digest}
        for key in ("frame_rate", "max_new_tokens", "max_batch_texts"):
            if key in value:
                number = value[key]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(number)
                    or number <= 0
                ):
                    raise TTSError("Breeze TTS model identity response is invalid")
                result[key] = number
        return result

    def synthesize(
        self,
        text: str,
        output: str | Path,
        *,
        instruction: str = "",
        reference_audio: str | Path | None = None,
        reference_text: str = "",
        cfg_scale: float = 1.0,
        seed: int = 42,
    ) -> dict:
        """Generate voice design, clone, or direction audio and save new WAV bytes."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text must be nonempty")
        if not isinstance(instruction, str) or not isinstance(reference_text, str):
            raise ValueError("TTS instruction and reference text must be strings")
        if bool(reference_audio) != bool(reference_text.strip()):
            raise ValueError("Reference audio and its full transcript must be supplied together")
        if not instruction.strip() and not reference_audio:
            raise ValueError("Provide a voice instruction or reference audio and transcript")
        if (
            isinstance(cfg_scale, bool)
            or not isinstance(cfg_scale, (int, float))
            or not math.isfinite(cfg_scale)
            or cfg_scale <= 0
        ):
            raise ValueError("TTS cfg_scale must be a positive finite number")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
            raise ValueError("TTS seed must be an integer between 0 and 2**32 - 1")
        reference = (
            _reference_bytes(reference_audio, self.max_audio_bytes) if reference_audio else None
        )
        target = _reject_links(Path(output))
        if target.suffix.lower() != ".wav":
            raise ValueError("TTS output must use the .wav extension")
        if target.exists():
            raise FileExistsError("TTS output already exists; choose a new output file")
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_links(target)
        parts = [
            ("text", (None, text)),
            ("instruction", (None, instruction)),
            ("cfg_scale", (None, str(cfg_scale))),
            ("ref_text", (None, reference_text)),
            ("seed", (None, str(seed))),
        ]
        if reference is not None:
            parts.append(("ref_audio", ("reference.wav", reference, "audio/wav")))
        mode = (
            "direction"
            if reference is not None and instruction.strip()
            else "clone"
            if reference is not None
            else "design"
        )
        fd, temporary_name = tempfile.mkstemp(prefix=".tts-", suffix=".wav", dir=target.parent)
        temporary = Path(temporary_name)
        pcm_bytes = 0
        try:
            with os.fdopen(fd, "w+b") as output_file:
                try:
                    with self._http.stream(
                        "POST",
                        self._base_url + "/v1/audio/speech",
                        headers=self._headers(),
                        files=parts,
                    ) as response:
                        self._status(response)
                        content_type = (
                            response.headers.get("Content-Type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if (
                            content_type != "audio/pcm"
                            or response.headers.get("X-Sample-Rate") != str(_SAMPLE_RATE)
                            or response.headers.get("X-Sample-Format", "").lower() != "s16le"
                        ):
                            raise TTSError("Breeze TTS returned unsupported audio metadata")
                        with wave.open(output_file, "wb") as wav:
                            wav.setnchannels(_CHANNELS)
                            wav.setsampwidth(_SAMPLE_WIDTH)
                            wav.setframerate(_SAMPLE_RATE)
                            for part in response.iter_bytes(chunk_size=65536):
                                pcm_bytes += len(part)
                                if pcm_bytes > self.max_audio_bytes:
                                    raise TTSError(
                                        "Breeze TTS audio exceeds the configured size limit"
                                    )
                                wav.writeframesraw(part)
                            if not pcm_bytes or pcm_bytes % _SAMPLE_WIDTH:
                                raise TTSError("Breeze TTS returned empty or incomplete PCM audio")
                except (httpx.HTTPError, httpx.InvalidURL):
                    raise TTSError("Breeze TTS request failed") from None
                output_file.flush()
                os.fsync(output_file.fileno())
            with temporary.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            _publish_new(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(target),
            "sha256": digest,
            "sample_rate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "duration_seconds": pcm_bytes / (_SAMPLE_RATE * _SAMPLE_WIDTH),
            "mode": mode,
            "seed": seed,
            "cfg_scale": cfg_scale,
        }
