"""Tests for the novel-storyboard H3 package adapter."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from comfyui_client.__main__ import _storyboard, build_parser  # noqa: E402
from comfyui_client.bindings import build_graph  # noqa: E402
from comfyui_client.storyboard import load_storyboard_bundle  # noqa: E402


class StoryboardBundleTests(unittest.TestCase):
    def make_bundle(self, directory: Path, *, prefixed: bool = True) -> Path:
        package = directory / "H3投产包"
        segment = package / "E01-01"
        segment.mkdir(parents=True)
        (segment / "prompt.md").write_text(
            "# E01-01 · H3 提示词\n\n首帧清单\n\n---\n\n"
            "Align Picture 1 then Picture 2.\n"
            "integrated_multimodal_description:\n"
            "[Shot 1] <Picture 1> start.\n[Shot 2] <Picture 2> cut.\n",
            encoding="utf-8",
        )
        (segment / "f1.png").write_bytes(_fake_png(1672, 941))
        (segment / "f2.png").write_bytes(_fake_png(1672, 941))
        prefix = "H3投产包/" if prefixed else ""
        manifest = [
            {
                "segment": "E01-01",
                "seconds": 9,
                "cuts": 2,
                "cutStarts": [0, 4.9],
                "prompt": f"{prefix}E01-01/prompt.md",
                "pictures": [f"{prefix}E01-01/f1.png", f"{prefix}E01-01/f2.png"],
                "missing": [],
            }
        ]
        path = package / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_loads_exported_manifest_paths_and_extracts_only_h3_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            segments = load_storyboard_bundle(manifest)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].seconds, 9)
        self.assertEqual(segments[0].aspect_ratio, "16:9")
        self.assertEqual([path.name for path in segments[0].pictures], ["f1.png", "f2.png"])
        self.assertTrue(segments[0].prompt.startswith("Align Picture 1"))
        self.assertNotIn("首帧清单", segments[0].prompt)

    def test_supports_export_to_current_directory_without_path_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory), prefixed=False)
            segments = load_storyboard_bundle(manifest)
        self.assertEqual(len(segments[0].pictures), 2)

    def test_rejects_picture_prompt_mismatch_before_workflow_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            prompt = manifest.parent / "E01-01" / "prompt.md"
            prompt.write_text("Title\n---\nOnly <Picture 1>\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "do not match"):
                load_storyboard_bundle(manifest)

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data[0]["prompt"] = "../outside.md"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stay inside"):
                load_storyboard_bundle(manifest)

    def test_rejects_reference_frames_with_different_aspect_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            (manifest.parent / "E01-01" / "f2.png").write_bytes(_fake_png(720, 1280))
            with self.assertRaisesRegex(ValueError, "aspect ratio does not match"):
                load_storyboard_bundle(manifest)

    def test_rejects_more_pictures_than_the_workflow_supports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            segment = manifest.parent / "E01-01"
            pictures = []
            for index in range(1, 11):
                name = f"f{index}.png"
                (segment / name).write_bytes(_fake_png(1672, 941))
                pictures.append(f"E01-01/{name}")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data[0]["pictures"] = pictures
            (segment / "prompt.md").write_text(
                "Title\n---\n" + " ".join(f"<Picture {i}>" for i in range(1, 11)),
                encoding="utf-8",
            )
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference image slots"):
                load_storyboard_bundle(manifest)

    def test_submit_uploads_in_picture_order_and_records_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_bundle(root)
            output = root / "output"
            args = build_parser().parse_args(
                [
                    "storyboard",
                    "--manifest",
                    str(manifest),
                    "--submit-only",
                    "--output-dir",
                    str(output),
                ]
            )
            client = MagicMock()
            client.upload_file.side_effect = ["batch/f1.png", "batch/f2.png"]
            client.submit.return_value = "prompt-123"
            self.assertEqual(_storyboard(args, client), 0)
            self.assertEqual(
                [call.args[0].name for call in client.upload_file.call_args_list],
                ["f1.png", "f2.png"],
            )
            record = json.loads((output / "comfy-prompt-123.json").read_text(encoding="utf-8"))
            self.assertEqual(record["segment"], "E01-01")
            self.assertEqual(record["reference_images"], ["batch/f1.png", "batch/f2.png"])
            self.assertEqual(record["seconds"], 9)
            self.assertEqual(record["aspect_ratio"], "16:9")
            submitted_graph = client.submit.call_args.args[0]
            original_graph = build_graph(
                {"prompt": "x", "length": 9, "reference_images": ["a.png"]}
            )
            self.assertEqual(submitted_graph["13"]["inputs"], original_graph["13"]["inputs"])
            self.assertEqual(submitted_graph["13"]["inputs"]["size_mode"], "scale_by")
            self.assertIn("16:9", submitted_graph["29"]["inputs"]["aspect_ratio"])
            self.assertNotIn("107", submitted_graph)
            client.wait.assert_not_called()

    def test_dry_run_reports_segments_without_a_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_bundle(Path(directory))
            args = build_parser().parse_args(
                ["storyboard", "--manifest", str(manifest), "--dry-run"]
            )
            self.assertEqual(_storyboard(args), 0)


def _fake_png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)


if __name__ == "__main__":
    unittest.main()
