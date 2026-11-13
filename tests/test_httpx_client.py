"""HTTP transport behavior; no real ComfyUI server or GPU is required."""

from __future__ import annotations

import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from comfyui_client import ComfyUIClient, ComfyUIError, WorkflowExecutionError


class _BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial-video"
        raise httpx.ReadError("remote disconnected")

    def close(self):
        pass


class HttpxClientTests(unittest.TestCase):
    def test_submit_never_retries_and_redacts_network_error(self):
        requests = []
        token = "private-token-123"

        def handler(request):
            requests.append(request)
            raise httpx.ConnectError(f"request failed with {token}", request=request)

        with ComfyUIClient(
            "http://comfy.invalid", api_key=token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(ComfyUIError) as caught:
                client.submit({"1": {"class_type": "TestNode", "inputs": {}}})
        self.assertEqual(len(requests), 1)
        self.assertEqual(caught.exception.endpoint, "/prompt")
        self.assertNotIn(token, "".join(traceback.format_exception(caught.exception)))

    def test_submit_http_error_includes_status_without_exposing_token(self):
        requests = []
        token = "private-token-123"

        def handler(request):
            requests.append(request)
            return httpx.Response(503, text=f"Server echoed {token}")

        with ComfyUIClient(
            "http://comfy.invalid", api_key=token, transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(ComfyUIError) as caught:
                client.submit({"1": {"class_type": "TestNode", "inputs": {}}})
        self.assertEqual(len(requests), 1)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(token, str(caught.exception.payload))

    def test_injected_redirect_following_client_does_not_repeat_submission(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={"location": "http://other.invalid/prompt"})

        with httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as external:
            with (
                ComfyUIClient("http://comfy.invalid", client=external) as wrapper,
                self.assertRaises(ComfyUIError),
            ):
                wrapper.submit({"1": {"class_type": "TestNode", "inputs": {}}})
        self.assertEqual(len(requests), 1)

    def test_prompt_id_cannot_escape_history_path(self):
        with ComfyUIClient(
            "http://comfy.invalid",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        ) as client:
            for prompt_id in ("../bad", "..", "job?other=1", "job#fragment", "%2f", "job\nnext"):
                with self.subTest(prompt_id=prompt_id), self.assertRaises(ValueError):
                    client.history(prompt_id)

    def test_multipart_upload_streams_source_and_preserves_unicode_filename(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read()
            captured["type"] = request.headers["content-type"]
            return httpx.Response(
                200, json={"name": "已存音频.wav", "subfolder": "codex/audio", "type": "input"}
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            ComfyUIClient(
                "http://comfy.invalid",
                transport=httpx.MockTransport(handler),
            ) as client,
        ):
            source = Path(tmp) / "音频 name.wav"
            source.write_bytes(b"RIFF sample audio")
            self.assertEqual(
                client.upload_file(source, subfolder="codex/audio"), "codex/audio/已存音频.wav"
            )
            self.assertEqual(source.read_bytes(), b"RIFF sample audio")
        self.assertIn("multipart/form-data", captured["type"])
        self.assertIn("音频 name.wav".encode(), captured["body"])
        self.assertIn(b"RIFF sample audio", captured["body"])

    def test_upload_rejects_remote_traversal_and_url_delimiters(self):
        cases = [
            {"name": "../secret.png", "subfolder": "codex"},
            {"name": "name#fragment.png", "subfolder": "codex"},
            {"name": "name.png", "subfolder": "../outside"},
            {"name": "name.png", "subfolder": "C:\\outside"},
            {"name": "name.png", "subfolder": "codex?other"},
            {"name": "name.png", "subfolder": "/absolute"},
            {"name": "name.png", "subfolder": "codex", "type": "output"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.png"
            source.write_bytes(b"image")
            for response in cases:
                with (
                    self.subTest(response=response),
                    ComfyUIClient(
                        "http://comfy.invalid",
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json=response)
                        ),
                    ) as client,
                    self.assertRaises(ComfyUIError),
                ):
                    client.upload_file(source)

    def test_streaming_download_failure_preserves_previous_file_and_cleans_temporary(self):
        def handler(request):
            return httpx.Response(200, stream=_BrokenStream())

        history = {"outputs": {"43": {"videos": [{"filename": "clip.mp4", "type": "output"}]}}}
        with (
            tempfile.TemporaryDirectory() as tmp,
            ComfyUIClient(
                "http://comfy.invalid",
                transport=httpx.MockTransport(handler),
            ) as client,
        ):
            original = Path(tmp) / "43_clip.mp4"
            original.write_bytes(b"prior-complete-video")
            with self.assertRaises(ComfyUIError) as caught:
                client.download_outputs(history, tmp, prompt_id="job-1")
            self.assertEqual(caught.exception.prompt_id, "job-1")
            self.assertEqual(original.read_bytes(), b"prior-complete-video")
            self.assertEqual(list(Path(tmp).glob(".comfy-*.tmp")), [])

    def test_wait_bounds_history_request_timeout_by_remaining_time(self):
        seen_timeouts = []
        clock = [1_000_000.0]

        def advance(seconds):
            clock[0] += seconds

        def handler(request):
            seen_timeouts.append(request.extensions["timeout"])
            advance(0.01)
            return httpx.Response(200, json={})

        with (
            patch("comfyui_client.client.time.monotonic", side_effect=lambda: clock[0]),
            patch("comfyui_client.client.time.sleep", side_effect=advance),
            ComfyUIClient(
                "http://comfy.invalid",
                timeout=10,
                poll_interval=0.001,
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(WorkflowExecutionError) as caught,
        ):
            client.wait("job-1", timeout=0.04)
        self.assertEqual(caught.exception.prompt_id, "job-1")
        self.assertTrue(seen_timeouts)
        for timeouts in seen_timeouts:
            self.assertTrue(all(value <= 0.04 for value in timeouts.values()))

    def test_wait_polls_running_status_without_treating_it_as_success_or_failure(self):
        responses = [
            {"status": {"status_str": "running", "completed": False}},
            {"status": {"status_str": "success", "completed": False}},
            {"status": {"status_str": "success", "completed": True}, "outputs": {}},
        ]
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"job-1": responses.pop(0)})

        with ComfyUIClient(
            "http://comfy.invalid",
            poll_interval=0.001,
            transport=httpx.MockTransport(handler),
        ) as client:
            result = client.wait("job-1", timeout=1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["status"]["completed"])

    def test_connection_ownership_and_context_cleanup(self):
        external = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        )
        with ComfyUIClient("http://comfy.invalid", client=external) as wrapper:
            wrapper.health()
        self.assertFalse(external.is_closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            wrapper.health()
        external.close()
        with ComfyUIClient(
            "http://comfy.invalid",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        ) as owned:
            owned.health()
        self.assertTrue(owned._http.is_closed)


if __name__ == "__main__":
    unittest.main()
