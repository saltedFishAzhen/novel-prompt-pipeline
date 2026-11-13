"""Shared application operations used by both the CLI and the MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from .config import Config
from .storage import RunLock, write_json


class WorkspacePathError(ValueError):
    """An MCP request attempted to access a path outside its workspace."""


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
        from novel_prompt_pipeline.resources import prompt_root, workflow_root

        config_path = self.config_path
        if config_path is None:
            local = self.workspace / "configs" / "pipeline.local.json"
            if local.is_file():
                config_path = local
        if self.restricted and config_path is not None:
            self._ensure_inside(config_path)
        config = Config.load(config_path)
        supplied = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path else {}
        if "prompt_project" not in supplied:
            config.prompt_project = str(prompt_root(self.workspace))
        if "asset_library" not in supplied:
            config.asset_library = str(self.workspace / "runs" / "library" / "assets.sqlite3")
        if self.restricted:
            # A local project configuration cannot grant the model access to
            # paths outside the workspace, including SQLite and media inputs.
            self._ensure_inside(config.prompt_project)
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

    def _prompts_root(self) -> Path:
        return Path(self.config().prompt_project).resolve()

    def workspace_info(self) -> dict[str, Any]:
        from prompt_project import PromptRelease

        config = self.config()
        current = PromptRelease.load(config.prompt_project, config.prompt_version)
        return {
            "workspace": str(self.workspace),
            "project_id": config.project_id,
            "prompt_version": current.version,
            "asset_library": str(Path(config.asset_library).resolve()),
            "asset_library_exists": Path(config.asset_library).is_file(),
            "llm_model_configured": bool(config.llm.model),
            "llm_key_configured": bool(os.getenv(config.llm.api_key_env)),
            "comfyui_url_configured": self._comfy_configured(config),
        }

    def prompts_list(self, version: str | None = None) -> dict[str, Any]:
        from prompt_project import PromptRelease
        from prompt_project.inventory import coverage

        current = PromptRelease.load(self._prompts_root(), version)
        training = coverage(self._prompts_root())
        return {
            "version": current.version,
            "node_count": len(current.manifest["nodes"]),
            "default_suite": training["default_suite"],
            "missing_default_cases": training["missing_default_cases"],
            "missing_contract_cases": training["missing_contract_cases"],
            "nodes": {
                name: {
                    **training["nodes"].get(name, {}),
                    "variables": entry.get("variables", []),
                    "output_contract": entry.get("output_contract", {}),
                    "release_sha256": entry["sha256"],
                }
                for name, entry in current.manifest["nodes"].items()
            },
        }

    def prompts_build(self, *, check: bool = False) -> dict[str, Any]:
        from prompt_project.compiler import build
        from prompt_project.inventory import coverage

        root = self._prompts_root()
        errors = build(check=check, root=root)
        if errors:
            raise ValueError("Prompt check failed: " + "\n".join(errors))
        report = coverage(root)
        return {
            "status": "checked" if check else "built",
            "prompt_root": self._present_path(root),
            "node_count": report["node_count"],
            "missing_default_cases": report["missing_default_cases"],
            "missing_contract_cases": report["missing_contract_cases"],
        }

    def prompts_stage(self) -> dict[str, Any]:
        from prompt_project.compiler import stage

        release = stage(root=self._prompts_root())
        return {
            "version": release.version,
            "directory": self._present_path(release.root),
            "activated": False,
        }

    def prompts_publish(
        self, evaluation: str | Path | None = None, *, offline: bool = False
    ) -> dict[str, Any]:
        from prompt_project.compiler import publish

        report = self.path(evaluation) if evaluation is not None else None
        release = publish(root=self._prompts_root(), evaluation=report, offline=offline)
        return {"version": release.version, "directory": self._present_path(release.root)}

    def prompts_evaluate(
        self,
        cases: str | Path | None = None,
        version: str | None = None,
        output_root: str | Path | None = None,
        *,
        candidate: bool = False,
        nodes: list[str] | None = None,
        require_coverage: bool = False,
    ) -> dict[str, Any]:
        from prompt_project import PromptRelease
        from prompt_project.evaluate import run_evaluation

        if candidate and version is not None:
            raise ValueError("Choose a candidate or a released version, not both")
        if candidate:
            from prompt_project.compiler import stage

            current = stage(root=self._prompts_root())
        else:
            current = PromptRelease.load(self._prompts_root(), version)
        target = (
            self.path(output_root)
            if output_root is not None
            else self.path("runs/prompt-evaluations")
        )
        path = run_evaluation(
            current,
            self.path(cases) if cases is not None else None,
            target,
            llm_config=self.config().llm,
            nodes=nodes,
            require_coverage=require_coverage,
            validation_retries=self.config().validation_retries,
        )
        return {
            "output": self._present_path(path),
            "prompt_version": current.version,
            "candidate": candidate,
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
    ) -> dict[str, Any]:
        from .engine import Pipeline

        source = self.path(input_path).read_text(encoding="utf-8-sig")
        output = self.path(output_path)
        if self.restricted and not dry_run and output.exists() and output.is_dir():
            # Resume can read or write many nested artifacts. Check pre-existing
            # symlinks before allowing the engine to open any of them.
            for path in output.rglob("*"):
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    self._ensure_inside(path)
        if not resume and not dry_run and output.exists() and not (output / "state.json").exists():
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
        pipeline = Pipeline(self.config())
        try:
            result = (
                pipeline.dry_run(source)
                if dry_run
                else pipeline.run(source, output, resume=resume, render=render)
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
        result = {key: state.get(key) for key in ("status", "prompt_version", "last_error")}
        result.update(
            nodes_completed=len(state["nodes"]),
            panels=len(state.get("panels", [])),
            media=state["media"],
        )
        if self.restricted:
            for job in result["media"].values():
                if isinstance(job, dict) and isinstance(job.get("files"), list):
                    job["files"] = [self._present_path(path) for path in job["files"]]
        return result

    def shots_list(self, run: str | Path) -> list[dict[str, Any]]:
        from .shots import RunShots

        root = self.path(run)
        if self.restricted:
            self._ensure_inside(root / "state.json")
        return RunShots(root).list()

    def shot_get(self, run: str | Path, panel_index: int) -> dict[str, Any]:
        from .shots import RunShots

        root = self.path(run)
        if self.restricted:
            self._ensure_inside(root / "state.json")
        return RunShots(root).get(panel_index)

    def shot_set_mode(self, run: str | Path, panel_index: int, mode: str) -> dict[str, Any]:
        from .shots import RunShots

        root = self.path(run)
        if self.restricted:
            for name in (
                "state.json",
                ".run.lock",
                "panels.json",
                "continuity.json",
                "shot_modes.json",
                "prompts",
                "history",
                "history/mode-changes",
            ):
                self._ensure_inside(root / name)
        return RunShots(root).set_mode(panel_index, mode)

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
        if profile == config.media.image_profile or profile in {"qwen", "qwen-unlock"}:
            return "image"
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
