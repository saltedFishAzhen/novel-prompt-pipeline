"""HTTPX client for a remote ComfyUI server."""

from __future__ import annotations

import copy
import json
import mimetypes
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlencode

import httpx

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "_data"
DEFAULT_BINDINGS_FILE = Path(__file__).resolve().parents[1] / "workflow_bindings.json"


def _default_bindings_file() -> Path:
    """Prefer the wheel-bundled copies, fall back to the source checkout."""
    packaged = PACKAGE_DATA_DIR / "workflow_bindings.json"
    return packaged if packaged.is_file() else DEFAULT_BINDINGS_FILE


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


class BindingError(ValueError):
    """A requested runtime value cannot be applied to this graph/profile."""


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


def bind_graph(
    graph: dict[str, Any],
    profile: str | Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
    *,
    bindings_file: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``graph`` with ``values`` applied through a profile.

    ``values`` accepts ``prompt`` (required string), ``seed``, ``width``,
    ``height``, ``length``, ``first_frame``, ``negative_prompt``,
    ``reference_images`` and ``reference_audios``. Media entries are
    ComfyUI input-relative names, normally returned by
    :meth:`ComfyUIClient.upload_file`. Unused reference slots are dropped so
    their loader nodes never reach the server. The source graph is not
    mutated.
    """
    if not isinstance(graph, dict) or not graph:
        raise BindingError("workflow graph must be a non-empty object")
    name, selected = _resolve_profile(profile, bindings_file)
    return _apply_values(graph, selected, name, values)


def build_graph(
    profile: str | Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
    *,
    bindings_file: str | os.PathLike[str] | None = None,
    workflows_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the profile's API graph and return it with ``values`` bound."""
    name, selected = _resolve_profile(profile, bindings_file)
    return _apply_values(_load_workflow(selected, workflows_dir), selected, name, values)


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


_VALUE_FIELDS = frozenset(
    {
        "prompt",
        "seed",
        "width",
        "height",
        "length",
        "first_frame",
        "negative_prompt",
        "reference_images",
        "reference_audios",
    }
)


def _load_bindings(bindings_file: str | os.PathLike[str] | None) -> dict[str, Any]:
    path = Path(bindings_file) if bindings_file is not None else _default_bindings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load binding profiles {path}: {exc}") from exc
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise BindingError(f"binding file {path} declares no profiles")
    return profiles


def _resolve_profile(
    profile: str | Mapping[str, Any], bindings_file: str | os.PathLike[str] | None
) -> tuple[str, dict[str, Any]]:
    if isinstance(profile, Mapping):
        return "custom", dict(profile)
    if not isinstance(profile, str) or not profile:
        raise BindingError("profile must be a profile name or object")
    profiles = _load_bindings(bindings_file)
    selected = profiles.get(profile)
    if not isinstance(selected, dict):
        known = ", ".join(sorted(profiles))
        raise BindingError(f"unknown workflow profile: {profile} (known: {known})")
    return profile, selected


def _load_workflow(
    profile: Mapping[str, Any], workflows_dir: str | os.PathLike[str] | None
) -> dict[str, Any]:
    workflow_name = profile.get("workflow")
    if not isinstance(workflow_name, str) or not workflow_name:
        raise BindingError("profile does not declare a workflow")
    windows, posix = PureWindowsPath(workflow_name), PurePosixPath(workflow_name)
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or ".." in windows.parts
        or ".." in posix.parts
    ):
        raise BindingError("profile workflow must be a relative path")
    root = (
        Path(workflows_dir).expanduser().resolve()
        if workflows_dir is not None
        else _default_bindings_file().parent
    )
    target = (root / workflow_name).resolve()
    if not _is_relative_to(target, root):
        raise BindingError(f"profile workflow escapes {root}")
    try:
        graph = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load workflow JSON {target}: {exc}") from exc
    if not isinstance(graph, dict) or not graph:
        raise BindingError(f"workflow {target} must contain a non-empty JSON object")
    return graph


