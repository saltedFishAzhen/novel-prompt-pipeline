"""Track repeated selections of an asset file without discarding history.

Revision ID: 0002_file_selections
Revises: 0001_asset_library
"""

from alembic import op

revision = "0002_file_selections"
down_revision = "0001_asset_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE asset_file_selections (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_version TEXT NOT NULL,
        role TEXT NOT NULL, sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        FOREIGN KEY (project_id, asset_id, asset_version, role, sha256)
            REFERENCES asset_files (project_id, asset_id, asset_version, role, sha256)
    )""")
    op.execute("""CREATE INDEX ix_asset_file_selections_file
        ON asset_file_selections (project_id, asset_id, asset_version, role, sha256, id)
    """)


def downgrade() -> None:
    raise RuntimeError("Asset file selection history cannot be downgraded destructively")
