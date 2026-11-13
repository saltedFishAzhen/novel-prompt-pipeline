from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class PromptReleaseError(ValueError):
    """Raised when a prompt release is missing, corrupt, or misused."""


class PromptRelease:
    """Load an immutable prompt release and safely render a named node."""

    def __init__(self, project_root: str | Path, version: str):
        self.project_root = Path(project_root).resolve()
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise PromptReleaseError("Invalid prompt release version")
        self.version = version
        release_dir = self.project_root / "releases" / version
        if not release_dir.resolve().is_relative_to(self.project_root):
            raise PromptReleaseError("Prompt release directory escapes its project root")
        if not release_dir.is_dir():
            raise PromptReleaseError(f"Prompt release not found: {version}")
        manifest_path = release_dir / "manifest.json"
        if not manifest_path.resolve().is_relative_to(release_dir.resolve()):
            raise PromptReleaseError("Prompt manifest escapes its release directory")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptReleaseError(f"Invalid release manifest: {manifest_path}") from exc
        if manifest.get("version") != version:
            raise PromptReleaseError("Manifest version does not match release directory")
        self.manifest: dict[str, Any] = manifest
        self.release_dir = release_dir
        self.root = release_dir
        self.directory = release_dir
        self._texts: dict[str, str] = {}
        self._verify_files()

    @property
    def project_root(self) -> Path:
        return self._project_root

    @project_root.setter
    def project_root(self, value: Path) -> None:
        self._project_root = value

    @classmethod
    def from_directory(cls, release_dir: str | Path) -> PromptRelease:
        directory = Path(release_dir).resolve()
        manifest = directory / "manifest.json"
        if not manifest.resolve().is_relative_to(directory):
            raise PromptReleaseError("Prompt snapshot manifest escapes its directory")
        try:
            version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PromptReleaseError(f"Cannot load prompt snapshot: {directory}") from exc
        instance = cls.__new__(cls)
        instance._project_root = directory.parent.parent
        instance.version = version
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise PromptReleaseError(f"Invalid prompt snapshot version: {version!r}")
        instance.release_dir = directory
        instance.root = directory
        instance.directory = directory
        instance.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        if instance.manifest.get("version") != version:
            raise PromptReleaseError("Snapshot manifest version mismatch")
        instance._texts = {}
        instance._verify_files()
        return instance

    @classmethod
    def load(cls, project_root: str | Path, version: str | None = None) -> PromptRelease:
        root = Path(project_root).resolve()
        if version is None or version == "latest":
            pointer_path = root / "current.json"
            if not pointer_path.resolve().is_relative_to(root):
                raise PromptReleaseError("Prompt release pointer escapes its project root")
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                version = pointer["version"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise PromptReleaseError(
                    f"Cannot resolve latest prompt release at {pointer_path}"
                ) from exc
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise PromptReleaseError(f"Invalid prompt release version: {version!r}")
        return cls(root, version)

    def _verify_files(self) -> None:
        payload = {
            "assets": self.manifest.get("assets", {}),
            "nodes": self.manifest.get("nodes", {}),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected_version = hashlib.sha256(canonical).hexdigest()
        if expected_version != self.version:
            raise PromptReleaseError(
                "Prompt release manifest content does not match its version hash"
            )
        files: dict[str, Any] = {}
        files.update(self.manifest.get("assets", {}))
        files.update(self.manifest.get("nodes", {}))
        for key, entry in files.items():
            relative = Path(entry["file"])
            windows = PureWindowsPath(entry["file"])
            posix = PurePosixPath(entry["file"])
            if (
                relative.is_absolute()
                or windows.drive
                or ".." in windows.parts
                or ".." in posix.parts
            ):
                raise PromptReleaseError(f"Unsafe release file path for {key}")
            path = self.release_dir / relative
            if not path.resolve().is_relative_to(self.release_dir.resolve()):
                raise PromptReleaseError(f"Prompt file escapes its release directory: {key}")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise PromptReleaseError(f"Missing prompt file for {key}: {relative}") from exc
            digest = hashlib.sha256(content).hexdigest()
            if digest != entry.get("sha256"):
                raise PromptReleaseError(f"Prompt release checksum mismatch for {key}")
            self._texts[key] = content.decode("utf-8")

    def _entry(self, node_id: str) -> dict[str, Any]:
        try:
            return self.manifest["nodes"][node_id]
        except KeyError as exc:
            raise PromptReleaseError(f"Unknown prompt node: {node_id}") from exc

    def text(self, node_id: str) -> str:
        self._entry(node_id)
        return self._texts[node_id]

    def render(self, node_id: str, variables: Mapping[str, Any]) -> str:
        entry = self._entry(node_id)
        expected = entry.get("variables", [])
        supplied = set(variables)
        missing = sorted(set(expected) - supplied)
        extra = sorted(supplied - set(expected))
        if missing:
            raise PromptReleaseError(f"Missing variables for {node_id}: {', '.join(missing)}")
        if extra:
            raise PromptReleaseError(f"Unexpected variables for {node_id}: {', '.join(extra)}")
        text = self._texts[node_id]
        pattern = re.compile(r"\{\{([\w.:-]+)\}\}")
        seen: set[str] = set()

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise PromptReleaseError(
                    f"Template placeholder {{{{{key}}}}} has no declared input in {node_id}"
                )
            value = variables[key]
            if not isinstance(value, (str, int, float, bool)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            seen.add(key)
            return str(value)

        rendered = pattern.sub(substitute, text)
        if seen != set(expected):
            unused = sorted(set(expected) - seen)
            raise PromptReleaseError(
                f"Declared variables not used by {node_id}: {', '.join(unused)}"
            )
        return rendered
