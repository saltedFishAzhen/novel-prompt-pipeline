"""Resolve ComfyUI server pools and assign new work without retrying submissions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from .config import ComfyUIConfig, Config
from .storage import fingerprint, write_json

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_POOL_ENV = {
    "image": "COMFYUI_IMAGE_SERVERS",
    "ref2va": "COMFYUI_REF2VA_SERVERS",
    "i2va": "COMFYUI_I2VA_SERVERS",
}


def _url_entries() -> dict[str, str]:
    value = os.getenv("COMFYUI_URLS", "").strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value) if value.startswith(("{", "[")) else value.split(",")
    except json.JSONDecodeError:
        raise ValueError(
            "COMFYUI_URLS must be a JSON mapping, JSON list or comma-separated URLs"
        ) from None
    if isinstance(parsed, list):
        entries = {f"server{i}": url for i, url in enumerate(parsed, 1)}
    elif isinstance(parsed, dict):
        entries = parsed
    else:
        raise ValueError("COMFYUI_URLS must be a JSON mapping, JSON list or comma-separated URLs")
    if not entries or any(not isinstance(url, str) or not url.strip() for url in entries.values()):
        raise ValueError("COMFYUI_URLS entries must be nonempty URL strings")
    return {name: url.strip() for name, url in entries.items()}


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            "ComfyUI server names must contain letters, digits, underscores or hyphens"
        )


def _key_env(name: str) -> str:
    return "COMFYUI_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_API_KEY"


class ComfyRouter:
    """Resolve named servers and provide process-safe round-robin assignment.

    URL values and credentials remain in memory. Only endpoint hashes and
    aliases are written to run records or to the shared round-robin cursor.
    """

    def __init__(self, config: Config):
        self.config = config
        self.settings = config.comfyui or ComfyUIConfig()
        self.legacy_url = os.getenv("COMFYUI_URL") or os.getenv("COMFYUI_BASE_URL", "")
        self._entries = _url_entries()
        self._names = set(self._entries) | set(self.settings.servers)
        for name in self._names:
            _check_name(name)
        normalized = [_key_env(name) for name in self._names]
        if len(normalized) != len(set(normalized)):
            raise ValueError("ComfyUI server names must have distinct API-key environment names")
        self.names = tuple(sorted(self._names))
        self._urls = {}
        for name in self.names:
            settings = self.settings.servers.get(name)
            value = (
                os.getenv(settings.url_env, "")
                if settings and settings.url_env
                else self._entries.get(name, "")
            )
            self._urls[name] = value.strip().rstrip("/")
        self.default_server = (
            self.settings.default_server or os.getenv("COMFYUI_DEFAULT_SERVER") or None
        )
        self.balance = os.getenv("COMFYUI_BALANCE") or self.settings.balance
        if self.balance != "round_robin":
            raise ValueError("COMFYUI_BALANCE currently supports only round_robin")
        self.pools = {}
        for stage, env_name in _POOL_ENV.items():
            values = getattr(self.settings, stage + "_servers")
            pool = (
                list(values)
                if values
                else [part.strip() for part in os.getenv(env_name, "").split(",") if part.strip()]
            )
            if len(pool) != len(set(pool)):
                raise ValueError(f"Duplicate server in {stage} pool")
            self.pools[stage] = tuple(pool)
        for name in ([self.default_server] if self.default_server else []) + [
            name for pool in self.pools.values() for name in pool
        ]:
            if name not in self._names:
                raise ValueError(f"Unknown ComfyUI server: {name}")
        self.cursor_path = Path(config.asset_library).resolve().parent / "comfyui-routing.json"
        self.upload_path = self.cursor_path.with_name("comfyui-upload-locations.json")
        self.job_path = self.cursor_path.with_name("comfyui-job-locations.json")

    @property
    def named(self) -> bool:
        return bool(self.names)

    def pool(self, stage: str) -> tuple[str, ...]:
        if stage not in self.pools:
            raise ValueError("Unknown ComfyUI generation stage")
        return self.pools[stage] or ((self.default_server,) if self.default_server else self.names)

    def url(self, server: str, *, required: bool = True) -> str:
        if server not in self._names:
            raise ValueError(f"Unknown ComfyUI server: {server}")
        url = self._urls[server]
        if not url:
            if required:
                raise ValueError(f"URL environment variable is not set for ComfyUI server {server}")
            return ""
        try:
            parts = urlsplit(url)
            invalid = (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            )
            parts.port  # Reject malformed or out-of-range ports without exposing the URL.
        except ValueError:
            invalid = True
        if invalid:
            raise ValueError(f"Invalid URL for ComfyUI server {server}")
        return url

    def endpoint_hash(self, server: str, *, required: bool = True) -> str:
        return hashlib.sha256(self.url(server, required=required).encode("utf-8")).hexdigest()

    def legacy_endpoint_hash(self) -> str:
        return hashlib.sha256(self.legacy_url.rstrip("/").encode("utf-8")).hexdigest()

    def credentials(self, server: str) -> str | None:
        if server not in self._names:
            raise ValueError(f"Unknown ComfyUI server: {server}")
        settings = self.settings.servers.get(server)
        env_name = (
            settings.api_key_env
            if settings and settings.api_key_env is not None
            else _key_env(server)
        )
        return os.getenv(env_name) or None if env_name else None

    def identity(self) -> dict:
        return {
            "balance": self.balance,
            "servers": {name: self.endpoint_hash(name, required=False) for name in self.names},
            "pools": {stage: list(self.pool(stage)) for stage in self.pools},
            "default_server": self.default_server,
        }

    def choose(self, stage: str, *, server: str | None = None) -> str | None:
        if not self.named:
            if server not in (None, "default"):
                raise ValueError(f"Unknown ComfyUI server: {server}")
            return None
        if server is not None:
            if server not in self._names:
                raise ValueError(f"Unknown ComfyUI server: {server}")
            self.url(server)
            return server
        pool = self.pool(stage)
        if not pool:
            raise ValueError(f"No ComfyUI servers configured for {stage}")
        for name in pool:
            self.url(name)
        if len(pool) == 1:
            return pool[0]
        key = fingerprint(
            {"stage": stage, "pool": [(name, self.endpoint_hash(name)) for name in pool]}
        )
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.cursor_path.with_suffix(self.cursor_path.suffix + ".lock")
        if self.cursor_path.is_symlink() or lock_path.is_symlink():
            raise ValueError("ComfyUI routing state cannot be a symbolic link")
        with FileLock(str(lock_path), timeout=30):
            if self.cursor_path.is_symlink():
                raise ValueError("ComfyUI routing state cannot be a symbolic link")
            if self.cursor_path.exists():
                try:
                    cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise ValueError("ComfyUI routing state cannot be read") from None
                if not isinstance(cursor, dict) or not all(
                    isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                    for k, v in cursor.items()
                ):
                    raise ValueError("ComfyUI routing state is invalid")
            else:
                cursor = {}
            counter = cursor.get(key, 0)
            cursor[key] = counter + 1
            write_json(self.cursor_path, cursor)
        return pool[counter % len(pool)]

    def _upload_locations(
        self, remote_name: str, *, server: str | None = None, add: bool = False
    ) -> list[dict]:
        return self._location_entries(self.upload_path, remote_name, server=server, add=add)

    def _location_entries(
        self, path: Path, identifier: str, *, server: str | None = None, add: bool = False
    ) -> list[dict]:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        if path.is_symlink() or lock_path.is_symlink():
            raise ValueError("ComfyUI location registry cannot be a symbolic link")
        with FileLock(str(lock_path), timeout=30):
            if path.is_symlink():
                raise ValueError("ComfyUI location registry cannot be a symbolic link")
            if path.exists():
                try:
                    registry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise ValueError("ComfyUI location registry cannot be read") from None
                if not isinstance(registry, dict):
                    raise ValueError("ComfyUI location registry is invalid")
            else:
                registry = {}
            key = fingerprint(identifier)
            entries = registry.get(key, [])
            if not isinstance(entries, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("server"), str)
                or not isinstance(item.get("url_sha256"), str)
                for item in entries
            ):
                raise ValueError("ComfyUI location registry is invalid")
            if add:
                entry = self._upload_identity(server)
                if entry not in entries:
                    registry[key] = [*entries, entry]
                    write_json(path, registry)
            return entries

    def _upload_identity(self, server: str | None) -> dict[str, str]:
        if server is None:
            return {"server": "_legacy", "url_sha256": self.legacy_endpoint_hash()}
        return {"server": server, "url_sha256": self.endpoint_hash(server)}

    def record_upload(self, server: str | None, remote_name: str) -> None:
        self._upload_locations(remote_name, server=server, add=True)

    def record_job(self, server: str | None, prompt_id: str) -> None:
        self._location_entries(self.job_path, prompt_id, server=server, add=True)

    def job_locations(self, prompt_id: str) -> list[dict]:
        return self._location_entries(self.job_path, prompt_id)

    def verify_upload_server(self, server: str | None, remote_name: str) -> None:
        """Reject known uploads on another endpoint; unknown remote files are caller-owned."""
        entries = self._upload_locations(remote_name)
        if entries and self._upload_identity(server) not in entries:
            raise ValueError("Referenced media was uploaded to a different ComfyUI server or URL")

    def client(self, server: str | None):
        from comfyui_client import ComfyUIClient

        media = self.config.media
        if server is None:
            if not self.legacy_url:
                raise ValueError("Set COMFYUI_URL before accessing the remote server")
            return ComfyUIClient(
                self.legacy_url,
                api_key=os.getenv("COMFYUI_API_KEY") or None,
                timeout=media.timeout,
                poll_interval=media.poll_interval,
            )
        credential = self.credentials(server)
        client = ComfyUIClient(
            self.url(server),
            api_key=credential,
            timeout=media.timeout,
            poll_interval=media.poll_interval,
        )
        # The lower-level client's legacy default falls back to COMFYUI_API_KEY
        # when api_key is falsey. Named servers must never inherit that secret.
        client.api_key = credential
        return client
