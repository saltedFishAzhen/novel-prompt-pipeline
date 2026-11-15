"""HTTP transport for a remote ComfyUI server."""

from __future__ import annotations

import json
import mimetypes
import os
import ssl
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .bindings import api_graph
from .errors import ComfyUIError, ComfyUITimeoutError, WorkflowExecutionError
from .validation import is_relative_to, safe_filename, validate_prompt_id, validate_remote_path

_ERROR_BODY_LIMIT = 4000
_DOWNLOAD_CHUNK = 65536
#: Upper bound on one downloaded output; a runaway server cannot fill the disk.
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024**3
_RETRY_BACKOFF = 0.5
_SUCCESS_STATES = frozenset({"success", "completed", "complete"})
_FAILURE_STATES = frozenset({"error", "failed", "failure"})
#: Storage buckets ComfyUI's ``/view`` endpoint accepts.
_STORAGE_TYPES = frozenset({"output", "input", "temp"})
_OUTPUT_KINDS = ("images", "videos", "gifs", "audio")


@dataclass(frozen=True)
class OutputFile:
    """One downloaded output, keeping the node and remote identity it came from.

    ``filename`` and ``subfolder`` are the raw server-side values (the identity
    the server will serve again); ``path`` is the sanitized local file and
    ``relative_name`` is a sanitized relative name. ``storage_type`` is
    ComfyUI's bucket (``output``/``input``/``temp``), not a MIME type.
    """

    node_id: str
    kind: str
    filename: str
    subfolder: str
    storage_type: str
    path: Path

    @property
    def relative_name(self) -> str:
        """Sanitized ``subfolder/filename``; safe to join onto a directory."""
        return "/".join(
            part
            for part in (
                _safe_relative_name(self.subfolder),
                _safe_relative_name(self.filename),
            )
            if part
        )

    def __str__(self) -> str:
        return str(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "filename": self.filename,
            "subfolder": self.subfolder,
            "type": self.storage_type,
            "storage_type": self.storage_type,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class RunResult:
    """Submitted prompt, history record and downloaded outputs of one run."""

    prompt_id: str
    history: dict[str, Any]
    outputs: tuple[OutputFile, ...]

    @property
    def files(self) -> list[str]:
        return [str(output.path) for output in self.outputs]

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "files": self.files,
            "outputs": [output.to_dict() for output in self.outputs],
        }


class _RejectedOutput(ComfyUIError):
    """A downloaded output the client refused locally; retrying cannot help."""


@dataclass(frozen=True)
class _Request:
    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None = None
    data: dict[str, str] | None = None
    files: dict[str, Any] | None = None
    timeout: float = 30.0


