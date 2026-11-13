"""HTTPX client for a remote ComfyUI server."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlencode

import httpx


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


class WorkflowExecutionError(ComfyUIError):
    """A prompt was accepted but ComfyUI reported that its execution failed."""


class ComfyUIClient:
    """HTTP client for ComfyUI's prompt, history, upload and view endpoints.

    ``base_url`` is the ComfyUI origin, for example ``http://localhost:8188``.
    Set ``COMFYUI_API_KEY`` or pass ``api_key`` when the remote server expects
    bearer authentication. Prompt submission is never automatically retried.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if client is not None and transport is not None:
            raise ValueError("Provide either client or transport, not both")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("COMFYUI_API_KEY")
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._owns_client = client is None
        self._http = (
            client
            if client is not None
            else httpx.Client(
                timeout=self.timeout,
                transport=transport,
                follow_redirects=False,
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

    def _headers(self, expect_json: bool = True) -> dict[str, str]:
        if self._closed:
            raise RuntimeError("ComfyUI client is closed")
        headers = {"Accept": "application/json" if expect_json else "*/*"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self.api_key, "[REDACTED]") if self.api_key else value
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _check_status(self, response: httpx.Response, path: str, prompt_id: str | None) -> None:
        if 200 <= response.status_code < 300:
            return
        # Read at most one bounded chunk for errors; the server controls its body.
        if response.is_stream_consumed:
            raw = response.content[:4000]
        else:
            raw = next(response.iter_bytes(chunk_size=4000), b"")
        detail = self._redact(raw.decode("utf-8", errors="replace"))
        raise ComfyUIError(
            f"ComfyUI request failed: {detail or self._redact(response.reason_phrase)}",
            endpoint=self._redact(path),
            status=response.status_code,
            prompt_id=prompt_id,
            payload=detail,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        prompt_id: str | None = None,
        expect_json: bool = True,
        timeout: float | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers(expect_json)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self._http.request(
                method,
                url,
                content=body,
                data=data,
                files=files,
                headers=headers,
                timeout=self.timeout if timeout is None else timeout,
                follow_redirects=False,
            )
            self._check_status(response, path, prompt_id)
            content = response.content
            if not expect_json:
                return content
            if not content:
                return {}
            try:
                return json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ComfyUIError(
                    "ComfyUI returned invalid JSON",
                    endpoint=path,
                    prompt_id=prompt_id,
                    payload=self._redact(content[:1000].decode("utf-8", errors="replace")),
                ) from exc
        except httpx.RequestError as exc:
            raise ComfyUIError(
                f"Could not reach ComfyUI ({type(exc).__name__})",
                endpoint=self._redact(path),
                prompt_id=prompt_id,
            ) from None

    def health(self) -> dict[str, Any]:
        """Return the remote queue status and client id."""
        result = self._request("GET", "/prompt")
        if not isinstance(result, dict):
            raise ComfyUIError(
                "Unexpected health response", endpoint="/prompt", payload=self._redact(result)
            )
        return result

    def object_info(self, node_class: str | None = None) -> dict[str, Any]:
        """Return installed node definitions, optionally for one node class."""
        path = "/object_info"
        if node_class:
            from urllib.parse import quote

            path += "/" + quote(node_class, safe="")
        result = self._request("GET", path)
        if not isinstance(result, dict):
            raise ComfyUIError(
                "Unexpected object_info response", endpoint=path, payload=self._redact(result)
            )
        return result

    def submit(self, graph: dict[str, Any], client_id: str | None = None) -> str:
        """Queue one API-format graph and return its prompt id."""
        if not isinstance(graph, dict) or not graph:
            raise ValueError("graph must be a non-empty object")
        clean_graph = _api_graph(graph)
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
        if response.get("node_errors"):
            raise ComfyUIError(
                "ComfyUI rejected the workflow graph",
                endpoint="/prompt",
                payload=self._redact(response["node_errors"]),
            )
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIError(
                "ComfyUI did not return a prompt_id",
                endpoint="/prompt",
                payload=self._redact(response),
            )
        try:
            _validate_prompt_id(prompt_id)
        except ValueError as exc:
            raise ComfyUIError("ComfyUI returned an unsafe prompt_id", endpoint="/prompt") from exc
        return prompt_id

    def history(self, prompt_id: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Fetch one history record, or ``None`` while it is not recorded yet."""
        _validate_prompt_id(prompt_id)
        response = self._request(
            "GET", f"/history/{prompt_id}", prompt_id=prompt_id, timeout=timeout
        )
        if not isinstance(response, dict):
            raise ComfyUIError(
                "Unexpected history response", endpoint=f"/history/{prompt_id}", prompt_id=prompt_id
            )
        entry = response.get(prompt_id)
        if entry is None:
            # Some compatible servers return the history record directly.
            entry = response if "status" in response or "outputs" in response else None
        if entry is not None and not isinstance(entry, dict):
            raise ComfyUIError(
                "Malformed history record", endpoint=f"/history/{prompt_id}", prompt_id=prompt_id
            )
        return entry

    def wait(self, prompt_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll until the job finishes, fails, or the timeout expires."""
        wait_limit = self.timeout if timeout is None else float(timeout)
        if wait_limit <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + wait_limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowExecutionError(
                    f"Timed out waiting after {wait_limit:g}s",
                    endpoint=f"/history/{prompt_id}",
                    prompt_id=prompt_id,
                )
            try:
                entry = self.history(prompt_id, timeout=min(self.timeout, wait_limit, remaining))
            except ComfyUIError:
                if time.monotonic() >= deadline:
                    raise WorkflowExecutionError(
                        f"Timed out waiting after {wait_limit:g}s",
                        endpoint=f"/history/{prompt_id}",
                        prompt_id=prompt_id,
                    ) from None
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowExecutionError(
                    f"Timed out waiting after {wait_limit:g}s",
                    endpoint=f"/history/{prompt_id}",
                    prompt_id=prompt_id,
                )
            if entry is not None:
                status = entry.get("status") or {}
                if not isinstance(status, dict):
                    raise WorkflowExecutionError(
                        "Malformed execution status",
                        endpoint=f"/history/{prompt_id}",
                        prompt_id=prompt_id,
                        payload=status,
                    )
                status_name = str(status.get("status_str", "")).lower()
                if status_name in {"error", "failed", "failure"}:
                    messages = status.get("messages", [])
                    raise WorkflowExecutionError(
                        "ComfyUI workflow execution failed",
                        endpoint=f"/history/{prompt_id}",
                        prompt_id=prompt_id,
                        payload=self._redact(messages or status),
                    )
                if status.get("completed") is True or (
                    status.get("completed") is not False and status_name in {"success", "completed"}
                ):
                    return entry
            time.sleep(min(self.poll_interval, remaining))

    def upload_file(self, path: str | os.PathLike[str], *, subfolder: str = "codex") -> str:
        """Upload an image, audio file or other media into ComfyUI input storage.

        ComfyUI currently accepts multipart uploads at ``/upload/image`` using
        the form field named ``image``. The server stores bytes under ``input``;
        loaders such as the video/audio node pack consume the returned filename.
        """
        image_path = Path(path).expanduser().resolve(strict=True)
        if not image_path.is_file():
            raise ValueError(f"image path is not a file: {image_path}")
        subfolder = _validate_relative_remote_path(subfolder, "subfolder", allow_empty=True)
        fields = {
            "type": "input",
            "subfolder": subfolder,
            "overwrite": "false",
        }
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        with image_path.open("rb") as source:
            response = self._request(
                "POST",
                "/upload/image",
                data=fields,
                files={"image": (image_path.name, source, content_type)},
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
            name = _validate_relative_remote_path(response["name"], "name", single_component=True)
            returned_subfolder = response.get("subfolder")
            if returned_subfolder is None:
                returned_subfolder = fields["subfolder"]
            returned_subfolder = _validate_relative_remote_path(
                returned_subfolder, "subfolder", allow_empty=True
            )
        except ValueError as exc:
            raise ComfyUIError("Unsafe upload response path", endpoint="/upload/image") from exc
        return "/".join(part for part in (returned_subfolder, name) if part)

    def upload_image(self, path: str | os.PathLike[str], *, subfolder: str = "codex") -> str:
        """Compatibility name for uploading an image to ComfyUI input."""
        return self.upload_file(path, subfolder=subfolder)

    def download_outputs(
        self,
        history: dict[str, Any],
        output_dir: str | os.PathLike[str],
        *,
        prompt_id: str | None = None,
    ) -> list[str]:
        """Download image, video and audio outputs referenced by a history entry."""
        output_root = Path(output_dir).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        outputs = history.get("outputs", {})
        if not isinstance(outputs, dict):
            raise ValueError("history.outputs must be an object")
        for node_id, node_output in outputs.items():
            if not isinstance(node_output, dict):
                continue
            for kind in ("images", "videos", "gifs", "audio"):
                files = node_output.get(kind, [])
                if not isinstance(files, list):
                    continue
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename")
                    if not isinstance(filename, str) or not filename:
                        continue
                    subfolder = str(item.get("subfolder") or "")
                    media_type = str(item.get("type") or "output")
                    identity = (filename, subfolder, media_type)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    query = urlencode(
                        {"filename": filename, "subfolder": subfolder, "type": media_type}
                    )
                    safe_name = _safe_filename(filename)
                    destination = output_root / f"{node_id}_{safe_name}"
                    if not _is_relative_to(destination.resolve(), output_root):
                        raise ValueError(f"unsafe output path: {filename}")
                    endpoint = f"/view?{query}"
                    fd, temp_name = tempfile.mkstemp(
                        prefix=".comfy-", suffix=".tmp", dir=output_root
                    )
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            try:
                                with self._http.stream(
                                    "GET",
                                    f"{self.base_url}{endpoint}",
                                    headers=self._headers(False),
                                    timeout=self.timeout,
                                    follow_redirects=False,
                                ) as response:
                                    self._check_status(response, endpoint, prompt_id)
                                    for chunk in response.iter_bytes(chunk_size=65536):
                                        stream.write(chunk)
                            except httpx.RequestError as exc:
                                raise ComfyUIError(
                                    f"Could not reach ComfyUI ({type(exc).__name__})",
                                    endpoint=self._redact(endpoint),
                                    prompt_id=prompt_id,
                                ) from None
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp_name, destination)
                    except Exception:
                        try:
                            os.unlink(temp_name)
                        except FileNotFoundError:
                            pass
                        raise
                    paths.append(str(destination.resolve()))
        return paths

    def run(
        self,
        graph: dict[str, Any],
        output_dir: str | os.PathLike[str],
        *,
        client_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Submit a graph, wait for completion and download its outputs."""
        prompt_id = self.submit(graph, client_id=client_id)
        history = self.wait(prompt_id, timeout=timeout)
        files = self.download_outputs(history, output_dir, prompt_id=prompt_id)
        return {"prompt_id": prompt_id, "history": history, "files": files}


def _api_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Remove UI-only metadata and validate API-format graph nodes."""
    clean: dict[str, Any] = {}
    for node_id, node in graph.items():
        if str(node_id).startswith("_"):
            continue
        if (
            not isinstance(node, dict)
            or "class_type" not in node
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError(f"node {node_id!r} must contain class_type and an inputs object")
        clean[str(node_id)] = node
    if not clean:
        raise ValueError("graph contains no API nodes")
    return clean


def _validate_relative_remote_path(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    single_component: bool = False,
) -> str:
    """Validate a ComfyUI input-relative name returned by an untrusted server."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value and allow_empty:
        return ""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must be a safe relative path")
    if any(char in value for char in "?#") or PureWindowsPath(value).drive:
        raise ValueError(f"{field} must be a safe relative path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if PurePosixPath(normalized).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} must be a safe relative path")
    if single_component and len(parts) != 1:
        raise ValueError(f"{field} must be a single file name")
    return normalized


def _validate_prompt_id(prompt_id: Any) -> None:
    if (
        not isinstance(prompt_id, str)
        or not prompt_id
        or prompt_id in {".", ".."}
        or any(char in prompt_id for char in "/\\?#%")
        or any(ord(char) < 32 or ord(char) == 127 for char in prompt_id)
    ):
        raise ValueError("prompt_id must be a non-empty path-safe id")


def _safe_filename(filename: str) -> str:
    # The server chooses names, but never let it choose a local directory.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-\u0080-\uffff]+", "_", name).strip("._")
    return name or "output.bin"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