def _remote_names(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BindingError(f"{field} must be a list of ComfyUI-relative names")
    names = list(value)
    for index, name in enumerate(names):
        _validate_remote_name(name, f"{field}[{index}]")
    return names


def _apply_values(
    graph: dict[str, Any],
    profile: Mapping[str, Any],
    name: str,
    values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(values or {})
    unknown = set(raw) - _VALUE_FIELDS
    if unknown:
        raise BindingError("unsupported workflow value(s): " + ", ".join(sorted(unknown)))
    prompt = raw.get("prompt")
    if not isinstance(prompt, str):
        raise BindingError("prompt must be a string")
    for field in ("width", "height", "length"):
        value = raw.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise BindingError(f"{field} must be a positive integer")
    first_frame = raw.get("first_frame")
    if first_frame is not None:
        _validate_remote_name(first_frame, "first_frame")
    reference_images = _remote_names(raw.get("reference_images"), "reference_images")
    reference_audios = _remote_names(raw.get("reference_audios"), "reference_audios")

    bound = copy.deepcopy(graph)
    if profile.get("requires_first_frame") and not first_frame:
        raise BindingError(f"profile {name!r} requires first_frame")
    for field in ("prompt", "seed", "width", "height", "length", "negative_prompt"):
        value = raw.get(field)
        if value is not None:
            _bind_scalar(bound, profile, name, field, value)
    if first_frame is not None and profile.get("first_frame") is not None:
        _bind_scalar(bound, profile, name, "first_frame", first_frame)

    explicit_size_mode = profile.get("explicit_size_mode")
    width, height = raw.get("width"), raw.get("height")
    if explicit_size_mode is not None and (width is not None or height is not None):
        _bind_explicit_size(bound, profile, name, explicit_size_mode, width, height)

    _bind_slots(bound, profile.get("reference_slots", []), reference_images, kind="reference image")
    _bind_slots(bound, profile.get("audio_slots", []), reference_audios, kind="reference audio")
    return bound


def _bind_scalar(
    graph: dict[str, Any], profile: Mapping[str, Any], name: str, field: str, value: Any
) -> None:
    if field == "first_frame":
        mappings: Any = [profile.get("first_frame")]
    else:
        bindings = profile.get("bindings", {})
        if not isinstance(bindings, dict):
            raise BindingError("profile bindings must be an object")
        mappings = bindings.get(field)
    if mappings is None:
        raise BindingError(f"profile {name!r} does not support {field!r}")
    if not isinstance(mappings, list) or not mappings:
        raise BindingError(f"profile binding {field!r} must be a non-empty list")
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise BindingError(f"invalid mapping for {field!r}")
        expected = mapping.get("type", "any")
        if not _matches_type(value, expected):
            raise BindingError(f"{field} must have type {expected}, got {type(value).__name__}")
        _set_input(graph, mapping, value, field=field)


def _bind_explicit_size(
    graph: dict[str, Any],
    profile: Mapping[str, Any],
    name: str,
    mode: Any,
    width: int | None,
    height: int | None,
) -> None:
    if not isinstance(mode, dict) or not isinstance(mode.get("value"), str):
        raise BindingError("explicit_size_mode must declare a target mode")
    _set_input(graph, mode, mode["value"], field="explicit_size_mode")
    size_node = graph.get(str(mode.get("node_id", "")), {})
    size_inputs = size_node.get("inputs", {}) if isinstance(size_node, dict) else {}
    effective_width = size_inputs.get("target_width")
    effective_height = size_inputs.get("target_height")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (effective_width, effective_height)
    ):
        raise BindingError("explicit target dimensions must be positive integers")
    # A single explicit dimension keeps the other target dimension and its
    # mirrored LOW-size bindings from the workflow.
    if width is None:
        _bind_scalar(graph, profile, name, "width", effective_width)
    if height is None:
        _bind_scalar(graph, profile, name, "height", effective_height)
    ratios = profile.get("explicit_aspect_ratios")
    if ratios is None:
        return
    if not isinstance(ratios, dict) or not ratios:
        raise BindingError("explicit_aspect_ratios must be a non-empty mapping")
    requested = effective_width / effective_height
    candidates: list[tuple[float, str]] = []
    for ratio, label in ratios.items():
        try:
            horizontal, vertical = str(ratio).split(":")
            reference = int(horizontal) / int(vertical)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise BindingError("invalid explicit aspect ratio") from exc
        if reference <= 0 or not isinstance(label, str) or not label:
            raise BindingError("invalid explicit aspect ratio")
        candidates.append((max(requested / reference, reference / requested), label))
    distance, label = min(candidates)
    if distance > 1.03:
        raise BindingError("explicit dimensions do not match a supported aspect ratio")
    _set_input(graph, profile.get("shuohao_aspect_ratio"), label, field="explicit_aspect_ratio")


def _bind_slots(graph: dict[str, Any], slots: Any, names: list[str], *, kind: str) -> None:
    if not isinstance(slots, list):
        raise BindingError(f"{kind} slots must be a list")
    if len(names) > len(slots):
        raise BindingError(f"profile supports {len(slots)} {kind}(s); received {len(names)}")
    for index, remote_name in enumerate(names):
        slot = slots[index]
        if not isinstance(slot, dict) or not isinstance(slot.get("load"), dict):
            raise BindingError(f"{kind} slot {index} needs a load mapping")
        _set_input(graph, slot["load"], remote_name, field=f"{kind}[{index}]")
        targets = slot.get("conditioning", [])
        if not isinstance(targets, list):
            raise BindingError(f"{kind} slot {index} conditioning must be a list")
        for target in targets:
            # Confirm each pass already exposes the socket; profiles never
            # invent ComfyUI inputs at runtime.
            _check_input(graph, target, field=f"{kind} slot {index}")
    for slot in slots[len(names) :]:
        if not isinstance(slot, dict):
            continue
        targets = slot.get("conditioning", [])
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict):
                continue
            node = graph.get(str(target.get("node_id", "")))
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"].pop(target.get("input"), None)
        load = slot.get("load")
        if isinstance(load, dict) and str(load.get("node_id", "")):
            graph.pop(str(load["node_id"]), None)


def _set_input(graph: dict[str, Any], mapping: Any, value: Any, *, field: str) -> None:
    if not isinstance(mapping, dict):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    if not node_id or not isinstance(input_name, str) or not input_name:
        raise BindingError(f"mapping for {field!r} requires node_id and input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    if input_name not in node["inputs"]:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")
    if not _matches_type(value, mapping.get("type", "any")):
        raise BindingError(f"mapped value for {field!r} has an unexpected type")
    node["inputs"][input_name] = value


def _check_input(graph: dict[str, Any], mapping: Any, *, field: str) -> None:
    if not isinstance(mapping, dict):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    if not isinstance(input_name, str) or input_name not in node["inputs"]:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")


def _matches_type(value: Any, type_name: Any) -> bool:
    checks: dict[str, Any] = {
        "any": lambda _: True,
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
    }
    if not isinstance(type_name, str) or type_name not in checks:
        raise BindingError(f"unsupported binding type: {type_name!r}")
    return bool(checks[type_name](value))


def _validate_remote_name(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{field} must be a non-empty ComfyUI-relative name")
    normalized = value.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise BindingError(f"{field} must be a safe ComfyUI-relative name")
