"""OpenAI SDK contract tests for the Markdown relationship director."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from test_shot_client import PNG, SOURCE, TABLE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from director_client import DirectorClient, DirectorError  # noqa: E402
from director_client.__main__ import main as director_main  # noqa: E402
from h3_client import ReferenceImage  # noqa: E402


class DirectorClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = [Path(self.temp.name) / "girl.png", Path(self.temp.name) / "door.png"]
        for index, path in enumerate(self.paths):
            path.write_bytes(PNG + bytes([index]))
        self.images = [
            ReferenceImage(self.paths[0], name="女孩"),
            ReferenceImage(self.paths[1], name="门"),
        ]
        self.requests: list[dict] = []
        self.content: str | None = TABLE
        self.finish_reason = "stop"
        self.refusal: str | None = None
        self.status_code = 200

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            if self.status_code != 200:
                return httpx.Response(
                    self.status_code, json={"error": {"message": "echo test-key"}}
                )
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "director",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": self.finish_reason,
                            "message": {
                                "role": "assistant",
                                "content": self.content,
                                "refusal": self.refusal,
                            },
                        }
                    ],
                },
            )

        self.sdk = OpenAI(
            api_key="test-key",
            base_url="https://example.test/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(self.sdk.close)

    def direct(self):
        return DirectorClient(model="director", sdk_client=self.sdk).direct(
            "ref2va", SOURCE, self.images, 6
        )

    def test_director_receives_ordered_images_and_original_method(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = self.direct()
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.table_text, TABLE)
        request = self.requests[0]
        self.assertEqual(request["reasoning_effort"], "low")
        system = request["messages"][0]["content"]
        for required in ("关系导演", "潜台词", "目标—障碍—行动", "## 七", "## 八"):
            self.assertIn(required, system)
        checklist = system.split("## 七", 1)[1].split("## 八", 1)[0]
        self.assertEqual(re.findall(r"(?m)^(\d+)\. ", checklist), [str(i) for i in range(1, 11)])
        for full in (
            "特写是否给出了太多答案，而不是留下悬念？",
            "是否尊重了人物“掩盖”情绪的那层壳，只在裂缝处介入？",
            "人物关系未变化时，是否随意切换了主观视角与主观视点？",
            "道具和场景的前景/中景/远景层次是否被机位调动起来，还是只是背景？",
        ):
            self.assertIn(full, checklist)
        for forbidden in (
            "以下为完整版",
            "可直接粘贴",
            "以上为完整提示词",
            "核心一句话",
            "## 九",
            "ShotPlan JSON schema",
        ):
            self.assertNotIn(forbidden, system)
        content = request["messages"][1]["content"]
        manifest = json.loads(content[0]["text"])
        self.assertEqual(manifest["output_language"], "English")
        self.assertEqual(
            manifest["verbatim_source_language"], ["dialogue", "lyrics", "visible_text"]
        )
        urls = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
        self.assertEqual(
            urls,
            [
                "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
                for p in self.paths
            ],
        )
        shot_request = result.to_shot_request(values={"seed": 42})
        self.assertEqual(shot_request["storyboard_table"], TABLE)
        self.assertEqual(shot_request["type"], "ref2va")
        self.assertEqual(shot_request["duration_seconds"], 6)
        self.assertEqual(shot_request["values"], {"seed": 42})
        for index, image in enumerate(shot_request["reference_images"]):
            self.assertEqual(Path(image["path"]), self.paths[index].resolve())

    def test_provider_errors_refusals_and_truncation_fail_safely(self) -> None:
        self.status_code = 401
        with self.assertRaises(DirectorError) as caught:
            self.direct()
        self.assertNotIn("test-key", "".join(traceback.format_exception(caught.exception)))
        self.status_code = 200
        self.finish_reason = "length"
        with self.assertRaises(DirectorError):
            self.direct()
        self.finish_reason = "stop"
        self.refusal = "cannot comply"
        self.content = None
        with self.assertRaises(DirectorError):
            self.direct()

    def test_director_can_request_missing_information(self) -> None:
        self.content = "需要补充信息：请说明两个角色与参考图的对应关系。"
        result = self.direct()
        self.assertEqual(result.status, "needs_input")
        with self.assertRaises((DirectorError, ValueError)):
            result.to_shot_request()

    def test_cli_rejects_unknown_fields_without_removing_referenced_image(self) -> None:
        root = Path(self.temp.name)
        target = root / "director.md"
        target.write_bytes(PNG)
        stale = root / "shot.request.json"
        stale.write_text("old request", encoding="utf-8")
        request = root / "input.json"
        request.write_text(
            json.dumps({"reference_images": [{"path": "director.md"}], "unknown": True}),
            encoding="utf-8",
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            patch("director_client.__main__.DirectorClient", side_effect=AssertionError),
        ):
            code = director_main(["--input", str(request), "--output-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), PNG)
        self.assertFalse(stale.exists())

    def test_cli_directory_target_does_not_block_other_stale_output_cleanup(self) -> None:
        root = Path(self.temp.name)
        (root / "director.md").mkdir()
        stale = root / "shot.request.json"
        stale.write_text("old request", encoding="utf-8")
        request = root / "input.json"
        request.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "source_text": SOURCE,
                    "reference_images": [str(self.paths[0])],
                    "duration_seconds": 6,
                }
            ),
            encoding="utf-8",
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            patch("director_client.__main__.DirectorClient", side_effect=AssertionError),
        ):
            code = director_main(["--input", str(request), "--output-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertTrue((root / "director.md").is_dir())
        self.assertFalse(stale.exists())

    def test_cli_never_removes_request_when_it_is_an_output_path(self) -> None:
        root = Path(self.temp.name)
        source = root / "shot.request.json"
        raw = json.dumps({"reference_images": [], "unknown": True})
        source.write_text(raw, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = director_main(["--input", str(source), "--output-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertEqual(source.read_text(encoding="utf-8"), raw)

    def test_cli_does_not_follow_symbolic_link_output(self) -> None:
        root = Path(self.temp.name)
        target = root / "preserve.txt"
        target.write_text("keep", encoding="utf-8")
        link = root / "director.md"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        source = root / "input.json"
        source.write_text(json.dumps({"reference_images": [], "unknown": True}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = director_main(["--input", str(source), "--output-dir", str(root)])
        self.assertEqual(code, 1)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
