from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from comfyui_client import (
    BindingError,
    ComfyUIClient,
    ComfyUIError,
    WorkflowExecutionError,
    WorkflowTemplate,
    get_default_template,
)


class FakeComfyHandler(BaseHTTPRequestHandler):
    history_record = {
        "status": {"completed": True, "status_str": "success"},
        "outputs": {
            "43": {"images": [{"filename": "../fake.png", "subfolder": "", "type": "output"}]}
        },
    }
    submitted_graph = None
    uploaded_payload = None

    def log_message(self, format, *args):
        pass

    def _json(self, code, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/prompt":
            return self._json(200, {"exec_info": {"queue_remaining": 0}})
        if path == "/object_info/TestNode":
            return self._json(200, {"TestNode": {"input": {}}})
        if path == "/history/job-1":
            return self._json(200, {"job-1": self.history_record})
        if path == "/view":
            body = b"safe-output-bytes"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "missing"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/prompt":
            payload = json.loads(body)
            type(self).submitted_graph = payload["prompt"]
            return self._json(200, {"prompt_id": "job-1", "number": 1})
        if self.path == "/upload/image":
            type(self).uploaded_payload = body
            subfolder = (
                body.split(b'name="subfolder"', 1)[1]
                .split(b"\r\n\r\n", 1)[1]
                .split(b"\r\n", 1)[0]
                .decode()
            )
            return self._json(200, {"name": "source.png", "subfolder": subfolder, "type": "input"})
        self._json(404, {"error": "missing"})


class ComfyClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        FakeComfyHandler.history_record = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "43": {"images": [{"filename": "../fake.png", "subfolder": "", "type": "output"}]}
            },
        }
        FakeComfyHandler.submitted_graph = None
        FakeComfyHandler.uploaded_payload = None
        self.client = ComfyUIClient(self.base_url, api_key="secret", timeout=2, poll_interval=0.01)

    def tearDown(self):
        self.client.close()

    def test_profiles_bind_existing_nodes_without_mutating_source(self):
        template = get_default_template("qwen")
        before = template._graph["37"]["inputs"]["prompt"]
        graph = template.prepare("a castle", seed=17, width=768, height=512)
        self.assertEqual(graph["37"]["inputs"]["prompt"], "a castle")
        self.assertEqual(graph["23"]["inputs"]["seed"], 17)
        self.assertEqual(graph["36"]["inputs"]["width"], 768)
        self.assertEqual(graph["36"]["inputs"]["height"], 512)
        self.assertEqual(template._graph["37"]["inputs"]["prompt"], before)
        self.assertEqual(template.reference_capacity, 10)
        self.assertFalse(template.has_first_frame)
        with self.assertRaisesRegex(BindingError, "supports 10"):
            template.prepare("a castle", reference_images=[f"input/{i}.png" for i in range(11)])
        one_ref = template.prepare("a castle", reference_images=["input/ref.png"])
        self.assertEqual(one_ref["37"]["inputs"]["images.image_1"], ["44", 0])
        self.assertNotIn("images.image_2", one_ref["37"]["inputs"])
        self.assertNotIn("45", one_ref)

    def test_video_profiles_declare_first_frame_and_zero_extra_references(self):
        for profile in ("ref2va", "i2va"):
            template = get_default_template(profile)
            self.assertTrue(template.capabilities["has_first_frame"])
            if profile == "ref2va":
                self.assertEqual(template.capabilities["reference_capacity"], 8)
                self.assertEqual(template.capabilities["audio_reference_capacity"], 3)
                first_plus_assets = template.prepare(
                    "video prompt",
                    first_frame="frames/first.png",
                    reference_images=[f"input/{i}.png" for i in range(8)],
                    reference_audios=["audio/voice.wav"],
                )
                self.assertEqual(
                    first_plus_assets["95"]["inputs"]["ref_images.ref_image_8"], ["908", 0]
                )
                self.assertEqual(
                    first_plus_assets["96"]["inputs"]["ref_audios.ref_audio_0"], ["107", 0]
                )
                self.assertEqual(first_plus_assets["107"]["inputs"]["audio"], "audio/voice.wav")
            else:
                self.assertEqual(template.capabilities["reference_capacity"], 0)
                with self.assertRaises(BindingError):
                    template.prepare(
                        "video prompt",
                        first_frame="first.png",
                        reference_images=["input/asset.png"],
                    )
        ref = get_default_template("ref2va").prepare("video", first_frame="codex/first.png")
        self.assertEqual(ref["66"]["inputs"]["image"], "codex/first.png")
        self.assertNotIn("107", ref)
        i2v = get_default_template("i2va").prepare("video", first_frame="codex/first.png")
        self.assertEqual(i2v["114"]["inputs"]["image"], "codex/first.png")
        self.assertEqual(i2v["903"]["inputs"]["prompt"], "video")

    def test_optional_loaders_are_removed_until_media_is_configured(self):
        qwen = get_default_template("qwen").prepare("portrait")
        self.assertFalse(any(str(node_id) in qwen for node_id in range(44, 54)))
        self.assertFalse(any(key.startswith("images.image_") for key in qwen["37"]["inputs"]))
        ref2va = get_default_template("ref2va").prepare("video", first_frame="frames/first.png")
        self.assertEqual(ref2va["66"]["inputs"]["image"], "frames/first.png")
        for node_id in (*map(str, range(901, 909)), "107", "108", "109"):
            self.assertNotIn(node_id, ref2va)
        self.assertFalse(any(key.startswith("ref_audios.") for key in ref2va["95"]["inputs"]))
        self.assertEqual(
            [key for key in ref2va["95"]["inputs"] if key.startswith("ref_images.ref_image_")],
            ["ref_images.ref_image_0"],
        )

    def test_custom_profile_expands_only_real_slots(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "empty.png"}},
            "2": {
                "class_type": "Condition",
                "inputs": {"prompt": "", "ref_images.ref_image_1": ["1", 0]},
            },
        }
        profile = {
            "bindings": {"prompt": [{"node_id": "2", "input": "prompt", "type": "str"}]},
            "first_frame": None,
            "reference_slots": [
                {
                    "load": {"node_id": "1", "input": "image", "type": "str"},
                    "conditioning": [{"node_id": "2", "input": "ref_images.ref_image_1"}],
                }
            ],
        }
        template = WorkflowTemplate(graph, profile)
        bound = template.prepare("video", reference_images=["codex/asset.png"])
        self.assertEqual(bound["1"]["inputs"]["image"], "codex/asset.png")
        self.assertEqual(bound["2"]["inputs"]["ref_images.ref_image_1"], ["1", 0])
        with self.assertRaisesRegex(BindingError, "supports 1"):
            template.prepare("video", reference_images=["a.png", "b.png"])

    def test_binding_checks_node_input_and_type(self):
        template = get_default_template("qwen")
        with self.assertRaises(BindingError):
            template.prepare("prompt", seed="not-an-int")
        self.assertEqual(template.prepare("prompt", seed=0)["23"]["inputs"]["seed"], 0)
        with self.assertRaises(BindingError):
            WorkflowTemplate({"1": {"inputs": {}}}, {"bindings": {}}).prepare("prompt")

    def test_submission_strips_ui_metadata_and_never_retries(self):
        template = get_default_template("qwen")
        graph = template.prepare("one request", seed=2)
        prompt_id = self.client.submit(graph, client_id="test-client")
        self.assertEqual(prompt_id, "job-1")
        self.assertNotIn("_api_config", FakeComfyHandler.submitted_graph)
        self.assertEqual(FakeComfyHandler.submitted_graph["37"]["inputs"]["prompt"], "one request")

    def test_submit_error_is_reported_without_retry(self):
        with patch.object(
            self.client, "_request", side_effect=ComfyUIError("server rejected", endpoint="/prompt")
        ) as request:
            with self.assertRaises(ComfyUIError):
                self.client.submit({"1": {"class_type": "Node", "inputs": {}}})
        self.assertEqual(request.call_count, 1)

    def test_wait_failure_and_timeout_include_prompt_id(self):
        FakeComfyHandler.history_record = {
            "status": {"completed": True, "status_str": "error", "messages": ["node failed"]},
            "outputs": {},
        }
        with self.assertRaises(WorkflowExecutionError) as caught:
            self.client.wait("job-1", timeout=1)
        self.assertEqual(caught.exception.prompt_id, "job-1")
        with (
            patch.object(self.client, "history", return_value=None),
            patch("comfyui_client.client.time.sleep"),
        ):
            with self.assertRaises(WorkflowExecutionError) as caught:
                self.client.wait("job-2", timeout=0.01)
        self.assertEqual(caught.exception.prompt_id, "job-2")

    def test_health_object_info_submit_wait_and_download(self):
        self.assertEqual(self.client.health()["exec_info"]["queue_remaining"], 0)
        self.assertIn("TestNode", self.client.object_info("TestNode"))
        prompt_id = self.client.submit({"1": {"class_type": "TestNode", "inputs": {}}})
        history = self.client.wait(prompt_id, timeout=1)
        with tempfile.TemporaryDirectory() as tmp:
            files = self.client.download_outputs(history, tmp)
            self.assertEqual(len(files), 1)
            self.assertTrue(Path(files[0]).is_absolute())
            self.assertEqual(Path(files[0]).read_bytes(), b"safe-output-bytes")
            self.assertEqual(Path(tmp).resolve(), Path(files[0]).parent)

    def test_upload_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "original image.png"
            source.write_bytes(b"original-image-bytes")
            before = source.read_bytes()
            remote = self.client.upload_image(source, subfolder="codex/assets")
            self.assertEqual(remote, "codex/assets/source.png")
            self.assertIn(b"original-image-bytes", FakeComfyHandler.uploaded_payload)
            self.assertEqual(source.read_bytes(), before)

    def test_upload_rejects_unsafe_subfolder_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "image.png"
            source.write_bytes(b"bytes")
            with self.assertRaises(ValueError):
                self.client.upload_image(source, subfolder="../outside")
            with self.assertRaises(ValueError):
                self.client.upload_image(source, subfolder="C:\\outside")


if __name__ == "__main__":
    unittest.main()
