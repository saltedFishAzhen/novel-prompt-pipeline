"""Remote ComfyUI execution and persisted job state."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from comfyui_client import get_default_template

from .comfy_servers import ComfyRouter
from .config import Config
from .storage import fingerprint, write_json


class RemoteExecutor:
    def __init__(self, config: Config, *, comfy=None):
        self.config = config
        self.comfy = comfy
        self._owns_comfy = comfy is None
        self._router = ComfyRouter(config)
        self._clients = {}
        self._active_server = None
        self.state = {}
        self.root = None
        self.release = None
        self.library = None
        if config.media.reference_audios:
            raise ValueError("Video reference audio is not supported")
        if config.media.image_backend != "codex":
            raise ValueError("Image backend must be codex")
        from .images.backend import CodexImageBackend

        self.image_template = CodexImageBackend()
        if config.media.video_profile == "i2va":
            raise ValueError(
                "video_profile must provide additional image references; configure i2va_profile separately"
            )
        self.video_template = self._template(config.media.video_profile)
        self.i2va_template = self._template(config.media.i2va_profile)
        if not self.video_template.has_first_frame:
            raise ValueError("Video workflow must expose a first_frame binding")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Release resources created by this pipeline; injected clients are caller-owned."""
        if self.library is not None:
            close = getattr(self.library, "close", None)
            if close:
                close()
            self.library = None
        if self._owns_comfy and self.comfy is not None:
            close = getattr(self.comfy, "close", None)
            if close:
                close()
            self.comfy = None
        if self._owns_comfy:
            for client in self._clients.values():
                close = getattr(client, "close", None)
                if close:
                    close()
            self._clients.clear()

    def _template(self, profile):
        if (
            profile in {"ref2va", "i2va"}
            and not self.config.media.bindings_file
            and not self.config.media.workflows_dir
        ):
            return get_default_template(profile)
        from comfyui_client import WorkflowTemplate

        return WorkflowTemplate.from_profile(
            profile,
            bindings_file=self.config.media.bindings_file,
            workflows_dir=self.config.media.workflows_dir,
        )

    def _mode_template(self, mode):
        if mode == "ref2va":
            return self.video_template
        if mode == "i2va":
            if not self.i2va_template.has_first_frame:
                raise ValueError("I2VA workflow must expose a true first_frame input")
            if self.i2va_template.reference_capacity:
                raise ValueError("I2VA profile cannot advertise unsupported asset reference inputs")
            return self.i2va_template
        raise ValueError(f"Unknown video mode: {mode}")

    def _activate_server(self, stage, key, *, preferred=None):
        """Pin the server before uploads; retries reuse the persisted assignment."""
        if not self._router.named:
            if preferred is not None:
                raise ValueError("A remote audio server requires named ComfyUI servers")
            self._active_server = None
            return None
        pool = self._router.pool(stage)
        if preferred is not None and preferred not in pool:
            raise ValueError(f"ComfyUI server {preferred} is not in the {stage} pool")
        assignments = self.state.setdefault("server_assignments", {})
        record = assignments.get(key)
        media = self.state.get("media", {})
        keys = [key]
        if key.startswith("scene_"):
            asset_key = hashlib.sha256(key[len("scene_") :].encode("utf-8")).hexdigest()[:24]
            keys.extend(name for name in media if name.startswith(f"scene_{asset_key}_"))
        existing_jobs = [media[name] for name in keys if name in media]
        if record and record.get("stage") != stage and not existing_jobs and key.endswith("_video"):
            # Mode may be changed before a video job is submitted. Prior
            # uploads are already scoped by server and can safely remain.
            assignments.pop(key)
            record = None
        if record is None and existing_jobs:
            raise ValueError("A saved media task has no pinned ComfyUI server assignment")
        if record is None:
            server = self._router.choose(stage, server=preferred)
            record = {
                "stage": stage,
                "server": server,
                "url_sha256": self._router.endpoint_hash(server),
            }
            assignments[key] = record
            self._save()
        else:
            server = record.get("server")
            if (
                record.get("stage") != stage
                or server not in pool
                or (preferred is not None and server != preferred)
                or record.get("url_sha256") != self._router.endpoint_hash(server)
            ):
                raise ValueError("ComfyUI server assignment changed; create a new run")
            if any(
                job.get("server") != server or job.get("url_sha256") != record["url_sha256"]
                for job in existing_jobs
            ):
                raise ValueError("Saved media task disagrees with its ComfyUI server assignment")
        self._active_server = server
        return server

    def _client(self):
        if self._router.named:
            if self._active_server is None:
                raise RuntimeError("A ComfyUI server must be selected before accessing media")
            if self.comfy is not None:
                if isinstance(self.comfy, Mapping):
                    if self._active_server not in self.comfy:
                        raise ValueError("No injected client for the selected ComfyUI server")
                    return self.comfy[self._active_server]
                if len(self._router.names) > 1:
                    raise ValueError("Multiple servers require a mapping of injected clients")
                return self.comfy
            if self._active_server not in self._clients:
                self._clients[self._active_server] = self._router.client(self._active_server)
            return self._clients[self._active_server]
        if self.comfy is None:
            self.comfy = self._router.client(None)
        return self.comfy

    def _job(self, key, graph):
        if self._router.named and self._active_server is None:
            self._activate_server("image", key)
        client = self._client()
        entry = self.state["media"].get(key)
        digest = fingerprint(graph)
        # Imported asset IDs are opaque identifiers, never filesystem paths.
        safe_key = (
            key
            if re.fullmatch(r"[A-Za-z0-9_-]+", key)
            else hashlib.sha256(key.encode("utf-8")).hexdigest()
        )
        directory = self.root / "media" / safe_key
        if entry:
            if self._router.named and (
                entry.get("server") != self._active_server
                or entry.get("url_sha256") != self._router.endpoint_hash(self._active_server)
            ):
                raise ValueError(f"ComfyUI server changed for existing media task {key}")
            if entry["graph_sha256"] != digest:
                raise ValueError(f"Workflow changed for existing media task {key}")
            if entry.get("status") == "submitting":
                raise RuntimeError(
                    f"Submission outcome unknown for {key}; check the remote queue before recording its prompt_id. No duplicate job was sent."
                )
            if entry.get("files") and all(
                Path(x).is_file()
                and not Path(x).is_symlink()
                and self._file_hash(x) == entry.get("file_hashes", {}).get(x)
                for x in entry["files"]
            ):
                return entry["files"]
        else:
            entry = {"graph_sha256": digest, "status": "submitting"}
            if self._router.named:
                entry.update(
                    server=self._active_server,
                    url_sha256=self._router.endpoint_hash(self._active_server),
                )
            self.state["media"][key] = entry
            write_json(directory / "workflow.json", graph)
            self._save()
            # Mark before POST. A lost response must not cause an automatic duplicate.
            prompt_id = client.submit(graph)
            entry.update(prompt_id=prompt_id, status="submitted")
            self._save()
        if self._router.named or self._router.legacy_url:
            self._router.record_job(self._active_server, entry["prompt_id"])
        history = client.wait(entry["prompt_id"], timeout=self.config.media.timeout)
        write_json(directory / "history.json", history)
        files = [
            str(Path(x).resolve()) for x in client.download_outputs(history, directory / "outputs")
        ]
        if not files:
            raise RuntimeError(
                f"Remote task {entry['prompt_id']} completed without downloadable files"
            )
        entry.update(
            status="completed", files=files, file_hashes={x: self._file_hash(x) for x in files}
        )
        self._save()
        return files

    def _upload(self, path, *, audio=False):
        digest = self._file_hash(path)
        key = ("audio:" if audio else "image:") + digest
        if self._router.named:
            if self._active_server is None:
                raise RuntimeError("A ComfyUI server must be selected before uploading media")
            server_key = fingerprint(
                {
                    "name": self._active_server,
                    "url": self._router.endpoint_hash(self._active_server),
                }
            )
            key = "server:" + server_key + ":" + key
        uploads = self.state.setdefault("uploads", {})
        if key not in uploads:
            client = self._client()
            uploads[key] = client.upload_file(path) if audio else client.upload_image(path)
            self._save()
        return uploads[key]

    @staticmethod
    def _file_hash(path):
        with Path(path).open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def _verify_media(self, record):
        path = Path(record["local_path"])
        if not path.is_file() or path.is_symlink() or self._file_hash(path) != record["sha256"]:
            raise ValueError(f"Managed asset reference failed integrity check: {path}")

    @staticmethod
    def _first_image(paths):
        for path in paths:
            if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                return path
        raise RuntimeError("Image workflow did not return a downloadable image")

    def _save(self):
        write_json(self.root / "state.json", self.state)
