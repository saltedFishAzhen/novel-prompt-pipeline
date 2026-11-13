"""Migrate and validate the versioned SQLite asset catalog."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.pool import NullPool

BASELINE = "0001_asset_library"
FILE_SELECTION_REVISION = "0002_file_selections"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"

# Required legacy schema. Extra columns are safe only when writes can omit them.
_COLUMNS = {
    "assets": (
        "project_id",
        "asset_id",
        "kind",
        "name",
        "current_version",
        "archived",
        "created_at",
        "updated_at",
    ),
    "asset_versions": (
        "project_id",
        "asset_id",
        "asset_version",
        "aliases",
        "description",
        "created_at",
    ),
    "asset_sources": (
        "project_id",
        "asset_id",
        "asset_version",
        "source_sha256",
        "prompt_version",
        "run_id",
        "created_at",
    ),
    "asset_files": (
        "project_id",
        "asset_id",
        "asset_version",
        "role",
        "sha256",
        "original_name",
        "stored_name",
        "created_at",
    ),
    "asset_usage": (
        "project_id",
        "asset_id",
        "asset_version",
        "run_id",
        "panel_index",
        "created_at",
    ),
}
_PRIMARY_KEYS = {
    "assets": ("project_id", "asset_id"),
    "asset_versions": ("project_id", "asset_id", "asset_version"),
    "asset_sources": (),
    "asset_files": (),
    "asset_usage": (),
}
_UNIQUE_KEYS = {
    "assets": ("project_id", "kind", "name"),
    "asset_sources": (
        "project_id",
        "asset_id",
        "asset_version",
        "source_sha256",
        "prompt_version",
        "run_id",
    ),
    "asset_files": ("project_id", "asset_id", "asset_version", "role", "sha256"),
    "asset_usage": ("project_id", "asset_id", "asset_version", "run_id", "panel_index"),
}
_FOREIGN_KEYS = {
    "asset_versions": (("project_id", "asset_id"), "assets", ("project_id", "asset_id")),
    "asset_sources": (
        ("project_id", "asset_id", "asset_version"),
        "asset_versions",
        ("project_id", "asset_id", "asset_version"),
    ),
    "asset_files": (
        ("project_id", "asset_id", "asset_version"),
        "asset_versions",
        ("project_id", "asset_id", "asset_version"),
    ),
    "asset_usage": (
        ("project_id", "asset_id", "asset_version"),
        "asset_versions",
        ("project_id", "asset_id", "asset_version"),
    ),
}


def sqlite_wal_is_safe(version: tuple[int, ...]) -> bool:
    """Whether this SQLite release includes the WAL-reset race fix.

    SQLite backported the fix to 3.44.6 and 3.50.7. Other old branches are
    kept in rollback-journal mode; a distributor's unreported custom patch is
    deliberately not assumed.
    """
    if len(version) < 3:
        return False
    major, minor, patch = version[:3]
    return (
        (major, minor, patch) >= (3, 51, 3)
        or ((major, minor) == (3, 50) and patch >= 7)
        or ((major, minor) == (3, 44) and patch >= 6)
    )


def create_catalog_engine(path: Path) -> Engine:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
        finally:
            cursor.close()

    return engine


def _normalized_check(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def validate_baseline(connection: Connection) -> None:
    """Reject malformed or incompatible unversioned legacy catalogs."""
    inspector = inspect(connection)
    for table, expected in _COLUMNS.items():
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        if not set(expected).issubset(columns):
            raise RuntimeError(f"Incompatible legacy asset catalog: {table} has missing columns")
        for name, column in columns.items():
            if name not in expected:
                if not column.get("nullable", True) and column.get("default") is None:
                    raise RuntimeError(f"Incompatible required extra column: {table}.{name}")
                continue
            expected_type = "INTEGER" if name in {"archived", "panel_index"} else "TEXT"
            if str(column["type"]).upper() != expected_type or column.get("nullable", True):
                raise RuntimeError(f"Incompatible column type/nullability: {table}.{name}")
            default = column.get("default")
            if name in {"created_at", "updated_at"} and (
                default is None
                or "strftime" not in str(default).lower()
                or "'now'" not in str(default).lower()
            ):
                raise RuntimeError(f"Missing timestamp default: {table}.{name}")
            if table == "assets" and name == "archived" and str(default).strip(" ()") != "0":
                raise RuntimeError("Incompatible archived default")
        pk = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ())
        if pk != _PRIMARY_KEYS[table]:
            raise RuntimeError(f"Incompatible primary key: {table}")
        if table in _UNIQUE_KEYS:
            uniques = {
                tuple(item.get("column_names") or ())
                for item in inspector.get_unique_constraints(table)
            }
            if _UNIQUE_KEYS[table] not in uniques:
                raise RuntimeError(f"Missing unique constraint: {table}")
        if table in _FOREIGN_KEYS:
            expected_fk = _FOREIGN_KEYS[table]
            foreign_keys = inspector.get_foreign_keys(table)
            if not any(
                (
                    tuple(fk.get("constrained_columns") or ()),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or ()),
                )
                == expected_fk
                and all(
                    str(value).upper() == "NO ACTION"
                    for value in (fk.get("options") or {}).values()
                )
                for fk in foreign_keys
            ):
                raise RuntimeError(f"Missing or incompatible foreign key: {table}")
    checks = {_normalized_check(c["sqltext"]) for c in inspector.get_check_constraints("assets")}
    required = {
        _normalized_check("kind IN ('character', 'scene', 'prop')"),
        _normalized_check("archived IN (0, 1)"),
    }
    if not required.issubset(checks):
        raise RuntimeError("Missing or incompatible asset check constraints")
    for table in _FOREIGN_KEYS:
        if connection.exec_driver_sql(f"PRAGMA foreign_key_check('{table}')").first() is not None:
            raise RuntimeError(f"Legacy asset catalog has foreign-key violations: {table}")


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    config.attributes["connection"] = connection
    return config


def _catalog_state(connection: Connection) -> tuple[set[str], str | None, Config, str]:
    tables = set(inspect(connection).get_table_names())
    present = tables.intersection(_COLUMNS)
    if present and present != set(_COLUMNS):
        raise RuntimeError("Incompatible legacy asset catalog: some required tables are missing")
    if not present:
        existing = connection.exec_driver_sql("""SELECT type, name FROM sqlite_master
            WHERE type IN ('table', 'view', 'trigger', 'index')
              AND name NOT GLOB 'sqlite_*' AND name != 'alembic_version'
            LIMIT 1""").first()
        if existing:
            raise RuntimeError("Refusing to initialize an unrelated non-empty SQLite database")
    version = None
    if "alembic_version" in tables:
        versions = (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
        )
        if len(versions) > 1:
            raise RuntimeError("Asset catalog has multiple migration heads")
        version = versions[0] if versions else None
    config = _config(connection)
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError("Asset catalog migration scripts must have one head")
    if version:
        try:
            known = script.get_revision(version)
        except Exception as exc:
            raise RuntimeError(f"Unknown asset catalog migration revision: {version}") from exc
        if known is None:
            raise RuntimeError(f"Unknown asset catalog migration revision: {version}")
    if present:
        validate_baseline(connection)
        if version and any(
            revision.revision == FILE_SELECTION_REVISION
            for revision in script.iterate_revisions(version, BASELINE)
        ):
            inspector = inspect(connection)
            if "asset_file_selections" not in tables:
                raise RuntimeError("Versioned asset catalog is missing file selection history")
            columns = {col["name"]: col for col in inspector.get_columns("asset_file_selections")}
            required = {
                "id",
                "project_id",
                "asset_id",
                "asset_version",
                "role",
                "sha256",
                "created_at",
            }
            if not required.issubset(columns):
                raise RuntimeError("Incompatible asset file selection history schema")
            if tuple(
                inspector.get_pk_constraint("asset_file_selections").get("constrained_columns")
                or ()
            ) != ("id",):
                raise RuntimeError("Incompatible asset file selection history primary key")
            foreign_keys = inspector.get_foreign_keys("asset_file_selections")
            reference = ("project_id", "asset_id", "asset_version", "role", "sha256")
            if not any(
                tuple(fk.get("constrained_columns") or ()) == reference
                and fk.get("referred_table") == "asset_files"
                and tuple(fk.get("referred_columns") or ()) == reference
                for fk in foreign_keys
            ):
                raise RuntimeError("Incompatible asset file selection history foreign key")
            if (
                connection.exec_driver_sql(
                    "PRAGMA foreign_key_check('asset_file_selections')"
                ).first()
                is not None
            ):
                raise RuntimeError("Asset file selection history has foreign-key violations")
    elif version:
        raise RuntimeError("Versioned asset catalog is missing its tables")
    return present, version, config, heads[0]


def migrate_catalog(engine: Engine) -> str:
    """Upgrade or safely adopt an existing catalog in a single write lock."""
    mode = "WAL" if sqlite_wal_is_safe(sqlite3.sqlite_version_info) else "DELETE"
    with engine.connect() as connection:
        # Do not change journal mode for an incompatible database. Recheck
        # after acquiring the write lock to close the preflight race window.
        connection.exec_driver_sql("BEGIN")
        _catalog_state(connection)
        connection.rollback()
        # journal_mode is persistent and cannot be changed inside a transaction.
        actual = connection.exec_driver_sql(f"PRAGMA journal_mode = {mode}").scalar_one()
        connection.commit()
        if str(actual).upper() != mode:
            raise RuntimeError(f"Could not select safe SQLite journal mode {mode}: {actual}")
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            present, version, config, head = _catalog_state(connection)
            if present and not version:
                command.stamp(config, BASELINE)
                version = BASELINE
            if version != head:
                command.upgrade(config, "head")
                validate_baseline(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return head
