import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from pipeline.library import AssetLibrary

ROOT = Path(__file__).resolve().parents[1]


def asset(asset_id="c1", name="默认的许青", description="green jacket"):
    return {
        "asset_id": asset_id,
        "kind": "character",
        "name": name,
        "aliases": "许青",
        "description": description,
    }


class AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.db = self.root / "library.sqlite3"
        self.library = AssetLibrary(self.db, "story-a")

    def tearDown(self):
        self.temp.cleanup()

    def test_project_isolation_and_shared_alias_does_not_merge_looks(self):
        first = asset()
        second = asset("c2", "白衬衫许青", "white shirt")
        self.library.register(
            [first, second], source_sha256="source", prompt_version="v1", run_id="r1"
        )
        self.assertEqual({x["asset_id"] for x in self.library.list(query="许青")}, {"c1", "c2"})
        other = AssetLibrary(self.db, "story-b")
        self.assertEqual(other.list(), [])
        other.register([asset(description="different story")])
        self.assertEqual(self.library.get("c1")["description"], "green jacket")
        self.assertEqual(other.get("c1")["description"], "different story")
        self.assertEqual(self.library.list(query="' OR 1=1 --"), [])

    def test_versions_reuse_manual_updates_and_preserve_provenance(self):
        initial = self.library.register([asset()], "source1", "prompt1", "run1")[0]
        edited = self.library.update("c1", description="curated green jacket")
        self.assertNotEqual(initial["asset_version"], edited["asset_version"])
        reused = self.library.register(
            [asset(description="new model output")], "source2", "prompt2", "run2"
        )[0]
        self.assertEqual(reused["asset_version"], edited["asset_version"])
        self.assertEqual(reused["description"], "curated green jacket")
        replacement = self.library.register(
            [asset(description="new model output")],
            "source3",
            "prompt3",
            "run3",
            reuse_existing=False,
        )[0]
        self.assertNotEqual(replacement["asset_version"], edited["asset_version"])
        self.assertEqual(
            self.library.get("c1", initial["asset_version"])["description"], "green jacket"
        )
        versions = self.library.versions("c1")
        self.assertEqual(len(versions), 3)
        curated = next(x for x in versions if x["asset_version"] == edited["asset_version"])
        self.assertEqual(curated["sources"][0]["run_id"], "run2")

    def test_concurrent_edits_merge_and_keep_history(self):
        self.library.register([asset()])
        gate = threading.Barrier(2)

        def update(**kwargs):
            gate.wait(timeout=5)
            return self.library.update("c1", **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(update, aliases="许青,阿青")
            b = pool.submit(update, description="blue coat")
            a.result(timeout=10)
            b.result(timeout=10)
        final = self.library.get("c1")
        self.assertEqual(final["aliases"], "许青,阿青")
        self.assertEqual(final["description"], "blue coat")
        self.assertEqual(len(self.library.versions("c1")), 3)

    def test_registration_is_atomic_and_archive_is_not_resurrected(self):
        with self.assertRaises(ValueError):
            self.library.register([asset(), asset("c1", "另一个角色")])
        self.assertEqual(self.library.list(), [])
        self.library.register([asset()])
        self.library.archive("c1")
        self.assertEqual(self.library.register([asset()]), [])
        self.assertEqual(self.library.list(), [])
        self.assertTrue(self.library.get("c1")["archived"])
        self.library.restore("c1")
        self.assertEqual(len(self.library.list()), 1)

    def test_managed_file_survives_original_and_export_import_keeps_versions(self):
        initial = self.library.register([asset()], "s", "p", "r")[0]
        original = self.root / "portrait.png"
        original.write_bytes(b"test image bytes")
        linked = self.library.link_file("c1", original, asset_version=initial["asset_version"])
        original.unlink()
        self.assertEqual(Path(linked["local_path"]).read_bytes(), b"test image bytes")
        copy_with_other_extension = self.root / "same.jpg"
        copy_with_other_extension.write_bytes(b"test image bytes")
        duplicate = self.library.link_file(
            "c1", copy_with_other_extension, asset_version=initial["asset_version"]
        )
        self.assertEqual(duplicate["local_path"], linked["local_path"])
        edited = self.library.update("c1", description="white coat")
        self.library.record_usage("c1", "run1", 0, asset_version=initial["asset_version"])
        self.library.record_usage("c1", "run1", 0, asset_version=initial["asset_version"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("select count(*) from asset_usage").fetchone()[0], 1)
        exported = json.loads(json.dumps(self.library.export(), ensure_ascii=False))
        other = AssetLibrary(self.root / "other" / "library.db", "other-project")
        other.import_data(exported)
        self.assertEqual(other.get("c1")["asset_version"], edited["asset_version"])
        self.assertEqual(len(other.versions("c1")), 2)
        copied = other.files("c1", initial["asset_version"], role="reference")
        self.assertEqual(len(copied), 1)
        self.assertEqual(Path(copied[0]["local_path"]).read_bytes(), b"test image bytes")
        self.assertNotEqual(copied[0]["local_path"], linked["local_path"])

    def test_import_rejects_tampered_media_before_writing(self):
        self.library.register([asset()])
        source = self.root / "source.png"
        source.write_bytes(b"original image")
        self.library.link_file("c1", source)
        exported = self.library.export()
        Path(exported["assets"][0]["files"][0]["local_path"]).write_bytes(b"changed bytes")
        other = AssetLibrary(self.root / "other" / "library.db", "other")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            other.import_data(exported)
        self.assertEqual(other.list(), [])

    def test_relink_existing_file_becomes_latest_and_preserves_history(self):
        self.library.register([asset()])
        image_a, image_b = self.root / "a.png", self.root / "b.png"
        image_a.write_bytes(b"image A")
        image_b.write_bytes(b"image B")
        first = self.library.link_file("c1", image_a)
        second = self.library.link_file("c1", image_b)
        third = self.library.link_file("c1", image_a)
        self.assertEqual(first["sha256"], third["sha256"])
        self.assertEqual(
            [x["sha256"] for x in self.library.files("c1")], [second["sha256"], first["sha256"]]
        )
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_files").fetchone()[0], 2
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_file_selections").fetchone()[0], 3
            )
        exported = self.library.export()
        other = AssetLibrary(self.root / "other" / "catalog.db", "story-b")
        other.import_data(exported)
        self.assertEqual(
            [x["sha256"] for x in other.files("c1")], [second["sha256"], first["sha256"]]
        )

    def test_concurrent_file_selections_keep_every_event(self):
        self.library.register([asset()])
        image_a, image_b = self.root / "a.png", self.root / "b.png"
        image_a.write_bytes(b"image A")
        image_b.write_bytes(b"image B")
        with ThreadPoolExecutor(max_workers=4) as pool:
            selections = list(
                pool.map(
                    lambda index: self.library.link_file("c1", image_a if index % 2 else image_b),
                    range(12),
                )
            )
        final = self.library.link_file("c1", image_a)
        self.assertEqual(self.library.files("c1")[-1]["sha256"], final["sha256"])
        self.assertEqual(len({item["sha256"] for item in selections}), 2)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_files").fetchone()[0], 2
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM asset_file_selections").fetchone()[0], 13
            )

    def test_external_managed_directory_is_rejected_for_link_and_import(self):
        self.library.register([asset()])
        image = self.root / "source.png"
        image.write_bytes(b"image")
        external = self.root / "outside"
        external.mkdir()
        other = AssetLibrary(self.root / "catalog" / "assets.db", "story-b")
        other.register([asset()])
        other.file_root = external
        with self.assertRaisesRegex(ValueError, "inside the database directory"):
            other.link_file("c1", image)
        imported = asset("c2", "白衬衫许青")
        imported["files"] = [{"local_path": str(image), "role": "reference"}]
        with self.assertRaisesRegex(ValueError, "inside the database directory"):
            other.import_data([imported])
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual([x["asset_id"] for x in other.list()], ["c1"])

    def test_corrupt_database_filename_cannot_escape_managed_directory(self):
        self.library.register([asset()])
        image = self.root / "source.png"
        image.write_bytes(b"image")
        self.library.link_file("c1", image)
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        try:
            with closing(sqlite3.connect(self.db)) as connection:
                connection.execute(
                    "UPDATE asset_files SET stored_name=? WHERE asset_id=?",
                    (f"../../{outside.name}/escaped.png", "c1"),
                )
                connection.commit()
            for operation in (
                lambda: self.library.link_file("c1", image),
                lambda: self.library.files("c1"),
                self.library.export,
            ):
                with self.assertRaisesRegex(ValueError, "Invalid managed asset filename"):
                    operation()
            self.assertEqual(list(outside.iterdir()), [])
            with closing(sqlite3.connect(self.db)) as connection:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM asset_file_selections").fetchone()[0],
                    1,
                )
        finally:
            outside.rmdir()

    def test_managed_directory_symlink_to_external_is_rejected(self):
        other = AssetLibrary(self.root / "catalog" / "assets.db", "story-b")
        other.register([asset()])
        external = self.root / "outside"
        external.mkdir()
        image = self.root / "source.png"
        image.write_bytes(b"image")
        try:
            os.symlink(external, other.file_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            other.link_file("c1", image)
        imported = asset("c2", "白衬衫许青")
        imported["files"] = [{"local_path": str(image), "role": "reference"}]
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            other.import_data([imported])
        self.assertEqual(list(external.iterdir()), [])

    def test_import_identity_conflict_rolls_back_all_versions_and_assets(self):
        before = self.library.register([asset()])[0]
        first = asset(description="would replace current")
        imported_file = self.root / "import.png"
        imported_file.write_bytes(b"candidate media")
        first["files"] = [{"local_path": str(imported_file), "role": "reference"}]
        second = asset("different-id", description="conflicts by name")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.library.import_data([first, second])
        after = self.library.get("c1")
        self.assertEqual(after["asset_version"], before["asset_version"])
        self.assertEqual(after["description"], "green jacket")
        self.assertEqual(len(self.library.versions("c1")), 1)
        self.assertEqual(self.library.files("c1"), [])
        self.assertEqual([x["asset_id"] for x in self.library.list()], ["c1"])

    def test_import_conflict_rolls_back_earlier_new_asset_and_restore(self):
        self.library.register([asset()])
        self.library.archive("c1")
        new = asset("new-id", "其他角色")
        restored = asset(description="would restore this asset")
        conflict = asset("new-id", "different name")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.library.import_data([new, restored, conflict])
        self.assertTrue(self.library.get("c1")["archived"])
        self.assertEqual(self.library.get("c1")["description"], "green jacket")
        self.assertEqual(len(self.library.versions("c1")), 1)
        self.assertEqual(self.library.list(), [])
        with self.assertRaises(KeyError):
            self.library.get("new-id")


if __name__ == "__main__":
    unittest.main()
