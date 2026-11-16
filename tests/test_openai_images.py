"""Offline tests: no real credentials, paid requests, or SDK network calls."""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from openai_images.__main__ import main  # noqa: E402
from openai_images.client import (  # noqa: E402
    BASE_URL,
    ImageRequest,
    OpenAIImagesClient,
    _create_sdk,
)
from openai_images.errors import ImageError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-offline-fixture"
JPEG = b"\xff\xd8\xff" + b"fake-offline-fixture"
WEBP = b"RIFF0000WEBP" + b"fake-offline-fixture"


def response(raw=PNG, n=1):
    return SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(raw).decode()) for _ in range(n)]
    )


class OpenAIImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_needs_no_key_sdk_or_output_directory(self):
        with patch("openai_images.client._create_sdk", side_effect=AssertionError("SDK touched")):
            code, out, err = self.cli(
                [
                    "generate",
                    "--prompt",
                    "private prompt",
                    "--output-dir",
                    str(self.root / "missing"),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "dry_run")
        self.assertNotIn("private prompt", out)
        self.assertFalse((self.root / "missing").exists())
        self.assertFalse(err)

    def test_prompt_file_utf8_and_model_environment(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("\ufeff中文 prompt", encoding="utf-8")
        with patch.dict("os.environ", {"OPENAI_IMAGE_MODEL": "gpt-image-2.6-flare"}):
            code, out, _ = self.cli(["generate", "--prompt-file", str(prompt)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["model"], "gpt-image-2.6-flare")
        prompt.write_bytes(b"\xff")
        self.assertEqual(self.cli(["generate", "--prompt-file", str(prompt)])[0], 1)

    def test_check_is_local_and_redacts_key(self):
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-secret"}),
            patch("openai_images.client.importlib.util.find_spec", return_value=None),
        ):
            code, out, _ = self.cli(["check"])
        self.assertEqual(code, 0)
        self.assertNotIn("fake-secret", out)
        self.assertFalse(json.loads(out)["network_requested"])
        self.assertFalse(json.loads(out)["sdk_installed"])

    def test_execution_requires_key(self):
        with patch("openai_images.client._create_sdk") as factory:
            code, _, err = self.cli(["generate", "--prompt", "test", "--execute"])
        self.assertEqual(code, 1)
        self.assertIn("OPENAI_API_KEY", err)
        factory.assert_not_called()

    def test_sdk_absence_is_clean(self):
        with patch.dict(sys.modules, {"openai": None}):
            with self.assertRaisesRegex(ImageError, "Install"):
                _create_sdk("fake", 1)

    def test_sdk_pins_endpoint_and_disables_retries_and_proxies(self):
        sdk = MagicMock()
        with (
            patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=sdk)}),
            patch("httpx.Client") as transport,
            patch.dict("os.environ", {"OPENAI_BASE_URL": "https://wrong.invalid"}),
        ):
            _create_sdk("fake", 19)
        self.assertEqual(sdk.call_args.kwargs["base_url"], BASE_URL)
        self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
        self.assertEqual(sdk.call_args.kwargs["timeout"], 19)
        self.assertFalse(transport.call_args.kwargs["trust_env"])
        self.assertFalse(transport.call_args.kwargs["follow_redirects"])

    def test_generate_formats_multiple_outputs_and_no_overwrite(self):
        for fmt, raw in [("png", PNG), ("jpeg", JPEG), ("webp", WEBP)]:
            with self.subTest(fmt=fmt):
                sdk = MagicMock()
                sdk.images.generate.return_value = response(raw, 2)
                original = self.root / f"image-01.{fmt}"
                original.write_bytes(b"original")
                with (
                    patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}),
                    patch("openai_images.client._create_sdk", return_value=sdk),
                ):
                    req = ImageRequest("test", n=2, output_format=fmt)
                    first = OpenAIImagesClient().run(req, self.root, execute=True)
                    second = OpenAIImagesClient().run(req, self.root, execute=True)
                self.assertNotEqual(first["run_dir"], second["run_dir"])
                self.assertEqual(original.read_bytes(), b"original")
                directory = Path(first["run_dir"])
                self.assertEqual((directory / f"image-01.{fmt}").read_bytes(), raw)
                self.assertEqual(len(first["outputs"]), 2)
                self.assertEqual(
                    json.loads((directory / "run.json").read_text())["status"], "completed"
                )
                self.assertNotIn("input_fidelity", sdk.images.generate.call_args.kwargs)
                sdk.close.assert_called()

    def test_edit_uses_ordered_open_files_and_closes_handles(self):
        paths = [self.root / "a.png", self.root / "b.jpg"]
        paths[0].write_bytes(PNG)
        paths[1].write_bytes(JPEG)
        sdk = MagicMock()
        captured = []

        def edit(**kwargs):
            captured.extend(kwargs["image"])
            self.assertEqual([f.read() for f in captured], [PNG, JPEG])
            self.assertNotIn("input_fidelity", kwargs)
            return response()

        sdk.images.edit.side_effect = edit
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}),
            patch("openai_images.client._create_sdk", return_value=sdk),
        ):
            code, _, _ = self.cli(
                [
                    "edit",
                    "--prompt",
                    "test",
                    "--image",
                    str(paths[0]),
                    "--image",
                    str(paths[1]),
                    "--execute",
                    "--output-dir",
                    str(self.root),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(all(f.closed for f in captured))
        self.assertEqual(paths[0].read_bytes(), PNG)
        sdk.images.generate.assert_not_called()

    def test_invalid_inputs_fail_before_client(self):
        fake = self.root / "bad.png"
        fake.write_bytes(b"not png")
        jpg = self.root / "wrong.jpg"
        jpg.write_bytes(PNG)
        requests = [
            ImageRequest(""),
            ImageRequest("x", n=0),
            ImageRequest("x", n=11),
            ImageRequest("x", operation="edit"),
            ImageRequest("x", images=(fake,)),
            ImageRequest("x", operation="edit", images=(fake,)),
            ImageRequest("x", operation="edit", images=(jpg,)),
            ImageRequest("x", operation="edit", images=(self.root,)),
            ImageRequest("x", operation="edit", images=(self.root / "absent.png",)),
            ImageRequest("x", size="999x1024"),
            ImageRequest("x", size="9000x9000"),
            ImageRequest("x", model="dall-e-3"),
            ImageRequest("x", model="gpt-image-1.5", size="1536x864"),
            ImageRequest("x", model="gpt-image-2", quality="max"),
        ]
        with patch("openai_images.client._create_sdk") as factory:
            for req in requests:
                with self.subTest(req=req), self.assertRaises(ImageError):
                    OpenAIImagesClient().run(req, self.root, execute=True)
            factory.assert_not_called()
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ImageError):
                OpenAIImagesClient(request_timeout=timeout)

    def test_output_path_failure_precedes_paid_call(self):
        blocked = self.root / "file"
        blocked.write_text("keep")
        sdk = MagicMock()
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}),
            patch("openai_images.client._create_sdk", return_value=sdk),
            self.assertRaises(ImageError),
        ):
            OpenAIImagesClient().run(ImageRequest("x"), blocked, execute=True)
        sdk.images.generate.assert_not_called()
        self.assertEqual(blocked.read_text(), "keep")

    def test_api_error_redacted_no_retry_and_failed_record(self):
        sdk = MagicMock()
        sdk.images.generate.side_effect = RuntimeError("fake-secret raw-response")
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-secret"}),
            patch("openai_images.client._create_sdk", return_value=sdk),
        ):
            code, _, err = self.cli(
                ["generate", "--prompt", "test", "--execute", "--output-dir", str(self.root)]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("fake-secret", err)
        self.assertNotIn("raw-response", err)
        sdk.images.generate.assert_called_once()
        record = next(self.root.glob("*/run.json")).read_text()
        self.assertNotIn("fake-secret", record)
        self.assertEqual(json.loads(record)["status"], "failed")

    def test_bad_responses_never_write_images(self):
        for result in [
            SimpleNamespace(data=[]),
            response(n=2),
            SimpleNamespace(data=[SimpleNamespace(b64_json="%%%")]),
            response(b"not-image"),
            response(JPEG),
        ]:
            sdk = MagicMock()
            sdk.images.generate.return_value = result
            with (
                patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}),
                patch("openai_images.client._create_sdk", return_value=sdk),
                self.assertRaises(ImageError),
            ):
                OpenAIImagesClient().run(ImageRequest("x"), self.root, execute=True)
        self.assertFalse(list(self.root.glob("*/*.png")))

    def test_real_sdk_offline_serialization_and_single_attempt(self):
        # Optional SDK tests use an in-memory transport, never a real HTTP socket.
        try:
            import openai  # noqa: F401
        except ImportError:
            self.skipTest("optional openai SDK is not installed")
        import httpx

        original_client = httpx.Client
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                self.assertEqual(request.url.path, "/v1/images/generations")
                self.assertEqual(request.url.host, "api.openai.com")
                self.assertEqual(request.headers["authorization"], "Bearer fake-offline")
                body = json.loads(request.content)
                self.assertEqual(body["model"], "gpt-image-2.6-sunburst")
                self.assertEqual(body["output_format"], "png")
                self.assertNotIn("input_fidelity", body)
                return httpx.Response(
                    200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}
                )
            return httpx.Response(
                429, json={"error": {"message": "secret-response", "type": "rate_limit"}}
            )

        class OfflineClient(original_client):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        with (
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "fake-offline", "OPENAI_BASE_URL": "https://wrong.invalid"},
            ),
            patch("httpx.Client", OfflineClient),
        ):
            result = OpenAIImagesClient().run(ImageRequest("test"), self.root, execute=True)
            self.assertEqual(result["status"], "completed")
            with self.assertRaises(ImageError) as caught:
                OpenAIImagesClient().run(ImageRequest("test"), self.root, execute=True)
            self.assertNotIn("secret-response", str(caught.exception))
        self.assertEqual(len(calls), 2)  # One success and one 429; zero retry.

    def test_real_sdk_edit_multipart_and_redirect_not_followed(self):
        try:
            import openai  # noqa: F401
        except ImportError:
            self.skipTest("optional openai SDK is not installed")
        import httpx

        original_client = httpx.Client
        image = self.root / "reference.png"
        image.write_bytes(PNG)
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.path, "/v1/images/edits")
            self.assertIn("multipart/form-data", request.headers["content-type"])
            content = request.read()
            self.assertIn(b'filename="reference.png"', content)
            self.assertIn(PNG, content)
            self.assertNotIn(b"input_fidelity", content)
            return httpx.Response(307, headers={"location": "https://wrong.invalid/images"})

        class OfflineClient(original_client):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-offline"}),
            patch("httpx.Client", OfflineClient),
            self.assertRaises(ImageError),
        ):
            OpenAIImagesClient().run(
                ImageRequest("test", operation="edit", images=(image,)), self.root, execute=True
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, "api.openai.com")

    def test_execute_dry_run_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["generate", "--prompt", "x", "--execute", "--dry-run"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
