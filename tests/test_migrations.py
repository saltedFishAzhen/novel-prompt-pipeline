from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from pipeline.database.catalog import (
    BASELINE,
    FILE_SELECTION_REVISION,
    MIGRATIONS,
    sqlite_wal_is_safe,
)
from pipeline.library import AssetLibrary


class CatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.database = Path(self.temp.name) / "assets.db"

    def tearDown(self):
        self.temp.cleanup()

    def _populated_legacy_catalog(self):
        library = AssetLibrary(self.database, "story")
        item = {
            "asset_id": "a",
            "kind": "character",
            "name": "许青",
            "aliases": "许青",
            "description": "green jacket",
        }
        original = library.register([item], "source", "prompt", "run")[0]
        library.update("a", description="curated jacket")
        library.record_usage("a", "run", 1, original["asset_version"])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE asset_file_selections")
            connection.execute("DROP TABLE alembic_version")
            connection.commit()
        return original

    def test_new_catalog_is_versioned_and_migration_is_idempotent(self):
        library = AssetLibrary(self.database)
        self.assertEqual(library.schema_version(), FILE_SELECTION_REVISION)
        self.assertEqual(library.migrate(), FILE_SELECTION_REVISION)
        self.assertEqual(library.schema_version(), FILE_SELECTION_REVISION)

    def test_adopt_unversioned_catalog_preserves_assets_and_history(self):
        original = self._populated_legacy_catalog()
        library = AssetLibrary(self.database, "story")
        self.assertEqual(library.schema_version(), FILE_SELECTION_REVISION)
        self.assertEqual(library.get("a")["description"], "curated jacket")
        self.assertEqual(library.get("a", original["asset_version"])["description"], "green jacket")
        self.assertEqual(len(library.versions("a")), 2)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_usage").fetchone()[0], 1
            )

    def test_incompatible_unversioned_catalog_is_not_stamped(self):
        self._populated_legacy_catalog()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("ALTER TABLE asset_usage RENAME TO previous_usage")
            connection.execute("CREATE TABLE asset_usage AS SELECT * FROM previous_usage")
            connection.execute("DROP TABLE previous_usage")
            connection.commit()
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal"
            )
        with self.assertRaisesRegex(RuntimeError, "Incompatible|Missing"):
            AssetLibrary(self.database, "story")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM assets").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_usage").fetchone()[0], 1
            )
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
                ).fetchone()
            )

    def test_unknown_migration_revision_fails_without_overwriting_it(self):
        library = AssetLibrary(self.database, "story")
        library.register(
            [
                {
                    "asset_id": "a",
                    "kind": "character",
                    "name": "许青",
                    "aliases": "许青",
                    "description": "coat",
                }
            ]
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE alembic_version SET version_num = 'future_revision'")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "Unknown asset catalog migration revision"):
            AssetLibrary(self.database, "story")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
                "future_revision",
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM assets").fetchone()[0], 1)

    def test_concurrent_initialization_adopts_once(self):
        self._populated_legacy_catalog()
        gate = Barrier(4)

        def adopt():
            gate.wait(timeout=10)
            library = AssetLibrary(self.database, "story")
            return library.schema_version(), library.get("a")["description"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(adopt) for _ in range(4)]
            self.assertEqual(
                [future.result(timeout=40) for future in futures],
                [(FILE_SELECTION_REVISION, "curated jacket")] * 4,
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM alembic_version").fetchone()[0], 1
            )

    def test_later_revision_upgrades_existing_catalog_without_losing_data(self):
        library = AssetLibrary(self.database, "story")
        library.register(
            [
                {
                    "asset_id": "a",
                    "kind": "character",
                    "name": "许青",
                    "aliases": "许青",
                    "description": "coat",
                }
            ]
        )
        migrations = Path(self.temp.name) / "migrations"
        shutil.copytree(MIGRATIONS, migrations)
        (migrations / "versions" / "0003_note.py").write_text(
            "from alembic import op\nimport sqlalchemy as sa\n"
            "revision = '0003_note'\ndown_revision = '0002_file_selections'\n"
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    op.add_column('assets', sa.Column('note', sa.Text, nullable=True))\n",
            encoding="utf-8",
        )
        with patch("pipeline.database.catalog.MIGRATIONS", migrations):
            upgraded = AssetLibrary(self.database, "story")
            self.assertEqual(upgraded.schema_version(), "0003_note")
            self.assertEqual(upgraded.get("a")["description"], "coat")
            self.assertEqual(upgraded.migrate(), "0003_note")
        with closing(sqlite3.connect(self.database)) as connection:
            names = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            self.assertIn("note", names)

    def test_failed_revision_rolls_back_schema_and_revision(self):
        library = AssetLibrary(self.database, "story")
        library.register(
            [
                {
                    "asset_id": "a",
                    "kind": "character",
                    "name": "许青",
                    "aliases": "许青",
                    "description": "coat",
                }
            ]
        )
        migrations = Path(self.temp.name) / "failed-migrations"
        shutil.copytree(MIGRATIONS, migrations)
        (migrations / "versions" / "0003_failure.py").write_text(
            "from alembic import op\nimport sqlalchemy as sa\n"
            "revision = '0003_failure'\ndown_revision = '0002_file_selections'\n"
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n"
            "    op.add_column('assets', sa.Column('partial', sa.Text, nullable=True))\n"
            "    raise RuntimeError('simulated migration error')\n",
            encoding="utf-8",
        )
        with patch("pipeline.database.catalog.MIGRATIONS", migrations):
            with self.assertRaisesRegex(RuntimeError, "simulated migration error"):
                AssetLibrary(self.database, "story")
        self.assertEqual(library.schema_version(), FILE_SELECTION_REVISION)
        self.assertEqual(library.get("a")["description"], "coat")
        with closing(sqlite3.connect(self.database)) as connection:
            names = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            self.assertNotIn("partial", names)

    def test_wal_fix_versions_and_runtime_mode(self):
        for version in ((3, 44, 5), (3, 45, 0), (3, 50, 4), (3, 51, 2)):
            self.assertFalse(sqlite_wal_is_safe(version), version)
        for version in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 52, 0), (4, 0, 0)):
            self.assertTrue(sqlite_wal_is_safe(version), version)
        AssetLibrary(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0].upper()
        self.assertEqual(
            mode, "WAL" if sqlite_wal_is_safe(sqlite3.sqlite_version_info) else "DELETE"
        )

    def test_unrelated_nonempty_database_is_never_adopted(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE user_data (value TEXT)")
            connection.execute("INSERT INTO user_data VALUES ('untouched')")
            connection.commit()
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal"
            )
        with self.assertRaisesRegex(RuntimeError, "unrelated non-empty"):
            AssetLibrary(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(
                connection.execute("SELECT value FROM user_data").fetchone()[0], "untouched"
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
                ).fetchone()
            )

    def test_v1_catalog_upgrades_without_losing_asset_files(self):
        library = AssetLibrary(self.database, "story")
        item = {
            "asset_id": "a",
            "kind": "character",
            "name": "许青",
            "aliases": "许青",
            "description": "coat",
        }
        library.register([item])
        image = Path(self.temp.name) / "portrait.png"
        image.write_bytes(b"image-a")
        first = library.link_file("a", image)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE asset_file_selections")
            connection.execute("UPDATE alembic_version SET version_num = ?", (BASELINE,))
            connection.commit()
        upgraded = AssetLibrary(self.database, "story")
        self.assertEqual(upgraded.schema_version(), FILE_SELECTION_REVISION)
        self.assertEqual(upgraded.files("a")[0]["sha256"], first["sha256"])
        self.assertEqual(Path(upgraded.files("a")[0]["local_path"]).read_bytes(), b"image-a")


if __name__ == "__main__":
    unittest.main()
