"""Command-line behaviour of shot-plan: outputs, exit codes and input safety."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
# Reuse the table fixture next to this file, whether run by discovery or by name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_shot_client import PNG, SOURCE, TABLE  # noqa: E402

from shot_client.__main__ import main as shot_main  # noqa: E402

OUTPUTS = ("shot.table.json", "shot.report.json", "storyboard.txt", "h3.request.json")


class ShotCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        images = []
        for index, name in enumerate(("girl", "door")):
            path = self.root / f"{name}.png"
            path.write_bytes(PNG + bytes([index]))
            images.append({"path": f"{name}.png", "name": name})
        self.request = {
            "type": "ref2va",
            "source_text": SOURCE,
            "duration_seconds": 6,
            "reference_images": images,
            "storyboard_table": TABLE,
        }

    def write_request(self, request: dict, name: str = "shot.request.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, request: Path, out: Path | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = shot_main(["--input", str(request), "--output-dir", str(out or self.out)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_valid_request_writes_four_outputs(self) -> None:
        code, stdout, stderr = self.run_cli(self.write_request(self.request))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), sorted(OUTPUTS))
        payload = json.loads((self.out / "h3.request.json").read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"type", "storyboard", "reference_images", "values"})
        self.assertEqual(payload["values"], {"length": 6})
        self.assertEqual(
            json.loads((self.out / "shot.report.json").read_text(encoding="utf-8"))["status"],
            "ready",
        )
        self.assertIn("今天不打开。", (self.out / "storyboard.txt").read_text(encoding="utf-8"))
        self.assertIn("h3.request.json", stdout)

    def test_invalid_table_exits_two_and_leaves_only_the_report(self) -> None:
        self.out.mkdir()
        stale = self.out / "h3.request.json"
        stale.write_text("stale", encoding="utf-8")
        self.request["storyboard_table"] = TABLE.replace("今天不打开。", "今天要打开。")
        code, _, stderr = self.run_cli(self.write_request(self.request))
        self.assertEqual(code, 2, stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ["shot.report.json"])
        report = json.loads((self.out / "shot.report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["issues"][0]["path"], "table")

    def test_missing_request_exits_one_and_reports_the_input(self) -> None:
        code, _, _ = self.run_cli(self.root / "absent.json")
        self.assertEqual(code, 1)
        report = json.loads((self.out / "shot.report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["issues"][0]["path"], "input")

    def test_bad_reference_image_exits_one_and_names_the_field(self) -> None:
        self.request["reference_images"] = [{"path": "missing.png"}]
        code, _, _ = self.run_cli(self.write_request(self.request))
        self.assertEqual(code, 1)
        report = json.loads((self.out / "shot.report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["issues"][0]["path"], "reference_images")

    def test_unknown_field_is_rejected_without_touching_the_request(self) -> None:
        self.request["unknown_field"] = True
        source = self.write_request(self.request)
        raw = source.read_text(encoding="utf-8")
        code, _, stderr = self.run_cli(source)
        self.assertEqual(code, 1)
        self.assertIn("unknown request fields", stderr)
        self.assertEqual(source.read_text(encoding="utf-8"), raw)

    def test_request_at_an_output_path_is_never_removed(self) -> None:
        self.out.mkdir()
        source = self.out / "shot.report.json"
        source.write_text(json.dumps(self.request, ensure_ascii=False), encoding="utf-8")
        raw = source.read_text(encoding="utf-8")
        code, _, stderr = self.run_cli(source)
        self.assertEqual(code, 1)
        self.assertIn("collides with an input", stderr)
        self.assertEqual(source.read_text(encoding="utf-8"), raw)

    def test_table_file_and_image_paths_are_relative_to_the_request(self) -> None:
        table = self.root / "table.md"
        table.write_text(TABLE, encoding="utf-8")
        request = dict(self.request)
        del request["storyboard_table"]
        request["table_file"] = "table.md"
        code, _, stderr = self.run_cli(self.write_request(request))
        self.assertEqual(code, 0, stderr)
        payload = json.loads((self.out / "h3.request.json").read_text(encoding="utf-8"))
        for entry in payload["reference_images"]:
            self.assertTrue(Path(entry["path"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
