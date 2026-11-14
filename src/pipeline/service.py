"""Shared application operations used by both the CLI and the MCP server."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import wave
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from uuid import uuid4

from filelock import FileLock, Timeout

from .config import Config
from .storage import RunLock, write_json


class WorkspacePathError(ValueError):
    """An MCP request attempted to access a path outside its workspace."""


def _tts_unlinked_path(path: Path) -> Path:
    """Keep output and reference paths from traversing links or junctions."""
    path = path.expanduser().absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("TTS paths cannot contain symbolic links or junctions")
    return path


def _publish_tts_file(source: Path, destination: Path) -> None:
    """Publish a staged file atomically without replacing an existing file."""
    _tts_unlinked_path(destination)
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination)
        source.unlink()


def _tts_model_summary(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise ValueError("Invalid TTS model information")
    digest = info.get("model_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid TTS model identity")
    result = {"model_digest": digest}
    for key in ("frame_rate", "max_new_tokens", "max_batch_texts"):
        if key in info:
            value = info[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("Invalid TTS model information")
            result[key] = value
    return result


def _tts_wav_summary(path: Path, max_bytes: int) -> dict[str, Any]:
    _tts_unlinked_path(path)
    if not path.is_file() or path.stat().st_size > max_bytes + 1024 * 1024:
        raise ValueError("TTS WAV is missing or exceeds the size limit")
    try:
        with wave.open(str(path), "rb") as source:
            channels, rate, width, frames = (
                source.getnchannels(),
                source.getframerate(),
                source.getsampwidth(),
                source.getnframes(),
            )
            if (channels, rate, width, source.getcomptype()) != (
                1,
                24000,
                2,
                "NONE",
            ) or frames <= 0:
                raise ValueError("TTS output must be mono 24 kHz 16-bit PCM WAV")
            remaining = frames * channels * width
            if remaining > max_bytes:
                raise ValueError("TTS WAV exceeds the size limit")
            while remaining:
                part = source.readframes(min(remaining // width, 32768))
                if not part:
                    raise ValueError("TTS WAV is truncated")
                remaining -= len(part)
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("TTS output is not a readable PCM WAV") from exc
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "sha256": digest,
        "sample_rate": rate,
        "channels": channels,
        "duration_seconds": frames / rate,
    }


class AppService:
    """Expose the same project operations to CLI and MCP transports.

    The local CLI can accept explicit paths anywhere the user has access.
    MCP callers are confined to the configured workspace, including symlinks
    and paths referenced by a configuration file.
    """

    def __init__(
        self,
        workspace: str | Path,
        config_path: str | Path | None = None,
        *,
        restricted: bool = False,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.restricted = restricted
        config = Path(config_path).expanduser() if config_path else None
        self.config_path = (
            (self.workspace / config) if config is not None and not config.is_absolute() else config
        )
        self.config_path = self.config_path.resolve() if self.config_path is not None else None
        if restricted and self.config_path is not None:
            self._ensure_inside(self.config_path)

    def _ensure_inside(self, path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_relative_to(self.workspace):
            raise WorkspacePathError("Path must remain inside the configured workspace")
        return resolved

    def _present_path(self, path: str | Path) -> str:
        resolved = Path(path).expanduser().resolve()
        if self.restricted:
            return str(self._ensure_inside(resolved).relative_to(self.workspace))
        return str(resolved)

    def path(self, value: str | Path) -> Path:
        """Resolve a user-provided path and enforce the MCP workspace boundary."""
        raw = str(value)
        if "\x00" in raw or not raw.strip():
            raise ValueError("Path must be nonempty and must not contain a NUL byte")
        if not self.restricted:
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (self.workspace / path).resolve()
        posix, windows = PurePosixPath(raw), PureWindowsPath(raw)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in posix.parts
            or ".." in windows.parts
        ):
            raise WorkspacePathError("Use a relative path inside the configured workspace")
        return self._ensure_inside(self.workspace / Path(raw))

    def config(self) -> Config:
        from novel_prompt_pipeline.resources import workflow_root

        config_path = self.config_path
        if config_path is None:
            local = self.workspace / "configs" / "pipeline.local.json"
            if local.is_file():
                config_path = local
        if self.restricted and config_path is not None:
            self._ensure_inside(config_path)
        config = Config.load(config_path)
        supplied = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path else {}
        if "asset_library" not in supplied:
            config.asset_library = str(self.workspace / "runs" / "library" / "assets.sqlite3")
        if self.restricted:
            # A local project configuration cannot grant access to
            # paths outside the workspace, including SQLite and media inputs.
            self._ensure_inside(config.asset_library)
            # AssetLibrary stores managed media next to the catalog in files/.
            # A directory symlink must not redirect MCP uploads outside root.
            self._ensure_inside(Path(config.asset_library).parent / "files")
            self._ensure_inside(Path(config.asset_library).parent / "comfyui-routing.json")
            self._ensure_inside(Path(config.asset_library).parent / "comfyui-routing.json.lock")
            self._ensure_inside(Path(config.asset_library).parent / "comfyui-upload-locations.json")
            self._ensure_inside(
                Path(config.asset_library).parent / "comfyui-upload-locations.json.lock"
            )
            self._ensure_inside(Path(config.asset_library).parent / "comfyui-job-locations.json")
            self._ensure_inside(
                Path(config.asset_library).parent / "comfyui-job-locations.json.lock"
            )
            if config.media.bindings_file:
                self._ensure_inside(config.media.bindings_file)
            if config.media.workflows_dir:
                self._ensure_inside(config.media.workflows_dir)
            for audio in config.media.reference_audios:
                if not audio.get("remote"):
                    self._ensure_inside(audio["path"])
            for portrait in config.media.character_face_references.values():
                self._ensure_inside(portrait)
            for reverse in config.media.scene_reverse_references.values():
                self._ensure_inside(reverse)
        if not config.media.workflows_dir:
            workflow_dir = workflow_root(self.workspace)
            # In an initialized workspace, bind its own workflow files. In a
            # bare checkout/testing context retain the existing default profile.
            if workflow_dir == self.workspace / "workflows":
                config.media.workflows_dir = str(workflow_dir)
        if config.media.workflows_dir and not config.media.bindings_file:
            local_bindings = Path(config.media.workflows_dir) / "workflow_bindings.json"
            if local_bindings.is_file():
                config.media.bindings_file = str(local_bindings.resolve())
                if self.restricted:
                    self._ensure_inside(config.media.bindings_file)
        return config

    def workspace_info(self) -> dict[str, Any]:
        config = self.config()
        return {
            "workspace": str(self.workspace),
            "project_id": config.project_id,
            "workflow": "shuohao-v1",
            "asset_library": str(Path(config.asset_library).resolve()),
            "asset_library_exists": Path(config.asset_library).is_file(),
            "comfyui_url_configured": self._comfy_configured(config),
        }

    def library(self):
        from .library import AssetLibrary

        config = self.config()
        return AssetLibrary(config.asset_library, config.project_id)

    def assets_list(
        self, kind: str | None = None, query: str | None = None, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        # Merely listing an uninitialised workspace must not create a database.
        if not Path(self.config().asset_library).is_file():
            return []
        return self.library().list(kind=kind, query=query, include_archived=include_archived)

    def asset_get(self, asset_id: str, version: str | None = None) -> dict[str, Any]:
        library = self.library()
        asset = library.get(asset_id, version)
        asset["files"] = [
            self._present_file(item) for item in library.files(asset_id, asset_version=version)
        ]
        return asset

    def _present_file(self, item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        if self.restricted and "local_path" in result:
            result["local_path"] = self._present_path(result["local_path"])
        return result

    def asset_versions(self, asset_id: str) -> list[dict[str, Any]]:
        return self.library().versions(asset_id)

    def assets_import(self, file: str | Path) -> list[dict[str, Any]]:
        data = json.loads(self.path(file).read_text(encoding="utf-8-sig"))
        if self.restricted:
            # Import manifests can contain media paths nested anywhere in their
            # records; do not allow a manifest to bypass the MCP path guard.
            self._validate_import_paths(data)
        return self.library().import_data(data)

    def _validate_import_paths(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self._validate_import_paths(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"local_path", "path"} and isinstance(item, str):
                    # MCP import files use workspace-relative paths, exactly as
                    # the MCP export tool emits; normalize before library use.
                    value[key] = str(self.path(item))
                else:
                    self._validate_import_paths(item)

    def assets_export(self, file: str | Path) -> dict[str, str]:
        path = self.path(file)
        if self.restricted and path.exists():
            raise FileExistsError("Choose a new file for the MCP asset export")
        data = self.library().export()
        if self.restricted:
            for asset in data["assets"]:
                asset["files"] = [self._present_file(item) for item in asset["files"]]
        write_json(path, data)
        return {"exported": self._present_path(path)}

    def asset_update(
        self, asset_id: str, description: str | None = None, aliases: str | None = None
    ) -> dict[str, Any]:
        if description is None and aliases is None:
            raise ValueError("Provide a description or aliases to update")
        return self.library().update(asset_id, description=description, aliases=aliases)

    def asset_link_image(self, asset_id: str, file: str | Path) -> dict[str, Any]:
        path = self.path(file)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("A supported image file is required")
        return self._present_file(self.library().link_file(asset_id, path, role="reference"))

    def asset_link_face(self, asset_id: str, file: str | Path) -> dict[str, Any]:
        from .config import verify_face_photo

        path = verify_face_photo(self.path(file))
        library = self.library()
        asset = library.get(asset_id)
        if asset["kind"] != "character":
            raise ValueError("Face references can only be attached to character assets")
        return self._present_file(library.link_file(asset_id, path, role="face"))

    def asset_set_archived(self, asset_id: str, archived: bool) -> dict[str, Any]:
        library = self.library()
        (library.archive if archived else library.restore)(asset_id)
        return {"asset_id": asset_id, "operation": "archive" if archived else "restore"}

    def _text_tasks_root(self, run: str | Path) -> Path:
        root = self.path(run)
        if self.restricted:
            for path in (root / "state.json", root / ".run.lock", root / "text_tasks"):
                self._ensure_inside(path)
            tasks = root / "text_tasks"
            if tasks.is_dir():
                for path in tasks.rglob("*"):
                    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                        self._ensure_inside(path)
        return root

    def _present_text_task(self, task: dict[str, Any]) -> dict[str, Any]:
        result = dict(task)
        if self.restricted:
            for metadata in (result.get("request"), result.get("response")):
                if not isinstance(metadata, dict):
                    continue
                for key, value in metadata.items():
                    if key == "path" or key.endswith(("_path", "_file", "_directory")):
                        if isinstance(value, str):
                            posix, windows = PurePosixPath(value), PureWindowsPath(value)
                            if (
                                posix.is_absolute()
                                or windows.is_absolute()
                                or windows.drive
                                or ".." in posix.parts
                                or ".." in windows.parts
                            ):
                                raise WorkspacePathError(
                                    "Text task metadata paths must be task-relative"
                                )
            for name in ("task_directory", "prompt_file", "response_file"):
                if result.get(name) is not None:
                    result[name] = self._present_path(result[name])
        return result

    def text_tasks_list(self, run: str | Path) -> list[dict[str, Any]]:
        """List text tasks waiting for Codex native subagents."""
        from .shuohao.tasks import list_text_tasks

        return [
            self._present_text_task(task) for task in list_text_tasks(self._text_tasks_root(run))
        ]

    def text_task_get(self, run: str | Path, task_id: str) -> dict[str, Any]:
        """Read a text task request."""
        from .shuohao.tasks import get_text_task

        return self._present_text_task(get_text_task(self._text_tasks_root(run), task_id))

    def text_task_complete(
        self, run: str | Path, task_id: str, file: str | Path, agent_id: str, notes: str = ""
    ) -> dict[str, Any]:
        """Submit a subagent response for subsequent engine validation."""
        from .shuohao.tasks import complete_text_task, get_text_task

        root = self._text_tasks_root(run)
        source = Path(file).expanduser()
        source = source if source.is_absolute() else self.workspace / source
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError("Text result cannot be a symbolic link")
        response = self.path(file)
        if self.restricted:
            self._present_text_task(get_text_task(root, task_id))
        return self._present_text_task(
            complete_text_task(root, task_id, response, agent_id=agent_id, notes=notes)
        )

    def _image_tasks_root(self, run: str | Path) -> Path:
        """Resolve a run before the task store can read or write its artifacts."""
        root = self.path(run)
        if self.restricted:
            for path in (root / "state.json", root / ".run.lock", root / "image_tasks"):
                self._ensure_inside(path)
            tasks = root / "image_tasks"
            if tasks.is_dir():
                for path in tasks.rglob("*"):
                    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                        self._ensure_inside(path)
        return root

    def _present_image_task(self, task: dict[str, Any]) -> dict[str, Any]:
        result = dict(task)
        if self.restricted:
            # The request/result metadata participates in the task fingerprint.
            # Keep its paths task-relative instead of rewriting signed content.
            def check_relative_paths(value: Any, *, path_value: bool = False) -> None:
                if isinstance(value, str) and path_value:
                    posix, windows = PurePosixPath(value), PureWindowsPath(value)
                    if (
                        posix.is_absolute()
                        or windows.is_absolute()
                        or windows.drive
                        or ".." in posix.parts
                        or ".." in windows.parts
                    ):
                        raise WorkspacePathError("Image task metadata paths must be task-relative")
                if isinstance(value, list):
                    for item in value:
                        check_relative_paths(item, path_value=path_value)
                elif isinstance(value, dict):
                    for key, item in value.items():
                        is_path = key in {"path", "paths", "file", "files", "directory"} or (
                            key.endswith(("_path", "_paths", "_file", "_files", "_directory"))
                        )
                        check_relative_paths(item, path_value=is_path or path_value)

            check_relative_paths(result.get("request"))
            check_relative_paths(result.get("result"))
            request = result.get("request")
            if isinstance(request, dict):
                # Context is free-form metadata, not prompt text. No nested
                # value may smuggle an absolute path under an arbitrary key.
                check_relative_paths(request.get("context"), path_value=True)
            for name in ("task_directory", "prompt_file", "result_file"):
                if result.get(name) is not None:
                    result[name] = self._present_path(result[name])
            if isinstance(result.get("reference_files"), list):
                result["reference_files"] = [
                    self._present_path(path) for path in result["reference_files"]
                ]
        return result

    def image_tasks_list(self, run: str | Path) -> list[dict[str, Any]]:
        """List local image-generation checkpoints for a run."""
        from .images.tasks import list_image_tasks

        root = self._image_tasks_root(run)
        return [self._present_image_task(task) for task in list_image_tasks(root)]

    def image_plan_get(self, run: str | Path) -> dict[str, Any]:
        """Read the complete asset prompt plan from this run without following a plan link."""
        from .protocols import json_value

        root = self.path(run)
        path = root / "asset_image_plan.json"
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise WorkspacePathError("Image plan must be a regular file in the run directory")
        if not path.resolve().is_relative_to(root):
            raise WorkspacePathError("Image plan must remain in the run directory")
        if self.restricted:
            self._ensure_inside(path)
        plan = json_value(path.read_text(encoding="utf-8"))
        if (
            not isinstance(plan, dict)
            or set(plan) != {"schema_version", "prompt_version", "assets"}
            or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1
            or not isinstance(plan["prompt_version"], str)
            or not plan["prompt_version"].strip()
            or not isinstance(plan["assets"], list)
            or any(not isinstance(item, dict) for item in plan["assets"])
        ):
            raise ValueError("Invalid asset image plan")
        state_path = root / "state.json"
        if state_path.is_symlink() or getattr(state_path, "is_junction", lambda: False)():
            raise WorkspacePathError("Run state must be a regular file in the run directory")
        if state_path.exists():
            state = json_value(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("prompt_version") != plan["prompt_version"]:
                raise ValueError("Image plan prompt version does not match this run")
        if self.restricted:

            def check_metadata_paths(value: Any, *, path_value: bool = False) -> None:
                if isinstance(value, str) and path_value:
                    posix, windows = PurePosixPath(value), PureWindowsPath(value)
                    if (
                        posix.is_absolute()
                        or windows.is_absolute()
                        or windows.drive
                        or ".." in posix.parts
                        or ".." in windows.parts
                    ):
                        raise WorkspacePathError("Image plan metadata paths must be relative")
                elif isinstance(value, list):
                    for item in value:
                        check_metadata_paths(item, path_value=path_value)
                elif isinstance(value, dict):
                    for key, item in value.items():
                        is_path = key in {
                            "path",
                            "paths",
                            "file",
                            "files",
                            "directory",
                        } or key.endswith(("_path", "_paths", "_file", "_files", "_directory"))
                        check_metadata_paths(
                            item,
                            path_value=path_value or is_path or key in {"metadata", "context"},
                        )

            check_metadata_paths(plan)
        return plan

    def image_task_get(self, run: str | Path, task_id: str) -> dict[str, Any]:
        """Show an image request and its fixed reference files."""
        from .images.tasks import get_image_task

        root = self._image_tasks_root(run)
        return self._present_image_task(get_image_task(root, task_id))

    def image_task_complete(
        self,
        run: str | Path,
        task_id: str,
        file: str | Path,
        notes: str,
        accepted: bool = False,
        main_region: list[int] | None = None,
    ) -> dict[str, Any]:
        """Attach an inspected result to a pending image-generation checkpoint."""
        from .images.tasks import complete_image_task, get_image_task

        root = self._image_tasks_root(run)
        source = Path(file).expanduser()
        source = source if source.is_absolute() else self.workspace / source
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError("Image result cannot be a symbolic link")
        image = self.path(file)
        if self.restricted:
            # Check untrusted metadata before writing, so a rejected response
            # cannot have already completed a task as a hidden side effect.
            self._present_image_task(get_image_task(root, task_id))
        return self._present_image_task(
            complete_image_task(
                root, task_id, image, notes=notes, accepted=accepted, main_region=main_region
            )
        )

    def tts_check(self) -> dict[str, Any]:
        """Check only the separately configured Breeze TTS endpoint."""
        from tts import BreezeTTSClient, TTSConfig

        config = self.config().tts or TTSConfig()
        if not os.getenv(config.url_env, "").strip():
            return {"configured": False, "status": "not_configured"}
        with BreezeTTSClient.from_config(config) as client:
            health = client.health()
            if health.get("status") != "ok" or health.get("sample_rate") != 24000:
                raise ValueError("Invalid TTS health information")
            model = _tts_model_summary(client.model_info())
        return {
            "configured": True,
            "health": {"status": "ok", "sample_rate": 24000},
            "model": model,
        }

    def _postproduction_path(self, value: str | Path) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = self.workspace / raw
        _tts_unlinked_path(raw)
        return self.path(value)

    def postproduction_check(
        self, run: str | Path | None = None, plan: str | Path | None = None
    ) -> dict[str, Any]:
        from .postproduction import check_environment, check_plan
        from .postproduction.plan import load_json

        result = check_environment()
        if bool(run) != bool(plan):
            raise ValueError("Run and plan must be provided together")
        if run and plan:
            data = load_json(self._postproduction_path(plan))
            refs = {
                key: self._postproduction_path(voice["reference_audio"])
                for key, voice in data.get("voices", {}).items()
                if voice.get("reference_audio")
            }
            checked = check_plan(self._postproduction_path(run), data, reference_paths=refs)
            result["plan_id"] = checked["plan_id"]
            result["cue_count"] = len(checked["plan"]["cues"])
        return result

    def postproduction_prepare(
        self, run: str | Path, plan: str | Path, retry_uncertain: bool = False
    ) -> dict[str, Any]:
        from .postproduction import prepare_dialogue
        from .postproduction.plan import load_json

        data = load_json(self._postproduction_path(plan))
        refs = {
            key: self._postproduction_path(voice["reference_audio"])
            for key, voice in data.get("voices", {}).items()
            if voice.get("reference_audio")
        }
        return prepare_dialogue(
            self._postproduction_path(run),
            data,
            reference_paths=refs,
            synthesize=lambda values, output: self.tts_synthesize(
                self._present_path(values), self._present_path(output)
            ),
            retry_uncertain=retry_uncertain,
        )

    def postproduction_render(
        self, run: str | Path, plan_id: str, panel_index: int, video: str | Path, model: str | Path
    ) -> dict[str, Any]:
        from .postproduction import render_shot
        from .postproduction.plan import load_json

        config = load_json(self._postproduction_path(model))
        if (
            not isinstance(config, dict)
            or set(config) != {"model_file", "sha256", "license", "companions"}
            or not isinstance(config["companions"], list)
        ):
            raise ValueError("Invalid local separator model manifest")
        config["model_file"] = self._postproduction_path(config["model_file"])
        for item in config["companions"]:
            if not isinstance(item, dict) or set(item) != {"file", "sha256"}:
                raise ValueError("Invalid separator companion file")
            item["file"] = self._postproduction_path(item["file"])
        return render_shot(
            self._postproduction_path(run),
            plan_id,
            panel_index,
            self._postproduction_path(video),
            config,
        )

    def tts_synthesize(
        self, values: str | Path, output: str | Path, asset_id: str | None = None
    ) -> dict[str, Any]:
        """Generate and preserve a voice sample, optionally linking it to a character."""
        from tts import BreezeTTSClient, TTSConfig

        values_path = self.path(values)
        try:
            with values_path.open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("TTS values file exceeds the size limit")
            data = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("TTS values must be a readable JSON object") from exc
        allowed = {"text", "instruction", "reference_audio", "reference_text", "cfg_scale", "seed"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("TTS values contain unsupported fields")
        text = data.get("text")
        instruction = data.get("instruction", "")
        transcript = data.get("reference_text", "")
        ref = data.get("reference_audio")
        scale = data.get("cfg_scale", 1.0)
        seed = data.get("seed", 42)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text must be nonempty")
        if not isinstance(instruction, str) or not isinstance(transcript, str):
            raise ValueError("TTS instruction and reference text must be strings")
        if ref is not None and (not isinstance(ref, str) or not ref.strip() or "\x00" in ref):
            raise ValueError("TTS reference audio must be a local path")
        if bool(ref) != bool(transcript.strip()):
            raise ValueError("Reference audio and its transcript must be supplied together")
        if not instruction.strip() and not ref:
            raise ValueError("Provide a voice instruction or reference audio and transcript")
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or scale <= 0
        ):
            raise ValueError("TTS cfg_scale must be a positive finite number")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("TTS seed is outside the supported range")

        reference = None
        if ref:
            posix, windows = PurePosixPath(ref), PureWindowsPath(ref)
            if self.restricted and (
                posix.is_absolute()
                or windows.is_absolute()
                or windows.drive
                or ".." in posix.parts
                or ".." in windows.parts
            ):
                raise WorkspacePathError("Use a relative reference path inside the workspace")
            reference = Path(ref).expanduser()
            if not reference.is_absolute():
                reference = values_path.parent / reference
            _tts_unlinked_path(reference)
            if self.restricted:
                self._ensure_inside(reference)
            if reference.suffix.lower() != ".wav" or not reference.is_file():
                raise ValueError("TTS reference must be an existing local WAV file")

        raw_output = Path(output).expanduser()
        if not raw_output.is_absolute():
            raw_output = self.workspace / raw_output
        _tts_unlinked_path(raw_output)
        target = self.path(output)
        if target.suffix.lower() != ".wav":
            raise ValueError("TTS output must use the .wav extension")
        metadata_path = target.with_suffix(".voice.json")
        lock_path = target.with_suffix(".voice.lock")
        recovery_audio = target.with_suffix(".voice.stage.wav")
        for path in (target, metadata_path, lock_path, recovery_audio):
            _tts_unlinked_path(path)
            if self.restricted:
                self._ensure_inside(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _tts_unlinked_path(target)
        config = self.config()
        tts_config = config.tts or TTSConfig()
        library = self.library() if asset_id is not None else None
        try:
            asset = None
            if library is not None:
                asset = library.get(asset_id)
                if asset["kind"] != "character":
                    raise ValueError("Voice references can only be linked to character assets")
            asset_identity = (
                {"asset_id": asset["asset_id"], "asset_version": asset["asset_version"]}
                if asset
                else None
            )
            try:
                with FileLock(str(lock_path), timeout=0):
                    _tts_unlinked_path(lock_path)
                    with tempfile.TemporaryDirectory(
                        prefix=".tts-stage-", dir=target.parent
                    ) as temp:
                        stage = Path(temp)
                        ref_copy = None
                        ref_hash = None
                        if reference is not None:
                            ref_copy = stage / "reference.wav"
                            size, digest = 0, hashlib.sha256()
                            with reference.open("rb") as incoming, ref_copy.open("xb") as outgoing:
                                for part in iter(lambda: incoming.read(65536), b""):
                                    size += len(part)
                                    if size > tts_config.max_audio_bytes:
                                        raise ValueError("TTS reference WAV exceeds the size limit")
                                    digest.update(part)
                                    outgoing.write(part)
                            ref_hash = digest.hexdigest()
                        request = {
                            "text": text,
                            "instruction": instruction,
                            "reference_text": transcript,
                            "reference_audio_sha256": ref_hash,
                            "mode": (
                                "direction"
                                if ref_hash and instruction.strip()
                                else "clone"
                                if ref_hash
                                else "design"
                            ),
                            "cfg_scale": scale,
                            "seed": seed,
                        }
                        if target.exists() or metadata_path.exists():
                            audio_to_verify = target if target.exists() else recovery_audio
                            if not audio_to_verify.is_file() or not metadata_path.is_file():
                                raise FileExistsError(
                                    "TTS output or metadata already exists; choose a new output"
                                )
                            with metadata_path.open("rb") as incoming:
                                saved = incoming.read(1024 * 1024 + 1)
                            if len(saved) > 1024 * 1024:
                                raise ValueError("TTS metadata exceeds the size limit")
                            try:
                                metadata = json.loads(saved)
                            except (json.JSONDecodeError, UnicodeError) as exc:
                                raise ValueError("Existing TTS metadata is invalid") from exc
                            if (
                                not isinstance(metadata, dict)
                                or set(metadata)
                                != {
                                    "schema_version",
                                    "provider",
                                    "request",
                                    "model",
                                    "audio",
                                    "asset",
                                }
                                or metadata["schema_version"] != 1
                                or metadata["provider"] != "breeze-tts2"
                                or metadata["request"] != request
                                or metadata["asset"] != asset_identity
                                or metadata["audio"]
                                != _tts_wav_summary(audio_to_verify, tts_config.max_audio_bytes)
                                or metadata["model"] != _tts_model_summary(metadata["model"])
                            ):
                                raise FileExistsError(
                                    "Existing TTS output does not match this request"
                                )
                            if audio_to_verify == recovery_audio:
                                _publish_tts_file(recovery_audio, target)
                            reused = True
                        else:
                            if recovery_audio.exists():
                                raise FileExistsError(
                                    "TTS recovery audio exists without metadata; choose a new output"
                                )
                            if not os.getenv(tts_config.url_env, "").strip():
                                raise ValueError(
                                    f"Set {tts_config.url_env} to the Breeze TTS service URL"
                                )
                            candidate = stage / "sample.wav"
                            with BreezeTTSClient.from_config(tts_config) as client:
                                model = _tts_model_summary(client.model_info())
                                generated = client.synthesize(
                                    text,
                                    candidate,
                                    instruction=instruction,
                                    reference_audio=ref_copy,
                                    reference_text=transcript,
                                    cfg_scale=scale,
                                    seed=seed,
                                )
                            audio = _tts_wav_summary(candidate, tts_config.max_audio_bytes)
                            if generated.get("sha256") != audio["sha256"]:
                                raise ValueError(
                                    "TTS output fingerprint does not match the returned audio"
                                )
                            metadata = {
                                "schema_version": 1,
                                "provider": "breeze-tts2",
                                "request": request,
                                "model": model,
                                "audio": audio,
                                "asset": asset_identity,
                            }
                            staged_metadata = stage / "sample.voice.json"
                            write_json(staged_metadata, metadata)
                            if target.exists() or metadata_path.exists():
                                raise FileExistsError(
                                    "TTS output appeared during generation; choose a new output"
                                )
                            _publish_tts_file(candidate, recovery_audio)
                            _publish_tts_file(staged_metadata, metadata_path)
                            _publish_tts_file(recovery_audio, target)
                            reused = False
                        linked = None
                        if library is not None:

                            def link_once(path: Path, role: str, sha: str) -> dict:
                                existing = library.files(
                                    asset_id,
                                    asset_version=asset_identity["asset_version"],
                                    role=role,
                                )
                                match = next(
                                    (item for item in reversed(existing) if item["sha256"] == sha),
                                    None,
                                )
                                if match is not None:
                                    managed = Path(match["local_path"])
                                    if managed.exists():
                                        _tts_unlinked_path(managed)
                                        with managed.open("rb") as source:
                                            actual = hashlib.file_digest(
                                                source, "sha256"
                                            ).hexdigest()
                                        if actual != sha:
                                            raise ValueError(
                                                "Managed TTS asset failed integrity check"
                                            )
                                        return match
                                return library.link_file(
                                    asset_id,
                                    path,
                                    role=role,
                                    asset_version=asset_identity["asset_version"],
                                )

                            with metadata_path.open("rb") as incoming:
                                metadata_sha = hashlib.file_digest(incoming, "sha256").hexdigest()
                            linked = {
                                "voice": self._present_file(
                                    link_once(
                                        target, "voice_reference", metadata["audio"]["sha256"]
                                    )
                                ),
                                "metadata": self._present_file(
                                    link_once(
                                        metadata_path, "voice_reference_metadata", metadata_sha
                                    )
                                ),
                            }
            except Timeout as exc:
                raise RuntimeError("Another TTS task is writing this output") from exc
        finally:
            if library is not None:
                library.close()
        return {
            "output": self._present_path(target),
            "metadata_file": self._present_path(metadata_path),
            "audio": metadata["audio"],
            "model": metadata["model"],
            "asset": linked,
            "reused": reused,
        }

    def database_info(self) -> dict[str, Any]:
        library = self.library()
        return {
            "path": str(library.path),
            "project_id": library.project_id,
            "schema_version": library.schema_version(),
        }

    def database_migrate(self) -> dict[str, Any]:
        library = self.library()
        return {"path": str(library.path), "schema_version": library.migrate()}

    def run(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        resume: bool = False,
        render: bool = False,
        dry_run: bool = False,
        stop_after: Literal[
            "outline", "characters", "art", "script", "staging", "prompts", "assets", "frames"
        ]
        | None = None,
    ) -> dict[str, Any]:
        from .shuohao.engine import ProductionPipeline

        stages = (
            "outline",
            "characters",
            "art",
            "script",
            "staging",
            "prompts",
            "assets",
            "frames",
        )
        if stop_after is not None and stop_after not in stages:
            raise ValueError("stop_after must be one of: " + ", ".join(stages))
        source = self.path(input_path).read_text(encoding="utf-8-sig")
        output = self.path(output_path)
        if self.restricted and not dry_run and output.exists() and output.is_dir():
            # Resume can read or write many nested artifacts. Check pre-existing
            # symlinks before allowing the engine to open any of them.
            for path in output.rglob("*"):
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    self._ensure_inside(path)
        if not resume and not dry_run and (output / "state.json").exists():
            raise ValueError("Output directory already contains a run; use a new output directory")
        if not resume and not dry_run and output.exists():
            for item in output.iterdir():
                # Native filelock leaves an empty marker when it is released.
                # The engine still acquires the OS lock and rejects live owners.
                if (
                    item.name == ".run.lock"
                    and not item.is_symlink()
                    and item.is_file()
                    and item.stat().st_size == 0
                ):
                    continue
                raise ValueError(
                    "Output directory is not empty; choose a new directory for the run"
                )
        if resume:
            state_path = output / "state.json"
            if self.restricted:
                self._ensure_inside(state_path)
            if not state_path.is_file():
                raise ValueError("Cannot resume without state.json; start a run in a new directory")
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if state.get("workflow") != "shuohao-v1":
                raise ValueError("Unsupported run format; use a new output directory")
        pipeline = ProductionPipeline(self.config())
        try:
            options = {}
            if stop_after is not None:
                options["stop_after"] = stop_after
            result = (
                pipeline.dry_run(source, **options)
                if dry_run
                else pipeline.run(source, output, resume=resume, render=render, **options)
            )
            if self.restricted and "output" in result:
                result["output"] = self._present_path(result["output"])
            return result
        finally:
            pipeline.close()

    def run_status(self, run: str | Path) -> dict[str, Any]:
        root = self.path(run)
        if self.restricted:
            self._ensure_inside(root / "state.json")
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        if state.get("workflow") == "shuohao-v1":
            result = {
                key: state[key]
                for key in (
                    "workflow",
                    "status",
                    "stage",
                    "stages",
                    "artifacts",
                    "prompt_version",
                    "last_error",
                    "pending_image_task",
                    "pending_image_tasks",
                    "pending_text_tasks",
                    "media",
                    "shot_count",
                    "frame_count",
                    "shots",
                    "frames",
                )
                if key in state
            }
            # Paths in production state are run-relative; resolve and check each
            # before exposing them to a restricted MCP client.
            if self.restricted:

                def present_artifact(value: str) -> str:
                    path = Path(value)
                    return self._present_path(path if path.is_absolute() else root / path)

                if isinstance(result.get("artifacts"), dict):
                    result["artifacts"] = {
                        key: present_artifact(value) if isinstance(value, str) else value
                        for key, value in result["artifacts"].items()
                    }
                if isinstance(result.get("stages"), dict):
                    result["stages"] = {
                        key: {
                            **value,
                            **(
                                {"artifact": present_artifact(value["artifact"])}
                                if isinstance(value.get("artifact"), str)
                                else {}
                            ),
                        }
                        if isinstance(value, dict)
                        else value
                        for key, value in result["stages"].items()
                    }
                if isinstance(result.get("media"), dict):
                    for job in result["media"].values():
                        if isinstance(job, dict) and isinstance(job.get("files"), list):
                            job["files"] = [present_artifact(path) for path in job["files"]]
            return result
        result = {key: state.get(key) for key in ("status", "prompt_version", "last_error")}
        for key in (
            "pending_image_task",
            "pending_image_tasks",
            "blocked_image_assets",
            "asset_prompt_preparation",
        ):
            if key in state:
                result[key] = state[key]
        if state.get("status") == "awaiting_character_design":
            result.update(
                pending_character_design=state.get("pending_character_design"),
                character_design_reason=state.get("character_design_reason"),
            )
        if isinstance(state.get("first_frame_media"), dict):
            # Only expose a count: individual records include managed local
            # paths, while image_tasks_list handles workspace-safe detail.
            result["first_frames_completed"] = len(state["first_frame_media"])
        chapter_review = state.get("chapter_prompt_review")
        if isinstance(chapter_review, dict):
            # Return only bounded status metadata. Source excerpts, findings,
            # and local audit paths remain in the run's review artifacts.
            summary = {}
            for key in ("status", "verdict"):
                value = chapter_review.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z_]{1,64}", value):
                    summary[key] = value
            digest = chapter_review.get("panels_sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                summary["panels_sha256"] = digest
            count = chapter_review.get("findings_count")
            if type(count) is int and count >= 0:
                summary["findings_count"] = count
            result["chapter_prompt_review"] = summary
        result.update(
            nodes_completed=len(state.get("nodes", {})),
            panels=len(state.get("panels", [])),
            media=state.get("media", {}),
        )
        if self.restricted:
            for job in result["media"].values():
                if isinstance(job, dict) and isinstance(job.get("files"), list):
                    job["files"] = [self._present_path(path) for path in job["files"]]
        return result

    def attach_job(self, run: str | Path, key: str, prompt_id: str) -> dict[str, str]:
        from .comfy_servers import ComfyRouter

        root = self.path(run)
        if self.restricted:
            self._ensure_inside(root / "state.json")
            self._ensure_inside(root / ".run.lock")
        with RunLock(root):
            path = root / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            entry = state["media"][key]
            if entry.get("status") != "submitting":
                raise ValueError("Only jobs with unknown submission outcome may be attached")
            if not prompt_id.strip():
                raise ValueError("prompt_id cannot be empty")
            router = ComfyRouter(self.config())
            if router.named:
                server = entry.get("server")
                if not server or entry.get("url_sha256") != router.endpoint_hash(server):
                    raise ValueError("The saved job's ComfyUI server is missing or has changed")
            entry.update(prompt_id=prompt_id, status="submitted")
            write_json(path, state)
        result = {"attached": key, "prompt_id": prompt_id}
        if entry.get("server"):
            result["server"] = entry["server"]
        return result

    def _comfy_template(self, profile: str):
        from comfyui_client import WorkflowTemplate, get_default_template

        config = self.config().media
        if config.bindings_file or config.workflows_dir:
            return WorkflowTemplate.from_profile(
                profile, bindings_file=config.bindings_file, workflows_dir=config.workflows_dir
            )
        return get_default_template(profile)

    @staticmethod
    def _comfy_configured(config: Config) -> bool:
        from .comfy_servers import ComfyRouter

        router = ComfyRouter(config)
        if router.named:
            return any(router.url(name, required=False) for name in router.names)
        return bool(os.getenv("COMFYUI_URL") or os.getenv("COMFYUI_BASE_URL"))

    def _comfy_client(self, server: str | None = None, *, router=None):
        from .comfy_servers import ComfyRouter

        router = router or ComfyRouter(self.config())
        if router.named and server is None:
            pool = router.pool("image")
            if len(pool) != 1:
                raise ValueError("Select a ComfyUI server before opening a client")
            server = pool[0]
        return router.client(server)

    def _selected_comfy_client(self, server: str | None, router=None):
        # Keep the legacy no-argument call useful for existing SDK test adapters.
        return (
            self._comfy_client(server, router=router)
            if server is not None
            else self._comfy_client()
        )

    @staticmethod
    def _comfy_stage(profile: str, config: Config, template: Any) -> str:
        if profile == config.media.i2va_profile or profile == "i2va":
            return "i2va"
        return "ref2va" if getattr(template, "has_first_frame", False) else "image"

    def comfy_servers(self) -> dict[str, Any]:
        from .comfy_servers import ComfyRouter

        router = ComfyRouter(self.config())
        if not router.named:
            return {
                "servers": [
                    {"name": "default", "url_configured": self._comfy_configured(router.config)}
                ],
                "balance": None,
            }
        return {
            "servers": [
                {
                    "name": name,
                    "url_configured": bool(router.url(name, required=False)),
                    "api_key_configured": bool(router.credentials(name)),
                }
                for name in router.names
            ],
            "balance": router.balance,
            "default_server": router.default_server,
            "pools": {stage: list(router.pool(stage)) for stage in router.pools},
        }

    def comfy_check(
        self, profile: str = "ref2va", *, remote: bool = False, server: str | None = None
    ) -> dict[str, Any]:
        from .comfy_servers import ComfyRouter

        template = self._comfy_template(profile)
        result = {
            "profile": profile,
            "capabilities": template.capabilities,
            "workflow_nodes": len(template.graph),
        }
        router = ComfyRouter(self.config())
        if server is not None:
            if router.named and server not in router.names:
                raise ValueError(f"Unknown ComfyUI server: {server}")
            if not router.named and server != "default":
                raise ValueError(f"Unknown ComfyUI server: {server}")
            result["server"] = server
        if remote:
            stage = self._comfy_stage(profile, router.config, template)
            candidates = router.pool(stage) if router.named and server is None else ()
            if len(candidates) > 1:
                result["remote_servers"] = {}
                for name in candidates:
                    with self._selected_comfy_client(name, router) as client:
                        result["remote_servers"][name] = client.health()
            else:
                selected = router.choose(stage, server=server)
                if selected is not None:
                    result["server"] = selected
                with self._selected_comfy_client(selected, router) as client:
                    result["remote"] = client.health()
        return result

    def comfy_upload(
        self, file: str | Path, *, subfolder: str = "novel-pipeline", server: str | None = None
    ) -> dict[str, str]:
        from .comfy_servers import ComfyRouter

        path = self.path(file)
        router = ComfyRouter(self.config())
        selected = router.choose("image", server=server)
        with self._selected_comfy_client(selected, router) as client:
            remote_name = client.upload_file(path, subfolder=subfolder)
        result = {"source": self._present_path(path), "remote_name": remote_name}
        if selected is not None or router.legacy_url:
            router.record_upload(selected, remote_name)
        if selected is not None:
            result["server"] = selected
        return result

    def comfy_submit(
        self,
        profile: str,
        values: dict[str, Any],
        output_dir: str | Path = "runs/comfyui",
        *,
        server: str | None = None,
    ) -> dict[str, Any]:
        from .comfy_servers import ComfyRouter
        from .storage import fingerprint

        target = self.path(output_dir)
        values = dict(values)
        prompt = values.pop("prompt", None)
        if not isinstance(prompt, str):
            raise ValueError("values must include a prompt string")
        template = self._comfy_template(profile)
        graph = template.prepare(prompt, **values)
        router = ComfyRouter(self.config())
        if (
            router.named
            and len(router.names) > 1
            and server is None
            and any(
                values.get(key) for key in ("first_frame", "reference_images", "reference_audios")
            )
        ):
            raise ValueError("Specify the ComfyUI server holding the referenced media")
        selected = router.choose(self._comfy_stage(profile, router.config, template), server=server)
        if selected is not None or router.upload_path.exists():
            media = [values.get("first_frame")]
            media.extend(values.get("reference_images") or [])
            media.extend(values.get("reference_audios") or [])
            for name in filter(None, media):
                router.verify_upload_server(selected, name)
        client = self._selected_comfy_client(selected, router)
        record_path = target / ("submission-" + uuid4().hex + ".json")
        record = {"profile": profile, "status": "submitting", "graph_sha256": fingerprint(graph)}
        if selected is not None:
            record.update(server=selected, url_sha256=router.endpoint_hash(selected))
        elif router.legacy_url:
            record["url_sha256"] = router.legacy_endpoint_hash()
        write_json(record_path, record)
        try:
            with client:
                prompt_id = client.submit(graph)
        except Exception as exc:
            # Do not retry an ambiguous POST: the remote server might have
            # accepted the graph before the response was lost.
            record["status"] = "unknown"
            write_json(record_path, record)
            raise RuntimeError(
                f"ComfyUI submission outcome is unknown; inspect the remote queue and {record_path}"
            ) from exc
        record.update(prompt_id=prompt_id, status="queued")
        write_json(record_path, record)
        if selected is not None or router.legacy_url:
            try:
                router.record_job(selected, prompt_id)
            except Exception as exc:
                raise RuntimeError(
                    f"ComfyUI job was submitted but its server registry could not be saved; inspect {record_path} and do not resubmit"
                ) from exc
        record["record_file"] = self._present_path(record_path)
        return record

    def comfy_wait(
        self,
        prompt_id: str,
        output_dir: str | Path = "runs/comfyui",
        *,
        timeout: float = 3600,
        server: str | None = None,
    ) -> dict[str, Any]:
        from .comfy_servers import ComfyRouter
        from .storage import fingerprint

        target = self.path(output_dir)
        router = ComfyRouter(self.config())
        selected = None
        records = []
        for path in target.glob("submission-*.json") if target.is_dir() else ():
            if path.is_symlink():
                continue
            if self.restricted:
                self._ensure_inside(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("prompt_id") == prompt_id:
                records.append(data)
        origins = router.job_locations(prompt_id)
        origins.extend(
            {"server": record.get("server") or "_legacy", "url_sha256": record.get("url_sha256")}
            for record in records
        )
        names = {origin["server"] for origin in origins if origin["server"] != "_legacy"}
        if router.named:
            if server is None:
                if len(names) != 1 or any(origin["server"] == "_legacy" for origin in origins):
                    raise ValueError("Specify the ComfyUI server for this prompt ID")
                server = next(iter(names))
            if (
                origins
                and server not in names
                and not any(
                    origin["server"] == "_legacy"
                    and origin["url_sha256"] in (None, router.endpoint_hash(server))
                    for origin in origins
                )
            ):
                raise ValueError("The saved prompt ID belongs to a different ComfyUI server")
            selected = router.choose("image", server=server)
            for origin in origins:
                if origin["server"] == selected and origin["url_sha256"] != router.endpoint_hash(
                    selected
                ):
                    raise ValueError("The saved ComfyUI server URL changed; do not query this job")
        else:
            if names:
                raise ValueError("The saved job's named ComfyUI server is no longer configured")
            if server not in (None, "default"):
                raise ValueError(f"Unknown ComfyUI server: {server}")
            for origin in origins:
                if origin["url_sha256"] not in (None, router.legacy_endpoint_hash()):
                    raise ValueError("The saved ComfyUI server URL changed; do not query this job")
        if self.restricted or selected is not None:
            # Isolate server-selected output filenames from existing workspace
            # files. Repeating the same wait remains resumable for the same job.
            identity = (
                prompt_id
                if selected is None
                else [selected, router.endpoint_hash(selected), prompt_id]
            )
            target = target / ("job-" + fingerprint(identity)[:24])
            if self.restricted:
                self._ensure_inside(target)
        with self._selected_comfy_client(selected, router) as client:
            history = client.wait(prompt_id, timeout=timeout)
            files = client.download_outputs(history, target, prompt_id=prompt_id)
        result = {
            "prompt_id": prompt_id,
            "status": "completed",
            "files": [self._present_path(path) for path in files],
        }
        if selected is not None:
            result["server"] = selected
        return result
