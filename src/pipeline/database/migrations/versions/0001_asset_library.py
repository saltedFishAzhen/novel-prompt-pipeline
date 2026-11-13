"""Create the original versioned asset catalog.

Revision ID: 0001_asset_library
Revises: None
"""

from alembic import op

revision = "0001_asset_library"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This baseline is intentionally frozen. Add later changes as new revisions.
    for statement in (
        """CREATE TABLE assets (
            project_id TEXT NOT NULL, asset_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('character', 'scene', 'prop')),
            name TEXT NOT NULL, current_version TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (project_id, asset_id), UNIQUE (project_id, kind, name)
        )""",
        """CREATE TABLE asset_versions (
            project_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_version TEXT NOT NULL,
            aliases TEXT NOT NULL, description TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (project_id, asset_id, asset_version),
            FOREIGN KEY (project_id, asset_id) REFERENCES assets (project_id, asset_id)
        )""",
        """CREATE TABLE asset_sources (
            project_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_version TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, prompt_version TEXT NOT NULL, run_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (project_id, asset_id, asset_version, source_sha256, prompt_version, run_id),
            FOREIGN KEY (project_id, asset_id, asset_version)
                REFERENCES asset_versions (project_id, asset_id, asset_version)
        )""",
        """CREATE TABLE asset_files (
            project_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_version TEXT NOT NULL,
            role TEXT NOT NULL, sha256 TEXT NOT NULL, original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (project_id, asset_id, asset_version, role, sha256),
            FOREIGN KEY (project_id, asset_id, asset_version)
                REFERENCES asset_versions (project_id, asset_id, asset_version)
        )""",
        """CREATE TABLE asset_usage (
            project_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_version TEXT NOT NULL,
            run_id TEXT NOT NULL, panel_index INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (project_id, asset_id, asset_version, run_id, panel_index),
            FOREIGN KEY (project_id, asset_id, asset_version)
                REFERENCES asset_versions (project_id, asset_id, asset_version)
        )""",
    ):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("The asset catalog baseline cannot be downgraded destructively")