class ComfyUIClient:
    """HTTP client for ComfyUI's prompt, history, queue, upload and view endpoints.

    ``base_url`` is the ComfyUI origin, for example ``http://localhost:8188``.
    Set ``COMFYUI_API_KEY`` or pass ``api_key`` when the remote server expects
    bearer authentication.

    ``timeout`` bounds one HTTP request. ``job_timeout`` bounds how long
    :meth:`wait` and :meth:`run` wait for a submitted prompt to finish; keep it
    well above the expected generation time. Prompt submission is never
    automatically retried, so a network error cannot queue the same job twice.

    ``download_retries`` retries a failed ``/view`` download (network errors and
    5xx only). ``max_output_bytes`` refuses a single output larger than that and
    ``output_kinds`` selects which history output buckets are downloaded.
    ``verify`` is passed to :class:`httpx.Client` for TLS configuration.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
        *,
        job_timeout: float = 1800.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool | str | ssl.SSLContext = True,
        download_retries: int = 2,
        max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
        output_kinds: Sequence[str] = _OUTPUT_KINDS,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        origin = base_url.strip().rstrip("/")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) origin, e.g. http://host:8188")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        for name, value in (
            ("timeout", timeout),
            ("poll_interval", poll_interval),
            ("job_timeout", job_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if isinstance(download_retries, bool) or not isinstance(download_retries, int):
            raise ValueError("download_retries must be a non-negative integer")
        if download_retries < 0:
            raise ValueError("download_retries must be a non-negative integer")
        if max_output_bytes is not None and (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer or None")
        kinds = _validate_output_kinds(output_kinds)
        if client is not None and transport is not None:
            raise ValueError("Provide either client or transport, not both")
        self.base_url = origin
        self.api_key = api_key or os.environ.get("COMFYUI_API_KEY")
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.job_timeout = float(job_timeout)
        self.download_retries = download_retries
        self.max_output_bytes = max_output_bytes
        self.output_kinds = kinds
        self._owns_client = client is None
        self._http = (
            client
            if client is not None
            else httpx.Client(
                timeout=self.timeout,
                transport=transport,
                follow_redirects=False,
                verify=verify,
            )
        )
        self._closed = False

    def __enter__(self) -> ComfyUIClient:
        if self._closed:
            raise RuntimeError("ComfyUI client is closed")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned connection pool; an injected client remains caller-owned."""
        if not self._closed:
            if self._owns_client:
                self._http.close()
            self._closed = True

    # ---------------------------------------------------------------- requests

    def _prepare(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
        accept: str = "application/json",
    ) -> _Request:
        if self._closed:
            raise RuntimeError("ComfyUI client is closed")
        headers = {"Accept": accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if content_type:
            headers["Content-Type"] = content_type
        return _Request(
            method=method,
            url=f"{self.base_url}/{path.lstrip('/')}",
            headers=headers,
            content=body,
            data=data,
            files=files,
            timeout=self.timeout if timeout is None else float(timeout),
        )

    @contextmanager
    def _open(
        self, method: str, path: str, *, prompt_id: str | None = None, **kwargs: Any
    ) -> Iterator[httpx.Response]:
        """Stream one request through the shared header/status/error pipeline."""
        request = self._prepare(method, path, **kwargs)
        try:
            with self._http.stream(
                request.method,
                request.url,
                content=request.content,
                data=request.data,
                files=request.files,
                headers=request.headers,
                timeout=request.timeout,
                follow_redirects=False,
            ) as response:
                self._check_status(response, path, prompt_id)
                yield response
        except httpx.RequestError as exc:
            raise self._network_error(exc, path, prompt_id) from None

    def _request(
        self,
        method: str,
        path: str,
        *,
        prompt_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # Stream even the JSON calls: ``_check_status`` then reads at most one
        # bounded chunk of an error body instead of buffering all of it.
        with self._open(method, path, prompt_id=prompt_id, **kwargs) as response:
            content = response.read()
        if not content:
            return {}
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIError(
                "ComfyUI returned invalid JSON",
                endpoint=self._redact(path),
                prompt_id=prompt_id,
                payload=_clean_text(self._redact(content[:1000].decode("utf-8", errors="replace"))),
            ) from exc

    def _redact(self, value: Any) -> Any:
        secret = self.api_key
        if not isinstance(secret, str) or not secret:
            return value
        if isinstance(value, str):
            return value.replace(secret, "[REDACTED]")
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact(item) for item in value)
        return value

    def _network_error(
        self, exc: httpx.RequestError, path: str, prompt_id: str | None
    ) -> ComfyUIError:
        reason = "timed out" if isinstance(exc, httpx.TimeoutException) else "could not be reached"
        return ComfyUIError(
            f"ComfyUI request {reason} ({type(exc).__name__})",
            endpoint=self._redact(path),
            prompt_id=prompt_id,
        )

    def _check_status(self, response: httpx.Response, path: str, prompt_id: str | None) -> None:
        if 200 <= response.status_code < 300:
            return
        # Read at most one bounded chunk for errors; the server controls its body.
        if response.is_stream_consumed:
            raw = response.content[:_ERROR_BODY_LIMIT]
        else:
            raw = next(response.iter_bytes(chunk_size=_ERROR_BODY_LIMIT), b"")[:_ERROR_BODY_LIMIT]
        detail = _clean_text(self._redact(raw.decode("utf-8", errors="replace")))
        raise ComfyUIError(
            f"ComfyUI request failed: {detail or self._redact(response.reason_phrase)}",
            endpoint=self._redact(path),
            status=response.status_code,
            prompt_id=prompt_id,
            payload=detail,
        )

    # ------------------------------------------------------------- inspection

    def health(self) -> dict[str, Any]:
        """Probe the server with ``/system_stats``, falling back to legacy ``/prompt``."""
        try:
            stats = self._request("GET", "/system_stats")
        except ComfyUIError as exc:
            if exc.status not in {404, 405}:
                raise
        else:
            if isinstance(stats, dict):
                return {"system_stats": stats}
        legacy = self._request("GET", "/prompt")
        if not isinstance(legacy, dict):
            raise ComfyUIError(
                "Unexpected health response", endpoint="/prompt", payload=self._redact(legacy)
            )
        return {"prompt": legacy}

    def queue(self) -> dict[str, Any]:
        """Return the server's running and pending queue entries."""
        result = self._request("GET", "/queue")
        if not isinstance(result, dict):
            raise ComfyUIError(
                "Unexpected queue response", endpoint="/queue", payload=self._redact(result)
            )
        return result

    def object_info(self, node_class: str | None = None) -> dict[str, Any]:
        """Return installed node definitions, optionally for one node class."""
        path = "/object_info"
        if node_class:
            path += "/" + quote(node_class, safe="")
        result = self._request("GET", path)
        if not isinstance(result, dict):
            raise ComfyUIError(
                "Unexpected object_info response", endpoint=path, payload=self._redact(result)
            )
        return result

    # ------------------------------------------------------------------- jobs

    def submit(self, graph: dict[str, Any], client_id: str | None = None) -> str:
        """Queue one API-format graph and return its prompt id."""
        clean_graph = api_graph(graph)
        payload: dict[str, Any] = {"prompt": clean_graph}
        if client_id is not None:
            payload["client_id"] = client_id
        response = self._request(
            "POST",
            "/prompt",
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        if not isinstance(response, dict):
            raise ComfyUIError(
                "Unexpected prompt response", endpoint="/prompt", payload=self._redact(response)
            )
        prompt_id = response.get("prompt_id")
        node_errors = response.get("node_errors")
        if node_errors:
            # Some versions queue the prompt and still report node errors.
            self._discard_queued(prompt_id)
            raise ComfyUIError(
                "ComfyUI rejected the workflow graph",
                endpoint="/prompt",
                payload=self._redact(node_errors),
            )
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIError(
                "ComfyUI did not return a prompt_id",
                endpoint="/prompt",
                payload=self._redact(response),
            )
        try:
            return validate_prompt_id(prompt_id)
        except ValueError as exc:
            raise ComfyUIError("ComfyUI returned an unsafe prompt_id", endpoint="/prompt") from exc

    def history(self, prompt_id: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Fetch one history record, or ``None`` while it is not recorded yet."""
        validate_prompt_id(prompt_id)
        response = self._request(
            "GET", f"/history/{prompt_id}", prompt_id=prompt_id, timeout=timeout
        )
        if not isinstance(response, dict):
            raise ComfyUIError(
                "Unexpected history response",
                endpoint=f"/history/{prompt_id}",
                prompt_id=prompt_id,
            )
        entry = response.get(prompt_id)
        if entry is None:
            # Some compatible servers return the history record directly.
            entry = response if "status" in response or "outputs" in response else None
        if entry is not None and not isinstance(entry, dict):
            raise ComfyUIError(
                "Malformed history record",
                endpoint=f"/history/{prompt_id}",
                prompt_id=prompt_id,
            )
        return entry

    def wait(self, prompt_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll until the job finishes, fails, or the wait budget expires.

        The budget defaults to ``job_timeout`` and is independent of the HTTP
        request timeout. On expiry a :class:`ComfyUITimeoutError` is raised;
        the job keeps running on the server, so ``wait`` may be called again or
        ``cancel`` may drop it.
        """
        validate_prompt_id(prompt_id)
        wait_limit = self.job_timeout if timeout is None else float(timeout)
        if wait_limit <= 0:
            raise ValueError("timeout must be positive")
        endpoint = f"/history/{prompt_id}"
        started = time.monotonic()
        deadline = started + wait_limit
        last_error: ComfyUIError | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._wait_timeout(prompt_id, wait_limit, started, endpoint, last_error)
            try:
                entry = self.history(prompt_id, timeout=min(self.timeout, remaining))
            except ComfyUIError as exc:
                # A 4xx will not fix itself; retrying would only hide the cause.
                if exc.status is not None and 400 <= exc.status < 500:
                    raise
                last_error = exc
                entry = None
            if entry is not None and self._execution_state(entry, endpoint, prompt_id) == "done":
                return entry
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._wait_timeout(prompt_id, wait_limit, started, endpoint, last_error)
            time.sleep(min(self.poll_interval, remaining))

    def _wait_timeout(
        self,
        prompt_id: str,
        wait_limit: float,
        started: float,
        endpoint: str,
        last_error: ComfyUIError | None,
    ) -> ComfyUITimeoutError:
        elapsed = time.monotonic() - started
        message = f"Timed out waiting after {elapsed:.1f}s (budget {wait_limit:g}s)"
        if last_error is not None:
            message += f"; last poll failed: {last_error}"
        return ComfyUITimeoutError(
            message,
            wait_seconds=elapsed,
            budget_seconds=wait_limit,
            endpoint=endpoint,
            prompt_id=prompt_id,
        )

    def _execution_state(self, entry: Mapping[str, Any], endpoint: str, prompt_id: str) -> str:
        status = entry.get("status")
        if status is None:
            # Compatible servers may return only outputs once the job is done.
            return "done" if entry.get("outputs") is not None else "running"
        if not isinstance(status, Mapping):
            raise WorkflowExecutionError(
                "Malformed execution status",
                endpoint=endpoint,
                prompt_id=prompt_id,
                payload=status,
            )
        name = str(status.get("status_str", "")).strip().lower()
        if name in _FAILURE_STATES:
            messages = status.get("messages", [])
            raise WorkflowExecutionError(
                "ComfyUI workflow execution failed",
                endpoint=endpoint,
                prompt_id=prompt_id,
                payload=self._redact(messages or dict(status)),
            )
        completed = status.get("completed")
        if completed is True or (completed is not False and name in _SUCCESS_STATES):
            return "done"
        return "running"

    def interrupt(self) -> dict[str, Any]:
        """Ask ComfyUI to stop the prompt that is currently executing.

        The endpoint is server-wide: it interrupts whatever is running right
        now, not a named prompt. :meth:`cancel` checks the queue first, but the
        server can still finish and start another prompt in between.
        """
        result = self._request("POST", "/interrupt", body=b"{}", content_type="application/json")
        return result if isinstance(result, dict) else {}

    def delete_from_queue(self, prompt_id: str) -> None:
        """Remove one pending prompt from the server queue."""
        validate_prompt_id(prompt_id)
        self._request(
            "POST",
            "/queue",
            body=json.dumps({"delete": [prompt_id]}).encode("utf-8"),
            content_type="application/json",
            prompt_id=prompt_id,
        )

    def _discard_queued(self, prompt_id: Any) -> None:
        """Best-effort cleanup for a prompt the server may have queued anyway."""
        try:
            safe_id = validate_prompt_id(prompt_id)
        except ValueError:
            return
        try:
            self.delete_from_queue(safe_id)
        except ComfyUIError:
            pass

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        """Drop ``prompt_id`` from the queue and interrupt it when it is running.

        Cancelling an unknown or already finished prompt is a no-op, which keeps
        the call safe to retry. ``interrupted`` reports that ComfyUI was asked to
        stop; because ``/interrupt`` is server-wide, a prompt that finished
        between the queue read and that call means another job was interrupted
        instead. Check :meth:`queue` yourself when that race matters.
        """
        validate_prompt_id(prompt_id)
        queued = self.queue()
        pending = _queue_contains(queued.get("queue_pending"), prompt_id)
        running = _queue_contains(queued.get("queue_running"), prompt_id)
        if pending:
            self.delete_from_queue(prompt_id)
        interrupted = False
        if running:
            self.interrupt()
            interrupted = True
        return {"prompt_id": prompt_id, "interrupted": interrupted, "deleted_pending": pending}

    # ------------------------------------------------------------------ media

    def upload_file(
        self,
        path: str | os.PathLike[str],
        *,
        subfolder: str = "",
        overwrite: bool = False,
    ) -> str:
        """Upload an image, audio file or other media into ComfyUI input storage.

        ComfyUI currently accepts multipart uploads at ``/upload/image`` using
        the form field named ``image``. The server stores bytes under ``input``;
        loaders such as the video/audio node pack consume the returned filename.
        The returned name is input-relative and safe to pass back as
        ``first_frame``/``reference_images``.
        """
        source_path = Path(path).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"upload path is not a file: {source_path}")
        fields = {
            "type": "input",
            "subfolder": validate_remote_path(subfolder, "subfolder", allow_empty=True),
            "overwrite": "true" if overwrite else "false",
        }
        content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        with source_path.open("rb") as source:
            response = self._request(
                "POST",
                "/upload/image",
                data=fields,
                files={"image": (source_path.name, source, content_type)},
            )
        if not isinstance(response, dict) or not isinstance(response.get("name"), str):
            raise ComfyUIError(
                "Unexpected upload response",
                endpoint="/upload/image",
                payload=self._redact(response),
            )
        if response.get("type", "input") != "input":
            raise ComfyUIError("Upload did not return an input file", endpoint="/upload/image")
        try:
            name = validate_remote_path(response["name"], "name", single_component=True)
            returned_subfolder = response.get("subfolder")
            if returned_subfolder is None:
                returned_subfolder = fields["subfolder"]
            returned_subfolder = validate_remote_path(
                returned_subfolder, "subfolder", allow_empty=True
            )
        except ValueError as exc:
            raise ComfyUIError("Unsafe upload response path", endpoint="/upload/image") from exc
        return "/".join(part for part in (returned_subfolder, name) if part)

    def download_outputs(
        self,
        history: Mapping[str, Any],
        output_dir: str | os.PathLike[str],
        *,
        prompt_id: str | None = None,
        subdir: str | None = None,
    ) -> list[OutputFile]:
        """Download the outputs referenced by a history entry.

        ``subdir`` writes below ``output_dir/<subdir>``; pass the prompt id when
        several prompts share one output directory so they cannot collide.
        Existing files are never overwritten - a numeric suffix is added - and an
        entry that cannot be downloaded is reported instead of being skipped
        silently.
        """
        outputs = history.get("outputs", {})
        if not isinstance(outputs, Mapping):
            raise ValueError("history.outputs must be an object")
        root = Path(output_dir).expanduser().resolve()
        output_root = root
        if subdir:
            output_root = (root / validate_remote_path(subdir, "output subdir")).resolve()
            if not is_relative_to(output_root, root):
                raise ValueError(f"output subdir escapes {root}")
        output_root.mkdir(parents=True, exist_ok=True)
        results: list[OutputFile] = []
        seen: set[tuple[str, str, str]] = set()
        used: set[Path] = set()
        rejected: list[str] = []
        for raw_node_id, node_output in outputs.items():
            if not isinstance(node_output, Mapping):
                rejected.append(f"node {raw_node_id}: outputs is not an object")
                continue
            node_id = safe_filename(raw_node_id, fallback="node")
            for kind in self.output_kinds:
                files = node_output.get(kind, [])
                if not isinstance(files, list):
                    rejected.append(f"node {raw_node_id} {kind}: not a list")
                    continue
                for index, item in enumerate(files):
                    output = self._download_one(
                        item,
                        kind=kind,
                        node_id=node_id,
                        output_root=output_root,
                        seen=seen,
                        used=used,
                        prompt_id=prompt_id,
                    )
                    if output is None:
                        rejected.append(f"node {raw_node_id} {kind}[{index}]: unusable entry")
                    else:
                        results.append(output)
        if rejected and not results:
            raise ComfyUIError(
                "No downloadable outputs in the history record",
                endpoint="/view",
                prompt_id=prompt_id,
                payload=self._redact(rejected[:10]),
            )
        return results

    def _download_one(
        self,
        item: Any,
        *,
        kind: str,
        node_id: str,
        output_root: Path,
        seen: set[tuple[str, str, str]],
        used: set[Path],
        prompt_id: str | None,
    ) -> OutputFile | None:
        if not isinstance(item, Mapping):
            return None
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            return None
        subfolder = str(item.get("subfolder") or "")
        storage_type = str(item.get("type") or "output")
        if storage_type not in _STORAGE_TYPES:
            # ``type`` becomes a query parameter; anything else would either be
            # rejected by ComfyUI or aim at a bucket we do not expect.
            return None
        identity = (filename, subfolder, storage_type)
        if identity in seen:
            return None
        seen.add(identity)
        query = urlencode({"filename": filename, "subfolder": subfolder, "type": storage_type})
        endpoint = f"/view?{query}"
        destination = _unique_destination(output_root, node_id, safe_filename(filename), used)
        self._download(endpoint, destination, prompt_id=prompt_id)
        used.add(destination)
        return OutputFile(
            node_id=node_id,
            kind=kind,
            filename=filename,
            subfolder=subfolder,
            storage_type=storage_type,
            path=destination,
        )

    def _download(self, endpoint: str, destination: Path, *, prompt_id: str | None) -> None:
        """Fetch one output, retrying transient failures and bounding its size."""
        attempts = self.download_retries + 1
        for attempt in range(attempts):
            try:
                self._download_once(endpoint, destination, prompt_id=prompt_id)
                return
            except _RejectedOutput:
                raise
            except ComfyUIError as exc:
                transient = exc.status is None or exc.status >= 500
                if not transient or attempt + 1 >= attempts:
                    raise
                time.sleep(_RETRY_BACKOFF * (attempt + 1))

    def _download_once(self, endpoint: str, destination: Path, *, prompt_id: str | None) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".comfy-", suffix=".tmp", dir=destination.parent
        )
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                with self._open("GET", endpoint, prompt_id=prompt_id, accept="*/*") as response:
                    for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if self.max_output_bytes is not None and written > self.max_output_bytes:
                            raise _RejectedOutput(
                                f"ComfyUI output exceeds {self.max_output_bytes} bytes",
                                endpoint=endpoint,
                                prompt_id=prompt_id,
                            )
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if written == 0:
                # A 200 with no bytes is a broken proxy or a truncated run, not
                # a valid output; fail instead of reporting an empty file.
                raise _RejectedOutput(
                    "ComfyUI returned an empty output file",
                    endpoint=endpoint,
                    prompt_id=prompt_id,
                )
            os.replace(temp_name, destination)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    # -------------------------------------------------------------------- run

    def run(
        self,
        graph: dict[str, Any],
        output_dir: str | os.PathLike[str],
        *,
        client_id: str | None = None,
        timeout: float | None = None,
        subdir: str | None = None,
    ) -> RunResult:
        """Submit a graph, wait for completion and download its outputs.

        Outputs go to ``output_dir/<prompt_id>/`` unless ``subdir`` overrides
        that; pass ``subdir=""`` to write straight into ``output_dir``.
        """
        prompt_id = self.submit(graph, client_id=client_id)
        history = self.wait(prompt_id, timeout=timeout)
        outputs = self.download_outputs(
            history,
            output_dir,
            prompt_id=prompt_id,
            subdir=prompt_id if subdir is None else subdir,
        )
        return RunResult(prompt_id=prompt_id, history=history, outputs=tuple(outputs))


def _clean_text(value: str) -> str:
    """Strip terminal control and bidirectional formatting from server text."""
    return "".join(
        char
        for char in value
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )


def _queue_contains(entries: Any, prompt_id: str) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, (list, tuple)) and len(entry) > 1 and entry[1] == prompt_id:
            return True
    return False


def _validate_output_kinds(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ValueError("output_kinds must be a non-empty sequence of output buckets")
    kinds: list[str] = []
    for kind in value:
        if not isinstance(kind, str) or not kind:
            raise ValueError("output_kinds entries must be non-empty strings")
        kinds.append(kind)
    return tuple(kinds)


def _safe_relative_name(value: str) -> str:
    """Sanitize every segment of a server-supplied relative name."""
    segments = []
    for segment in value.replace("\\", "/").split("/"):
        clean = safe_filename(segment, fallback="")
        if clean:
            segments.append(clean)
    return "/".join(segments)


def _unique_destination(root: Path, node_id: str, name: str, used: set[Path]) -> Path:
    """Never let two outputs - in this run or from an earlier one - share a path."""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = root / f"{node_id}_{name}"
    index = 1
    while candidate in used or candidate.exists():
        candidate = root / f"{node_id}_{stem}-{index}{suffix}"
        index += 1
    if not is_relative_to(candidate.resolve(), root):
        raise ValueError(f"unsafe output path: {candidate}")
    return candidate
