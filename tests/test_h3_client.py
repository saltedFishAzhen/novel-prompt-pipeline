"""Contract tests for H3 compilation and ComfyUI profile integration."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from comfyui_client import build_graph  # noqa: E402
from h3_client import H3Client, H3Compilation, H3Error, ReferenceImage  # noqa: E402
from h3_client.__main__ import main as h3_main  # noqa: E402
from h3_client.prompts import (  # noqa: E402
    I2VA_FIELDS,
    I2VA_HEADER,
    REF2VA_FIELDS,
    SYSTEM_PROMPT,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)

I2VA = """For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] The woman opens the door with her left hand.
overall_soundscape: N/A
non_diegetic_music: N/A"""

REF2VA = """subject_definitions:
<Subject 1> is the woman in <Picture 1>.
<Subject 2> is the door in <Picture 2>.
summary:
[reference generation] <Subject 1> opens <Subject 2>.
retention_analysis:
<Subject 1>: fully_preserved - preserve the referenced woman.
<Subject 2>: fully_preserved - preserve the referenced door.
detailed_description:
[Shot 1] <Subject 1> opens <Subject 2> with her left hand.
overall_soundscape:
N/A
non_diegetic_music:
N/A"""


class H3ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.first = self.root / "first.png"
        self.second = self.root / "second.png"
        self.frame = self.root / "frame.png"
        for index, path in enumerate((self.first, self.second, self.frame)):
            path.write_bytes(PNG + bytes([index]))
        self.requests: list[dict] = []
        self.response_content: str | None = json.dumps({"prompt": I2VA})
        self.finish_reason = "stop"
        self.status_code = 200

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            if self.status_code != 200:
                return httpx.Response(
                    self.status_code,
                    json={"error": {"message": "gateway echoed test-key"}},
                )
            return httpx.Response(
                200,
                json={
                    "id": "test-completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "compiler-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": self.finish_reason,
                            "message": {"role": "assistant", "content": self.response_content},
                        }
                    ],
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handle))
        sdk = OpenAI(
            api_key="test-key", base_url="https://example.test/v1", http_client=http_client
        )
        self.addCleanup(sdk.close)
        self.client = H3Client(model="compiler-model", sdk_client=sdk)

    def ref(self, path: Path, remote_name: str, name: str | None = None) -> ReferenceImage:
        return ReferenceImage(path, name=name, remote_name=remote_name)

    def test_reasoning_effort_default_env_and_explicit_override(self) -> None:
        for configured, explicit, expected in (
            ("", None, "low"),
            ("high", None, "high"),
            ("high", "low", "low"),
        ):
            with self.subTest(configured=configured, explicit=explicit):
                with patch.dict("os.environ", {"H3_LLM_REASONING_EFFORT": configured}):
                    client = H3Client(
                        model="compiler-model",
                        sdk_client=self.client._sdk,
                        reasoning_effort=explicit,
                    )
                    client.compile("i2va", "女子推门。", [self.first])
                self.assertEqual(self.requests[-1]["reasoning_effort"], expected)

    def test_invalid_reasoning_effort_is_rejected_before_request(self) -> None:
        with self.assertRaises(H3Error):
            H3Client(
                model="compiler-model", sdk_client=self.client._sdk, reasoning_effort="invalid"
            )
        self.assertEqual(self.requests, [])

    def test_i2va_compilation_is_a_valid_profile_input(self) -> None:
        result = self.client.compile(
            "i2va",
            "图1中的女子用左手推开门。",
            [self.ref(self.first, "codex/first.png", "图1")],
        )
        values = {"seed": 42, "length": 5}
        compiled = result.to_values(values=values)
        self.assertEqual(values, {"seed": 42, "length": 5})
        self.assertEqual(result.profile, "i2va")
        self.assertEqual(compiled["prompt"], I2VA)
        self.assertEqual(compiled["first_frame"], "codex/first.png")
        graph = build_graph(result.profile, compiled)
        self.assertEqual(graph["114"]["inputs"]["image"], "codex/first.png")
        self.assertEqual(graph["139"]["inputs"]["prompt"], I2VA)
        self.assertEqual(graph["903"]["inputs"]["prompt"], I2VA)
        self.assertEqual(graph["137"]["inputs"]["noise_seed"], 42)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request["model"], "compiler-model")
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["messages"][0]["role"], "system")
        system = request["messages"][0]["content"]
        self.assertIn("format compiler", system)
        self.assertIn("Preserve every existing action", system)
        self.assertIn("Do not improve, embellish", system)
        self.assertIn("Do not invent sounds, speech, music or silence", system)
        request_text = json.dumps(request, ensure_ascii=False)
        self.assertIn("图1中的女子用左手推开门", request_text)
        self.assertIn(base64.b64encode(self.first.read_bytes()).decode("ascii"), request_text)

    def test_ref2va_preserves_reference_order_across_all_workflow_slots(self) -> None:
        self.response_content = json.dumps({"prompt": REF2VA})
        images = [
            self.ref(self.first, "codex/woman.png", "女子"),
            self.ref(self.second, "codex/door.png", "门"),
        ]
        result = self.client.compile("ref2va", "女子用左手推开门。", images)
        values = result.to_values()
        self.assertNotIn("first_frame", values)
        self.assertEqual(values["reference_images"], ["codex/woman.png", "codex/door.png"])
        graph = build_graph(result.profile, values)
        self.assertEqual(graph["66"]["inputs"]["image"], "codex/woman.png")
        self.assertEqual(graph["901"]["inputs"]["image"], "codex/door.png")
        for conditioning in ("95", "96"):
            self.assertEqual(graph[conditioning]["inputs"]["ref_images.ref_image_0"], ["66", 0])
            self.assertEqual(graph[conditioning]["inputs"]["ref_images.ref_image_1"], ["901", 0])
        self.assertEqual(graph["28"]["inputs"]["prompt"], REF2VA)
        self.assertNotIn("902", graph)
        self.assertNotIn("903", graph)
        request_text = json.dumps(self.requests[0], ensure_ascii=False)
        self.assertIn(base64.b64encode(self.first.read_bytes()).decode("ascii"), request_text)
        self.assertIn(base64.b64encode(self.second.read_bytes()).decode("ascii"), request_text)
        content = self.requests[0]["messages"][1]["content"]
        pictures = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
        expected = [
            "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            for path in (self.first, self.second)
        ]
        self.assertEqual(pictures, expected)
        self.assertIn("<Picture 1>", content[1]["text"])
        self.assertIn("<Picture 2>", content[3]["text"])

    def test_ref2va_accepts_nine_distinct_images(self) -> None:
        self.response_content = json.dumps({"prompt": REF2VA})
        images = []
        for index in range(9):
            path = self.root / f"reference-{index + 1}.png"
            path.write_bytes(PNG + bytes([index]))
            images.append(self.ref(path, f"codex/reference-{index + 1}.png"))
        result = self.client.compile("ref2va", "女子用左手推开门。", images)
        values = result.to_values()
        self.assertEqual(
            values["reference_images"], [f"codex/reference-{i}.png" for i in range(1, 10)]
        )
        graph = build_graph(result.profile, values)
        self.assertEqual(graph["908"]["inputs"]["image"], "codex/reference-9.png")
        self.assertEqual(graph["95"]["inputs"]["ref_images.ref_image_8"], ["908", 0])

    def test_ref2va_only_includes_audio_slots_explicitly_supplied(self) -> None:
        base = {"prompt": "A scene", "reference_images": ["input/first.png"]}
        graph = build_graph("ref2va", base)
        for node in ("107", "108", "109"):
            self.assertNotIn(node, graph)
        for conditioning in ("95", "96"):
            for index in range(3):
                self.assertNotIn(f"ref_audios.ref_audio_{index}", graph[conditioning]["inputs"])
            self.assertEqual(graph[conditioning]["inputs"]["ref_images.ref_image_0"], ["66", 0])

        graph = build_graph("ref2va", {**base, "reference_audios": ["sound/a.wav", "sound/b.mp3"]})
        self.assertEqual(graph["107"]["inputs"]["audio"], "sound/a.wav")
        self.assertEqual(graph["108"]["inputs"]["audio"], "sound/b.mp3")
        self.assertNotIn("109", graph)
        for conditioning in ("95", "96"):
            self.assertEqual(graph[conditioning]["inputs"]["ref_audios.ref_audio_0"], ["107", 0])
            self.assertEqual(graph[conditioning]["inputs"]["ref_audios.ref_audio_1"], ["108", 0])
            self.assertNotIn("ref_audios.ref_audio_2", graph[conditioning]["inputs"])

    def test_cli_failure_quarantines_only_old_results_and_protects_inputs(self) -> None:
        source = self.root / "request.json"
        source.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "storyboard": "女子推门。",
                    "reference_images": [{"path": str(self.first), "remote_name": "first.png"}],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "values.json"
        env = self.root / ".env.mock"
        env.write_text("H3_LLM_MODEL=unused\n", encoding="utf-8")
        old_values = {"prompt": "old output", "first_frame": "first.png"}
        output.write_text(json.dumps(old_values), encoding="utf-8")
        with (
            patch("h3_client.__main__.H3Client") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value.compile.side_effect = H3Error("LLM failed")
            self.assertEqual(
                h3_main(["--input", str(source), "--output", str(output), "--env-file", str(env)]),
                1,
            )
        # The stale result must not stay at the requested path, but it is moved
        # aside instead of deleted so a failed recompile cannot lose data.
        self.assertFalse(output.exists())
        stale = output.with_name(output.name + ".stale")
        self.assertEqual(json.loads(stale.read_text(encoding="utf-8")), old_values)
        self.assertTrue(source.exists())
        self.assertEqual(self.first.read_bytes(), PNG + bytes([0]))
        self.assertEqual(env.read_text(encoding="utf-8"), "H3_LLM_MODEL=unused\n")

        # Output paths may accidentally name input/config files, even when
        # their content looks like a previous values JSON object.
        disguised_image = self.root / "input.json.png"
        disguised_image.write_text(json.dumps(old_values), encoding="utf-8")
        source.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "storyboard": "女子推门。",
                    "reference_images": [
                        {"path": str(disguised_image), "remote_name": "first.png"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        for protected in (source, disguised_image, env):
            with self.subTest(protected=protected), contextlib.redirect_stderr(io.StringIO()):
                before = protected.read_bytes()
                self.assertEqual(
                    h3_main(
                        ["--input", str(source), "--output", str(protected), "--env-file", str(env)]
                    ),
                    1,
                )
                self.assertEqual(protected.read_bytes(), before)

        # An unrelated JSON file isn't treated as an old values artifact.
        output.write_text('{"other": "preserve"}', encoding="utf-8")
        with (
            patch("h3_client.__main__.H3Client") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value.compile.side_effect = H3Error("LLM failed")
            self.assertEqual(
                h3_main(["--input", str(source), "--output", str(output), "--env-file", str(env)]),
                1,
            )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"other": "preserve"})
        self.assertEqual(json.loads(stale.read_text(encoding="utf-8")), old_values)

    def test_cli_prompt_only_uses_real_images_without_comfy_or_remote_names(self) -> None:
        source = self.root / "request.json"
        source.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "storyboard": "女子用左手推门。",
                    "reference_images": [{"path": str(self.first), "name": "女子"}],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "prompt.txt"
        stdout = io.StringIO()
        with (
            patch("h3_client.__main__.H3Client", return_value=self.client),
            patch("h3_client.__main__.ComfyUIClient") as comfy,
            patch(
                "h3_client.client.H3Compilation.to_values",
                side_effect=AssertionError("must not make values"),
            ),
            patch.dict("os.environ", {"COMFYUI_URL": "", "COMFYUI_BASE_URL": ""}),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(
                h3_main(["--input", str(source), "--output", str(output), "--prompt-only"]), 0
            )
        self.assertEqual(output.read_text(encoding="utf-8"), I2VA + "\n")
        summary = json.loads(stdout.getvalue())
        self.assertEqual(
            summary, {"profile": "i2va", "prompt_file": str(output), "mode": "prompt-only"}
        )
        comfy.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        content = self.requests[0]["messages"][1]["content"]
        image_url = next(
            part["image_url"]["url"] for part in content if part["type"] == "image_url"
        )
        self.assertEqual(
            image_url,
            "data:image/png;base64," + base64.b64encode(self.first.read_bytes()).decode("ascii"),
        )

    def test_cli_default_values_mode_still_requires_and_binds_remote_name(self) -> None:
        source = self.root / "request.json"
        source.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "storyboard": "女子推门。",
                    "reference_images": [
                        {"path": str(self.first), "remote_name": "input/first.png"}
                    ],
                    "values": {"seed": 42},
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "values.json"
        stdout = io.StringIO()
        with (
            patch("h3_client.__main__.H3Client", return_value=self.client),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(h3_main(["--input", str(source), "--output", str(output)]), 0)
        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(value["prompt"], I2VA)
        self.assertEqual(value["first_frame"], "input/first.png")
        self.assertEqual(value["seed"], 42)
        self.assertEqual(
            json.loads(stdout.getvalue()), {"profile": "i2va", "values_file": str(output)}
        )

    def test_cli_prompt_only_rejects_upload_without_calling_llm(self) -> None:
        source = self.root / "request.json"
        source.write_text(
            json.dumps(
                {"type": "i2va", "storyboard": "女子推门。", "reference_images": [str(self.first)]}
            ),
            encoding="utf-8",
        )
        output = self.root / "prompt.txt"
        output.write_text(I2VA + "\n", encoding="utf-8")
        with (
            patch("h3_client.__main__.H3Client") as compiler,
            patch("h3_client.__main__.ComfyUIClient") as comfy,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(
                h3_main(
                    ["--input", str(source), "--output", str(output), "--prompt-only", "--upload"]
                ),
                1,
            )
        self.assertIn("mutually exclusive", stderr.getvalue())
        compiler.assert_not_called()
        comfy.assert_not_called()
        self.assertFalse(output.exists())

    def test_cli_failure_cleans_old_prompt_or_values_but_not_unrelated_text(self) -> None:
        source = self.root / "request.json"
        source.write_text(
            json.dumps(
                {
                    "type": "i2va",
                    "storyboard": "女子推门。",
                    "reference_images": [{"path": str(self.first), "remote_name": "first.png"}],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "output.txt"
        stale = (
            I2VA + "\n",
            REF2VA + "\n",
            json.dumps({"prompt": "old", "first_frame": "first.png"}),
        )
        for old, mode in zip(stale, ([], ["--prompt-only"], ["--prompt-only"]), strict=True):
            with self.subTest(mode=mode, old=old[:25]):
                output.write_text(old, encoding="utf-8")
                with (
                    patch("h3_client.__main__.H3Client") as factory,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    factory.return_value.__enter__.return_value.compile.side_effect = H3Error(
                        "LLM failed"
                    )
                    self.assertEqual(
                        h3_main(["--input", str(source), "--output", str(output), *mode]), 1
                    )
                self.assertFalse(output.exists())
        output.write_text("A user's unrelated notes.\n", encoding="utf-8")
        with (
            patch("h3_client.__main__.H3Client") as factory,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            factory.return_value.__enter__.return_value.compile.side_effect = H3Error("LLM failed")
            self.assertEqual(
                h3_main(["--input", str(source), "--output", str(output), "--prompt-only"]), 1
            )
        self.assertEqual(output.read_text(encoding="utf-8"), "A user's unrelated notes.\n")

    def test_missing_remote_names_can_be_supplied_by_upload(self) -> None:
        result = self.client.compile("i2va", "女子用左手推门。", [self.first])
        with self.assertRaises((ValueError, H3Error)):
            result.to_values()

        class UploadClient:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def upload_file(self, path: str | Path) -> str:
                self.paths.append(str(path))
                return "uploaded/first.png"

        uploader = UploadClient()
        values = result.to_values(uploader)
        self.assertEqual(values["first_frame"], "uploaded/first.png")
        self.assertEqual(uploader.paths, [str(self.first)])
        build_graph(result.profile, values)

    def test_supplied_remote_name_does_not_trigger_upload(self) -> None:
        result = self.client.compile("i2va", "女子推门。", [self.ref(self.first, "in/first.png")])

        class UnexpectedUpload:
            def upload_file(self, path: str | Path) -> str:
                raise AssertionError(f"Unexpected upload: {path}")

        self.assertEqual(result.to_values(UnexpectedUpload())["first_frame"], "in/first.png")

    def test_reserved_values_cannot_replace_compiled_prompt_or_images(self) -> None:
        result = self.client.compile("i2va", "女子推门。", [self.ref(self.first, "in/first.png")])
        for field, value in (
            ("prompt", "replacement"),
            ("first_frame", "other.png"),
            ("reference_images", ["other.png"]),
        ):
            with self.subTest(field=field), self.assertRaises((ValueError, H3Error)):
                result.to_values(values={field: value})

    def test_invalid_inputs_fail_before_contacting_llm(self) -> None:
        cases = (
            ("i2va", []),
            ("i2va", [self.first, self.second]),
            ("ref2va", []),
            ("ref2va", [self.first] * 10),
            ("unknown", [self.first]),
        )
        for profile, images in cases:
            with self.subTest(profile=profile, count=len(images)):
                with self.assertRaises((ValueError, H3Error)):
                    self.client.compile(profile, "一个镜头。", images)
        self.assertEqual(self.requests, [])

    def test_missing_image_and_duplicate_alias_fail_before_contacting_llm(self) -> None:
        with self.assertRaises(H3Error):
            self.client.compile("i2va", "女子推门。", [self.root / "missing.png"])
        images = [
            self.ref(self.first, "one.png", "角色"),
            self.ref(self.second, "two.png", "角色"),
        ]
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "角色推门。", images)
        self.assertEqual(self.requests, [])

    def test_explicit_alias_must_not_collide_with_default_alias(self) -> None:
        images = [
            self.ref(self.first, "one.png"),
            self.ref(self.second, "two.png", "image1"),
        ]
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "image1 推门。", images)
        self.assertEqual(self.requests, [])

    def test_gateway_error_does_not_expose_key(self) -> None:
        self.status_code = 401
        with self.assertRaises(H3Error) as caught:
            self.client.compile("i2va", "女子推门。", [self.first])
        self.assertNotIn("test-key", str(caught.exception))
        formatted = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("test-key", formatted)

    def test_llm_errors_and_invalid_output_are_rejected(self) -> None:
        cases = (
            (json.dumps({"error": "The reference is ambiguous."}), "stop"),
            ("not json", "stop"),
            (None, "stop"),
            (json.dumps({"prompt": I2VA}), "length"),
            (json.dumps({"prompt": "[Shot 1] A door opens."}), "stop"),
            (json.dumps({"prompt": I2VA.replace("<Picture 1>", "<Picture 2>")}), "stop"),
        )
        for content, finish in cases:
            with self.subTest(content=content, finish=finish):
                self.response_content = content
                self.finish_reason = finish
                with self.assertRaises((ValueError, H3Error)):
                    self.client.compile("i2va", "女子推门。", [self.first])

    def test_out_of_range_ref2va_picture_label_is_rejected(self) -> None:
        self.response_content = json.dumps({"prompt": REF2VA.replace("<Picture 2>", "<Picture 3>")})
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "女子推门。", [self.first, self.second])

    def test_invalid_cut_times_and_empty_sections_are_rejected(self) -> None:
        description = "[Shot 1] One.\n[Shot 2] At 00:02.000, two.\n[Shot 3] At 00:03.000, three."
        cases = (
            I2VA.replace(
                "[Shot 1] The woman opens the door with her left hand.",
                description.replace("00:03.000", "00:02.000"),
            ),
            I2VA.replace(
                "[Shot 1] The woman opens the door with her left hand.",
                description.replace("00:03.000", "00:01.000"),
            ),
            I2VA.replace(
                "[Shot 1] The woman opens the door with her left hand.",
                description.replace(" At 00:02.000,", ""),
            ),
            I2VA.replace(
                "[Shot 1] The woman opens the door with her left hand.",
                description.replace("00:02.000", "00:62.000"),
            ),
            I2VA.replace("overall_soundscape: N/A", "overall_soundscape:"),
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.response_content = json.dumps({"prompt": prompt})
                with self.assertRaises((ValueError, H3Error)):
                    self.client.compile("i2va", "女子推门。", [self.first])

    def test_first_shot_must_not_have_a_cut_timestamp(self) -> None:
        self.response_content = json.dumps(
            {"prompt": I2VA.replace("[Shot 1] The woman", "[Shot 1] At 00:00.000, the woman")}
        )
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("i2va", "女子推门。", [self.first])

    def test_cuts_at_or_after_length_are_rejected_before_upload(self) -> None:
        class UploadClient:
            def __init__(self) -> None:
                self.calls = 0

            def upload_file(self, path: str | Path) -> str:
                self.calls += 1
                return "uploaded/first.png"

        for timestamp in ("00:05.000", "00:06.000"):
            with self.subTest(timestamp=timestamp):
                description = f"[Shot 1] One.\n[Shot 2] At {timestamp}, two."
                self.response_content = json.dumps(
                    {
                        "prompt": I2VA.replace(
                            "[Shot 1] The woman opens the door with her left hand.", description
                        )
                    }
                )
                result = self.client.compile("i2va", "女子推门。", [self.first])
                uploader = UploadClient()
                with self.assertRaises((ValueError, H3Error)):
                    result.to_values(uploader, values={"length": 5})
                self.assertEqual(uploader.calls, 0)

    def test_ref2va_undefined_subject_is_rejected(self) -> None:
        self.response_content = json.dumps(
            {"prompt": REF2VA.replace("[Shot 1] <Subject 1> opens", "[Shot 1] <Subject 3> opens")}
        )
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "女子推门。", [self.first, self.second])

    def test_ref2va_chinese_dialogue_keeps_original_punctuation(self) -> None:
        dialogue = "<Subject 1> (S1) says, <d>[Chinese] 你来了？快进来！</d>"
        prompt = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
            f"[Shot 1] <Subject 1> opens <Subject 2> with her left hand. {dialogue}",
        )
        self.response_content = json.dumps({"prompt": prompt}, ensure_ascii=False)
        result = self.client.compile(
            "ref2va", "女子推门说：你来了？快进来！", [self.first, self.second]
        )
        self.assertEqual(result.prompt, prompt)
        self.assertIn(dialogue, result.prompt)

    def test_ref2va_rejects_malformed_dialogue_or_missing_speaker(self) -> None:
        cases = (
            "<Subject 1> says, <d>[Chinese] 你来了？</d>",
            "<Subject 1> (S1) says, <d>你来了？</d>",
            "<Subject 1> (S1) says, <d>[Chinese] 你来了？",
        )
        for dialogue in cases:
            with self.subTest(dialogue=dialogue):
                prompt = REF2VA.replace(
                    "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
                    f"[Shot 1] <Subject 1> opens <Subject 2> with her left hand. {dialogue}",
                )
                self.response_content = json.dumps({"prompt": prompt}, ensure_ascii=False)
                with self.assertRaises((ValueError, H3Error)):
                    self.client.compile("ref2va", "女子推门说：你来了？", [self.first, self.second])

    def test_known_heading_case_is_normalized_without_changing_body(self) -> None:
        normalized = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
            '[Shot 1] <Subject 1> opens <Subject 2>. The SIGN reads "WELCOME".',
        )
        mixed_case = normalized.replace("detailed_description:", "Detailed_description:")
        self.response_content = json.dumps({"prompt": mixed_case}, ensure_ascii=False)
        result = self.client.compile("ref2va", "女子推门。", [self.first, self.second])
        self.assertEqual(result.prompt, normalized)

        unknown = normalized.replace("detailed_description:", "hyper-detailed description:")
        self.response_content = json.dumps({"prompt": unknown}, ensure_ascii=False)
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "女子推门。", [self.first, self.second])

    def test_i2va_three_shots_compile_and_bind_within_length(self) -> None:
        description = (
            "[Shot 1] The woman reaches for the door.\n"
            "[Shot 2] At 00:02.000, she opens it with her left hand.\n"
            "[Shot 3] At 00:04.000, she steps through."
        )
        prompt = I2VA.replace("[Shot 1] The woman opens the door with her left hand.", description)
        self.response_content = json.dumps({"prompt": prompt})
        result = self.client.compile(
            "i2va", "女子推门并走进去。", [self.ref(self.first, "input/frame.png")]
        )
        values = result.to_values(values={"length": 6})
        self.assertEqual(values["prompt"], prompt)
        self.assertEqual(values["length"], 6)
        graph = build_graph(result.profile, values)
        self.assertEqual(graph["139"]["inputs"]["prompt"], prompt)

    def test_only_empty_consecutive_duplicate_headings_are_collapsed(self) -> None:
        duplicated = REF2VA.replace(
            "detailed_description:", "Detailed_description:\n\ndetailed_description:"
        )
        self.response_content = json.dumps({"prompt": duplicated})
        compiled = self.client.compile("ref2va", "女子推门。", [self.first, self.second])
        self.assertEqual(compiled.prompt, REF2VA)

        with_content = REF2VA.replace(
            "detailed_description:",
            "detailed_description: An additional scene.\ndetailed_description:",
        )
        self.response_content = json.dumps({"prompt": with_content})
        with self.assertRaises(H3Error):
            self.client.compile("ref2va", "女子推门。", [self.first, self.second])

    def test_compile_rejects_a_dropped_structured_dialogue_end_time(self) -> None:
        source = "台词（仅此栏为实际对白，保持原话与标点）：3.0–4.6s Woman (Mandarin):「请稍等。」"
        line = (
            "The woman holds her position. "
            "From 3.0 to 4.6 seconds, <Subject 1> (S1) says: <d>[Mandarin] 请稍等。</d>"
        )
        valid = REF2VA.replace("<Subject 1> opens <Subject 2> with her left hand.", line)
        self.response_content = json.dumps({"prompt": valid}, ensure_ascii=False)
        self.assertEqual(
            self.client.compile("ref2va", source, [self.first, self.second]).prompt, valid
        )
        missing_end = valid.replace("From 3.0 to 4.6 seconds", "At 3.0 seconds")
        self.response_content = json.dumps({"prompt": missing_end}, ensure_ascii=False)
        with self.assertRaisesRegex(H3Error, "original time range"):
            self.client.compile("ref2va", source, [self.first, self.second])

    def test_ref2va_dialogue_can_continue_across_a_cut(self) -> None:
        description = (
            "[Shot 1] <Subject 1> (S1) says, <d>[Chinese] 你来了，<scenetrans></d>\n"
            "[Shot 2] At 00:02.000, <Subject 1> (S1) continues, "
            "<d>[Chinese] <scenetrans>快进来！</d>"
        )
        prompt = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.", description
        )
        self.response_content = json.dumps({"prompt": prompt}, ensure_ascii=False)
        result = self.client.compile(
            "ref2va", "女子说：你来了，快进来！", [self.first, self.second]
        )
        self.assertEqual(result.prompt, prompt)
        chunks = re.findall(r"<d>\[Chinese\]\s*(.*?)</d>", result.prompt)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            "".join(chunk.replace("<scenetrans>", "") for chunk in chunks), "你来了，快进来！"
        )

        missing_speaker = prompt.replace("<Subject 1> (S1) continues", "<Subject 1> continues")
        self.response_content = json.dumps({"prompt": missing_speaker}, ensure_ascii=False)
        with self.assertRaises((ValueError, H3Error)):
            self.client.compile("ref2va", "女子说：你来了，快进来！", [self.first, self.second])

    def test_dialogue_accepts_cutoff_but_rejects_unknown_or_closing_tokens(self) -> None:
        base = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
            "[Shot 1] <Subject 1> (S1) says, <d>[Chinese] 等一下<cutoff></d>",
        )
        self.response_content = json.dumps({"prompt": base}, ensure_ascii=False)
        result = self.client.compile("ref2va", "女子说：等一下", [self.first, self.second])
        self.assertEqual(result.prompt, base)

        for token in ("<tag>", "</scenetrans>"):
            with self.subTest(token=token):
                self.response_content = json.dumps(
                    {"prompt": base.replace("<cutoff>", token)}, ensure_ascii=False
                )
                with self.assertRaises((ValueError, H3Error)):
                    self.client.compile("ref2va", "女子说：等一下", [self.first, self.second])

    def test_dialogue_rejects_empty_control_only_chunks(self) -> None:
        for body in ("<scenetrans>", "<cutoff>", "  <scenetrans> <cutoff>  "):
            with self.subTest(body=body):
                prompt = REF2VA.replace(
                    "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
                    f"[Shot 1] <Subject 1> (S1) says, <d>[Chinese] {body}</d>",
                )
                self.response_content = json.dumps({"prompt": prompt}, ensure_ascii=False)
                with self.assertRaises(H3Error):
                    self.client.compile("ref2va", "女子说话。", [self.first, self.second])

    def test_speaker_ids_are_positive_ordered_and_stable_for_subjects(self) -> None:
        base = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
            "[Shot 1] <Subject 1> (S1) says <d>[Chinese] 你来了。</d>\n"
            "[Shot 2] At 00:02.000, <Subject 1> (S1) continues off-screen "
            "<d>[Chinese] 快进来。</d>",
        )
        for prompt in (
            base.replace("(S1)", "(S0)"),
            base.replace("(S1)", "(S2)"),
            base.replace("<Subject 1> (S1) continues", "<Subject 1> (S2) continues"),
            base.replace("<Subject 1> (S1) continues", "<Subject 2> (S1) continues"),
        ):
            with self.subTest(prompt=prompt):
                self.response_content = json.dumps({"prompt": prompt}, ensure_ascii=False)
                with self.assertRaises(H3Error):
                    self.client.compile(
                        "ref2va", "女子说：你来了，快进来。", [self.first, self.second]
                    )
        self.response_content = json.dumps({"prompt": base}, ensure_ascii=False)
        self.assertEqual(
            self.client.compile("ref2va", "女子说话。", [self.first, self.second]).prompt, base
        )

        group = REF2VA.replace(
            "[Shot 1] <Subject 1> opens <Subject 2> with her left hand.",
            "[Shot 1] <Subject 1> (S1) says <d>[Chinese] 你好。</d> "
            "<Subject 2> (S2) says <d>[Chinese] 你好。</d>\n"
            "[Shot 2] At 00:02.000, <Subject 1> and <Subject 2> (S1,S2) say "
            "<d>[Chinese] 走吧。</d>",
        )
        self.response_content = json.dumps({"prompt": group}, ensure_ascii=False)
        self.assertEqual(
            self.client.compile("ref2va", "他们说话。", [self.first, self.second]).prompt, group
        )

    def test_crlf_and_zero_padded_markers_are_normalized(self) -> None:
        padded = I2VA.replace("<Picture 1>", "<Picture 01>")
        self.response_content = json.dumps({"prompt": padded.replace("\n", "\r\n")})
        result = self.client.compile("i2va", "女子推门。", [self.first])
        self.assertEqual(result.prompt, I2VA)

    def test_content_parts_are_reported_as_h3_error(self) -> None:
        class ContentParts:
            chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_: SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason="stop",
                                message=SimpleNamespace(content=[{"type": "text"}], refusal=None),
                            )
                        ]
                    )
                )
            )

        client = H3Client(model="compiler-model", sdk_client=ContentParts())
        with self.assertRaises(H3Error):
            client.compile("i2va", "女子推门。", [self.first])

    def test_multi_shot_requires_length_and_accepts_fractional_length(self) -> None:
        description = "[Shot 1] One.\n[Shot 2] At 00:02.000, two."
        prompt = I2VA.replace("[Shot 1] The woman opens the door with her left hand.", description)
        self.response_content = json.dumps({"prompt": prompt})
        result = self.client.compile("i2va", "女子推门。", [self.ref(self.first, "in/first.png")])
        with self.assertRaises(H3Error):
            result.to_values()
        values = result.to_values(values={"length": 2.5})
        self.assertEqual(values["length"], 2.5)
        self.assertEqual(build_graph(result.profile, values)["141"]["inputs"]["value"], 2.5)

    def test_inline_shot_markers_are_rejected(self) -> None:
        description = "[Shot 1] One. [Shot 2] At 00:02.000, two."
        prompt = I2VA.replace("[Shot 1] The woman opens the door with her left hand.", description)
        self.response_content = json.dumps({"prompt": prompt})
        with self.assertRaises(H3Error):
            self.client.compile("i2va", "女子推门。", [self.first])

    def test_invalid_remote_name_and_hand_built_compilations_raise_h3_error(self) -> None:
        with self.assertRaises(H3Error):
            ReferenceImage(self.first, remote_name="../escape.png")
        cases = (
            ("i2va", ()),
            ("ref2va", ()),
            ("unknown", (self.ref(self.first, "a.png"),)),
        )
        for profile, images in cases:
            with self.subTest(profile=profile), self.assertRaises(H3Error):
                H3Compilation(profile, I2VA, images)

    def test_oversized_reference_image_is_rejected_before_the_request(self) -> None:
        with patch("h3_client.client.MAX_IMAGE_BYTES", 4), self.assertRaises(H3Error):
            self.client.compile("i2va", "女子推门。", [self.first])
        self.assertEqual(self.requests, [])

    def test_system_prompt_matches_the_declared_constants(self) -> None:
        self.assertIn(I2VA_HEADER, SYSTEM_PROMPT)
        for fields in (I2VA_FIELDS, REF2VA_FIELDS):
            with self.subTest(fields=fields):
                self.assertIn("\n".join(f"{field}:" for field in fields), SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
