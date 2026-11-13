"""Project-scoped, versioned visual asset catalog and managed media files.

An asset version is a content hash of its metadata, not an ML model version.
Each operation owns its SQLite connection; write operations use an immediate
transaction so concurrent updates do not read and overwrite stale values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError

from .database.catalog import create_catalog_engine, migrate_catalog


def _version(kind: str, name: str, aliases: str, description: str) -> str:
    encoded = json.dumps(
        {"kind": kind, "name": name, "aliases": aliases, "description": description},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _CatalogConnection:
    """SQLAlchemy connection with mapping rows for the legacy query layer."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement: str, parameters=()):
        # SQLite positional parameters are supplied separately to SQLAlchemy;
        # asset data is never interpolated into SQL strings.
        result = self.connection.exec_driver_sql(statement, tuple(parameters))
        return result.mappings() if result.returns_rows else result

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


class AssetLibrary:
    def __init__(self, path: str | Path, project_id: str = "default"):
        self.path = Path(path).expanduser().resolve()
        self.project_id = _text(project_id, "project_id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file_root = self.path.parent / "files"
        self._catalog_parent = self.path.parent.resolve(strict=True)
        self.engine = create_catalog_engine(self.path)
        self._initialize()

    def close(self) -> None:
        self.engine.dispose()

    def _managed_root(self, *, create: bool = False) -> Path:
        """Validate the managed file directory before every filesystem write."""
        if self.path.parent.resolve(strict=True) != self._catalog_parent:
            raise ValueError("Asset database directory changed after opening the catalog")
        if self.file_root.is_symlink() or getattr(self.file_root, "is_junction", lambda: False)():
            raise ValueError("Managed asset directory must not be a symlink or junction")
        root = self.file_root.resolve(strict=False)
        if root == self._catalog_parent or not root.is_relative_to(self._catalog_parent):
            raise ValueError("Managed asset directory must stay inside the database directory")
        if create:
            self.file_root.mkdir(parents=True, exist_ok=True)
            return self._managed_root()
        return root

    def _remove_temporary(self, path: Path) -> None:
        """Do not follow a directory replaced after a temporary file was made."""
        try:
            root = self._managed_root()
        except (OSError, ValueError):
            return
        if path.parent == root:
            path.unlink(missing_ok=True)

    def _managed_path(self, stored_name: str) -> Path:
        """Resolve a catalog filename without trusting database contents."""
        root = self._managed_root()
        if (
            not isinstance(stored_name, str)
            or not stored_name
            or stored_name in {".", ".."}
            or "/" in stored_name
            or "\\" in stored_name
            or Path(stored_name).is_absolute()
            or Path(stored_name).name != stored_name
        ):
            raise ValueError("Invalid managed asset filename")
        path = root / stored_name
        if path.is_symlink() or path.resolve(strict=False).parent != root:
            raise ValueError("Managed asset filename escapes the managed directory")
        return path

    def _connect(self) -> _CatalogConnection:
        return _CatalogConnection(self.engine.connect())

    def _initialize(self) -> None:
        self.migrate()

    def migrate(self) -> str:
        """Safely upgrade the catalog or adopt a compatible legacy database."""
        return migrate_catalog(self.engine)

    def schema_version(self) -> str:
        with self.engine.connect() as conn:
            value = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one_or_none()
            if value is None:
                raise RuntimeError("Asset catalog has no migration revision")
            return value

    def _get(self, conn: _CatalogConnection, asset_id: str, version: str | None = None) -> dict:
        row = conn.execute(
            """SELECT a.asset_id, a.kind, a.name, a.archived, a.created_at, a.updated_at,
                      v.asset_version, v.aliases, v.description
               FROM assets a JOIN asset_versions v
                 ON v.project_id = a.project_id AND v.asset_id = a.asset_id
                AND v.asset_version = COALESCE(?, a.current_version)
               WHERE a.project_id = ? AND a.asset_id = ?""",
            (version, self.project_id, asset_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown asset or version: {asset_id} / {version or 'current'}")
        item = dict(row)
        item["archived"] = bool(item["archived"])
        return item

    def register(
        self,
        assets: list[dict],
        source_sha256: str = "",
        prompt_version: str = "",
        run_id: str = "",
        reuse_existing: bool = True,
    ) -> list[dict]:
        if not isinstance(assets, list):
            raise ValueError("assets must be a list")
        if any(not isinstance(value, str) for value in (source_sha256, prompt_version, run_id)):
            raise ValueError("source_sha256, prompt_version and run_id must be strings")
        if not isinstance(reuse_existing, bool):
            raise ValueError("reuse_existing must be boolean")
        result = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for asset in assets:
                    if not isinstance(asset, dict):
                        raise ValueError("Each asset must be an object")
                    asset_id = _text(asset.get("asset_id"), "asset_id")
                    kind = _text(asset.get("kind"), "kind")
                    if kind not in {"character", "scene", "prop"}:
                        raise ValueError(f"Unknown asset kind: {kind}")
                    name = _text(asset.get("name"), "name")
                    aliases = _text(asset.get("aliases"), "aliases")
                    description = _text(asset.get("description"), "description")
                    existing = conn.execute(
                        "SELECT * FROM assets WHERE project_id = ? AND kind = ? AND name = ?",
                        (self.project_id, kind, name),
                    ).fetchone()
                    if existing:
                        if existing["asset_id"] != asset_id:
                            raise ValueError(
                                f"Asset ID conflicts with existing {kind} asset {name}"
                            )
                        if existing["archived"]:
                            # A later extraction must not silently resurrect an archived asset.
                            continue
                        if reuse_existing:
                            chosen = existing["current_version"]
                        else:
                            chosen = _version(kind, name, aliases, description)
                            conn.execute(
                                "INSERT OR IGNORE INTO asset_versions (project_id,asset_id,asset_version,aliases,description) VALUES (?,?,?,?,?)",
                                (self.project_id, asset_id, chosen, aliases, description),
                            )
                            conn.execute(
                                "UPDATE assets SET current_version = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id = ? AND asset_id = ?",
                                (chosen, self.project_id, asset_id),
                            )
                    else:
                        chosen = _version(kind, name, aliases, description)
                        try:
                            conn.execute(
                                "INSERT INTO assets (project_id,asset_id,kind,name,current_version) VALUES (?,?,?,?,?)",
                                (self.project_id, asset_id, kind, name, chosen),
                            )
                        except IntegrityError as exc:
                            raise ValueError(
                                f"Asset ID already used in project: {asset_id}"
                            ) from exc
                        conn.execute(
                            "INSERT INTO asset_versions (project_id,asset_id,asset_version,aliases,description) VALUES (?,?,?,?,?)",
                            (self.project_id, asset_id, chosen, aliases, description),
                        )
                    conn.execute(
                        """INSERT OR IGNORE INTO asset_sources
                           (project_id,asset_id,asset_version,source_sha256,prompt_version,run_id)
                           VALUES (?,?,?,?,?,?)""",
                        (self.project_id, asset_id, chosen, source_sha256, prompt_version, run_id),
                    )
                    result.append(self._get(conn, asset_id, chosen))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result

    def get(self, asset_id: str, version: str | None = None) -> dict:
        with closing(self._connect()) as conn:
            return self._get(conn, _text(asset_id, "asset_id"), version)

    def list(
        self, kind: str | None = None, query: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        sql = "SELECT asset_id FROM assets WHERE project_id = ?"
        args: list[Any] = [self.project_id]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if not include_archived:
            sql += " AND archived = 0"
        if query:
            # instr is a literal substring search: %, _ and quotes remain data.
            sql += " AND (instr(name, ?) > 0 OR EXISTS (SELECT 1 FROM asset_versions v WHERE v.project_id = assets.project_id AND v.asset_id = assets.asset_id AND v.asset_version = assets.current_version AND instr(v.aliases, ?) > 0))"
            args.extend([query, query])
        sql += " ORDER BY kind, name, asset_id"
        with closing(self._connect()) as conn:
            return [self._get(conn, row["asset_id"]) for row in conn.execute(sql, args)]

    def update(
        self, asset_id: str, description: str | None = None, aliases: str | None = None
    ) -> dict:
        if description is None and aliases is None:
            raise ValueError("Specify description and/or aliases")
        if description is not None:
            _text(description, "description")
        if aliases is not None:
            _text(aliases, "aliases")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._get(conn, asset_id)
                if old["archived"]:
                    raise ValueError("Restore this asset before editing it")
                new_aliases = old["aliases"] if aliases is None else aliases
                new_desc = old["description"] if description is None else description
                version = _version(old["kind"], old["name"], new_aliases, new_desc)
                conn.execute(
                    "INSERT OR IGNORE INTO asset_versions (project_id,asset_id,asset_version,aliases,description) VALUES (?,?,?,?,?)",
                    (self.project_id, asset_id, version, new_aliases, new_desc),
                )
                conn.execute(
                    "UPDATE assets SET current_version = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id = ? AND asset_id = ?",
                    (version, self.project_id, asset_id),
                )
                item = self._get(conn, asset_id)
                conn.commit()
                return item
            except Exception:
                conn.rollback()
                raise

    def versions(self, asset_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            self._get(conn, asset_id)
            rows = conn.execute(
                """SELECT asset_version, aliases, description, created_at FROM asset_versions
                   WHERE project_id = ? AND asset_id = ? ORDER BY rowid""",
                (self.project_id, asset_id),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["sources"] = [
                    dict(x)
                    for x in conn.execute(
                        """SELECT source_sha256, prompt_version, run_id, created_at FROM asset_sources
                       WHERE project_id = ? AND asset_id = ? AND asset_version = ? ORDER BY rowid""",
                        (self.project_id, asset_id, item["asset_version"]),
                    )
                ]
                result.append(item)
            return result

    def _set_archived(self, asset_id: str, archived: bool) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._get(conn, asset_id)
                conn.execute(
                    "UPDATE assets SET archived = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id = ? AND asset_id = ?",
                    (int(archived), self.project_id, asset_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def archive(self, asset_id: str) -> None:
        self._set_archived(asset_id, True)

    def restore(self, asset_id: str) -> None:
        self._set_archived(asset_id, False)

    def link_file(
        self,
        asset_id: str,
        path: str | Path,
        role: str = "reference",
        asset_version: str | None = None,
    ) -> dict:
        role = _text(role, "role")
        src = Path(path).expanduser().resolve(strict=True)
        if not src.is_file():
            raise ValueError(f"Asset media is not a file: {src}")
        # Validate before making a copy, then revalidate under the transaction.
        asset = self.get(asset_id, asset_version)
        version = asset["asset_version"]
        root = self._managed_root(create=True)
        digest = hashlib.sha256()
        suffix = src.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ""
        self._managed_root()
        fd, temp_name = tempfile.mkstemp(prefix=".asset-", dir=root)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as out, src.open("rb") as incoming:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    digest.update(block)
                    out.write(block)
            sha = digest.hexdigest()
            stored = sha + suffix
            destination = self._managed_path(stored)
            if destination.exists():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or _file_hash(destination) != sha
                ):
                    raise ValueError(f"Existing managed file failed integrity check: {destination}")
            else:
                self._managed_root()
                os.replace(temp, destination)
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._get(conn, asset_id, version)
                    conn.execute(
                        """INSERT OR IGNORE INTO asset_files
                           (project_id,asset_id,asset_version,role,sha256,original_name,stored_name)
                           VALUES (?,?,?,?,?,?,?)""",
                        (self.project_id, asset_id, version, role, sha, src.name, stored),
                    )
                    row = conn.execute(
                        """SELECT asset_id,asset_version,role,sha256,original_name,stored_name,created_at
                           FROM asset_files WHERE project_id=? AND asset_id=? AND asset_version=? AND role=? AND sha256=?""",
                        (self.project_id, asset_id, version, role, sha),
                    ).fetchone()
                    item = dict(row)
                    linked_path = self._managed_path(item["stored_name"])
                    if linked_path != destination:
                        if not linked_path.exists():
                            self._managed_root()
                            fd2, name2 = tempfile.mkstemp(prefix=".asset-", dir=root)
                            try:
                                with (
                                    os.fdopen(fd2, "wb") as output,
                                    destination.open("rb") as incoming,
                                ):
                                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                                        output.write(block)
                                self._managed_root()
                                os.replace(name2, linked_path)
                            finally:
                                self._remove_temporary(Path(name2))
                        if linked_path.is_symlink() or _file_hash(linked_path) != sha:
                            raise ValueError(
                                f"Existing managed file failed integrity check: {linked_path}"
                            )
                    conn.execute(
                        """INSERT INTO asset_file_selections (project_id,asset_id,asset_version,role,sha256)
                           VALUES (?,?,?,?,?)""",
                        (self.project_id, asset_id, version, role, sha),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            item["local_path"] = str(linked_path)
            return item
        finally:
            self._remove_temporary(temp)

    def files(
        self, asset_id: str, asset_version: str | None = None, role: str | None = None
    ) -> list[dict]:
        self._managed_root()
        with closing(self._connect()) as conn:
            self._get(conn, asset_id, asset_version)
            sql = "SELECT asset_id,asset_version,role,sha256,original_name,stored_name,created_at FROM asset_files WHERE project_id=? AND asset_id=?"
            args: list[Any] = [self.project_id, asset_id]
            if asset_version is not None:
                sql += " AND asset_version = ?"
                args.append(asset_version)
            if role is not None:
                sql += " AND role = ?"
                args.append(role)
            sql += """ ORDER BY COALESCE((SELECT MAX(s.id) FROM asset_file_selections s
                       WHERE s.project_id=asset_files.project_id AND s.asset_id=asset_files.asset_id
                         AND s.asset_version=asset_files.asset_version AND s.role=asset_files.role
                         AND s.sha256=asset_files.sha256), 0), asset_files.rowid"""
            result = []
            for row in conn.execute(sql, args):
                item = dict(row)
                item["local_path"] = str(self._managed_path(item["stored_name"]))
                result.append(item)
            return result

    def record_usage(
        self, asset_id: str, run_id: str, panel_index: int, asset_version: str | None = None
    ) -> None:
        _text(run_id, "run_id")
        if type(panel_index) is not int or panel_index < 0:
            raise ValueError("panel_index must be a nonnegative integer")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._get(conn, asset_id, asset_version)["asset_version"]
                conn.execute(
                    """INSERT OR IGNORE INTO asset_usage (project_id,asset_id,asset_version,run_id,panel_index)
                       VALUES (?,?,?,?,?)""",
                    (self.project_id, asset_id, version, run_id, panel_index),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def export(self) -> dict:
        assets = self.list(include_archived=True)
        for asset in assets:
            asset["versions"] = self.versions(asset["asset_id"])
            asset["files"] = self.files(asset["asset_id"])
        return {"schema_version": 1, "project_id": self.project_id, "assets": assets}

    def import_data(self, data: dict | list) -> list[dict]:
        """Import asset metadata and files, retaining history when supplied by export().

        Exported media paths must be locally accessible. Importing to another
        project creates independent records and copies the referenced media.
        """
        assets = data.get("assets") if isinstance(data, dict) else data
        if not isinstance(assets, list):
            raise ValueError("Expected an asset list or exported {assets:[...]} object")
        # Validate every record and its media before making any database changes.
        for asset in assets:
            if not isinstance(asset, dict):
                raise ValueError("Each asset must be an object")
            asset_id = _text(asset.get("asset_id"), "asset_id")
            kind = _text(asset.get("kind"), "kind")
            if kind not in {"character", "scene", "prop"}:
                raise ValueError(f"Unknown asset kind: {kind}")
            name = _text(asset.get("name"), "name")
            aliases = _text(asset.get("aliases"), "aliases")
            description = _text(asset.get("description"), "description")
            current_version = _version(kind, name, aliases, description)
            if asset.get("asset_version") and asset["asset_version"] != current_version:
                raise ValueError(f"Current asset version content does not match hash: {asset_id}")
            known_versions = {current_version}
            for version in asset.get("versions", []):
                if not isinstance(version, dict):
                    raise ValueError("Each asset version must be an object")
                calculated = _version(
                    kind,
                    name,
                    _text(version.get("aliases"), "aliases"),
                    _text(version.get("description"), "description"),
                )
                if version.get("asset_version") and version["asset_version"] != calculated:
                    raise ValueError(f"Asset version content does not match hash: {asset_id}")
                known_versions.add(calculated)
                for source in version.get("sources", []):
                    if not isinstance(source, dict) or any(
                        not isinstance(source.get(key, ""), str)
                        for key in ("source_sha256", "prompt_version", "run_id")
                    ):
                        raise ValueError("Invalid asset provenance")
            for media in asset.get("files", []):
                if not isinstance(media, dict) or not Path(media.get("local_path", "")).is_file():
                    raise ValueError("Exported asset media is missing or invalid")
                if media.get("asset_version") and media["asset_version"] not in known_versions:
                    raise ValueError(f"Asset file refers to unknown version: {asset_id}")
                if media.get("sha256") and _file_hash(Path(media["local_path"])) != media["sha256"]:
                    raise ValueError(f"Asset file hash mismatch: {asset_id}")
        # Stage files before acquiring the write lock. Their names are content
        # hashes; failed imports may leave unreferenced copies but no catalog rows.
        staged = {}
        for index, asset in enumerate(assets):
            for media_index, media in enumerate(asset.get("files", [])):
                src = Path(media["local_path"]).expanduser().resolve(strict=True)
                role = _text(media.get("role", "reference"), "role")
                suffix = src.suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
                    suffix = ""
                root = self._managed_root(create=True)
                self._managed_root()
                fd, temp_name = tempfile.mkstemp(prefix=".asset-", dir=root)
                temporary = Path(temp_name)
                digest = hashlib.sha256()
                try:
                    with os.fdopen(fd, "wb") as output, src.open("rb") as incoming:
                        for block in iter(lambda: incoming.read(1024 * 1024), b""):
                            digest.update(block)
                            output.write(block)
                    sha = digest.hexdigest()
                    if media.get("sha256") and sha != media["sha256"]:
                        raise ValueError(f"Asset file hash mismatch: {asset['asset_id']}")
                    stored = sha + suffix
                    destination = self._managed_path(stored)
                    if destination.exists():
                        if (
                            destination.is_symlink()
                            or not destination.is_file()
                            or _file_hash(destination) != sha
                        ):
                            raise ValueError(
                                f"Existing managed file failed integrity check: {destination}"
                            )
                    else:
                        self._managed_root()
                        os.replace(temporary, destination)
                    staged[index, media_index] = (
                        role,
                        sha,
                        stored,
                        media.get("original_name") or src.name,
                    )
                finally:
                    self._remove_temporary(temporary)

        result = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for index, asset in enumerate(assets):
                    asset_id, kind, name = asset["asset_id"], asset["kind"], asset["name"]
                    current = _version(kind, name, asset["aliases"], asset["description"])
                    by_id = conn.execute(
                        "SELECT kind,name FROM assets WHERE project_id=? AND asset_id=?",
                        (self.project_id, asset_id),
                    ).fetchone()
                    by_name = conn.execute(
                        "SELECT asset_id FROM assets WHERE project_id=? AND kind=? AND name=?",
                        (self.project_id, kind, name),
                    ).fetchone()
                    if by_id and (by_id["kind"], by_id["name"]) != (kind, name):
                        raise ValueError(f"Asset ID already used in project: {asset_id}")
                    if by_name and by_name["asset_id"] != asset_id:
                        raise ValueError(f"Asset ID conflicts with existing {kind} asset {name}")
                    if not by_id:
                        conn.execute(
                            "INSERT INTO assets (project_id,asset_id,kind,name,current_version) VALUES (?,?,?,?,?)",
                            (self.project_id, asset_id, kind, name, current),
                        )
                    versions = list(asset.get("versions") or [])
                    # Ensure current is present even when omitted from historical list.
                    versions.append(
                        {"aliases": asset["aliases"], "description": asset["description"]}
                    )
                    for version in versions:
                        calculated = _version(
                            kind, name, version["aliases"], version["description"]
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO asset_versions (project_id,asset_id,asset_version,aliases,description) VALUES (?,?,?,?,?)",
                            (
                                self.project_id,
                                asset_id,
                                calculated,
                                version["aliases"],
                                version["description"],
                            ),
                        )
                        for source in version.get("sources", []):
                            conn.execute(
                                """INSERT OR IGNORE INTO asset_sources
                                   (project_id,asset_id,asset_version,source_sha256,prompt_version,run_id)
                                   VALUES (?,?,?,?,?,?)""",
                                (
                                    self.project_id,
                                    asset_id,
                                    calculated,
                                    source.get("source_sha256", ""),
                                    source.get("prompt_version", ""),
                                    source.get("run_id", ""),
                                ),
                            )
                    conn.execute(
                        """UPDATE assets SET current_version=?, archived=?,
                           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                           WHERE project_id=? AND asset_id=?""",
                        (current, int(bool(asset.get("archived"))), self.project_id, asset_id),
                    )
                    for media_index, media in enumerate(asset.get("files", [])):
                        role, sha, stored, original_name = staged[index, media_index]
                        version = media.get("asset_version") or current
                        conn.execute(
                            """INSERT INTO asset_files
                               (project_id,asset_id,asset_version,role,sha256,original_name,stored_name)
                               VALUES (?,?,?,?,?,?,?)
                               ON CONFLICT (project_id,asset_id,asset_version,role,sha256)
                               DO UPDATE SET stored_name=excluded.stored_name""",
                            (self.project_id, asset_id, version, role, sha, original_name, stored),
                        )
                        conn.execute(
                            """INSERT INTO asset_file_selections
                               (project_id,asset_id,asset_version,role,sha256) VALUES (?,?,?,?,?)""",
                            (self.project_id, asset_id, version, role, sha),
                        )
                    result.append(self._get(conn, asset_id, current))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result
