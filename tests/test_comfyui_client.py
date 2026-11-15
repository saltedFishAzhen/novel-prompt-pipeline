"""Unit tests for the ComfyUI transport, graph binding and CLI layers."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from comfyui_client import (  # noqa: E402
    BindingError,
    ComfyUIClient,
    ComfyUIError,
    ComfyUITimeoutError,
    WorkflowExecutionError,
    api_graph,
    build_graph,
)
from comfyui_client.__main__ import build_parser  # noqa: E402
from comfyui_client.validation import (  # noqa: E402
    safe_filename,
    validate_prompt_id,
    validate_remote_path,
)

Handler = Callable[[httpx.Request], httpx.Response]

RUNNING = {"status": {"status_str": "running", "completed": False}}
SUCCESS = {"status": {"status_str": "success", "completed": True}, "outputs": {}}
PICTURES = ["p1.png", "p2.png"]


class TransportTestCase(unittest.TestCase):
    def make_client(self, routes: dict[tuple[str, str], Handler], **kwargs: Any) -> ComfyUIClient:
        def handle(request: httpx.Request) -> httpx.Response:
            handler = routes.get((request.method, request.url.path)) or routes.get(
                (request.method, "*")
            )
            if handler is None:
                return httpx.Response(404, text="not found")
            return handler(request)

        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        kwargs.setdefault("poll_interval", 0.01)
        return ComfyUIClient("https://comfy.test", client=http, **kwargs)

    # ---------------------------------------------------------------- submit

    def test_submit_returns_prompt_id(self) -> None:
        client = self.make_client(
            {("POST", "/prompt"): lambda request: httpx.Response(200, json={"prompt_id": "p-1"})}
        )
        graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}}}
        self.assertEqual(client.submit(graph), "p-1")

    def test_submit_requires_numeric_api_node_ids(self) -> None:
        client = self.make_client({})
        with self.assertRaises(ValueError):
            client.submit({"node_1": {"class_type": "X", "inputs": {}}})

    def test_submit_surfaces_http_node_errors(self) -> None:
        client = self.make_client(
            {
                ("POST", "/prompt"): lambda request: httpx.Response(
                    400, json={"node_errors": {"1": {"errors": [{"message": "bad"}]}}}
                )
            }
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.submit({"1": {"class_type": "X", "inputs": {}}})
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("node_errors", str(caught.exception.payload))

    def test_submit_rejects_node_errors_returned_with_200(self) -> None:
        client = self.make_client(
            {
                ("POST", "/prompt"): lambda request: httpx.Response(
                    200, json={"prompt_id": "p-1", "node_errors": {"1": {}}}
                )
            }
        )
        with self.assertRaises(ComfyUIError):
            client.submit({"1": {"class_type": "X", "inputs": {}}})

    def test_submit_rejects_unsafe_prompt_id(self) -> None:
        client = self.make_client(
            {("POST", "/prompt"): lambda request: httpx.Response(200, json={"prompt_id": "../x"})}
        )
        with self.assertRaises(ComfyUIError):
            client.submit({"1": {"class_type": "X", "inputs": {}}})

    def test_submit_is_never_retried(self) -> None:
        calls = {"count": 0}

        def fail(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(500, text="boom")

        client = self.make_client({("POST", "/prompt"): fail})
        with self.assertRaises(ComfyUIError):
            client.submit({"1": {"class_type": "X", "inputs": {}}})
        self.assertEqual(calls["count"], 1)

    # ------------------------------------------------------------------ wait

    def test_wait_polls_until_the_job_completes(self) -> None:
        polls = {"count": 0}

        def history(request: httpx.Request) -> httpx.Response:
            polls["count"] += 1
            entry = RUNNING if polls["count"] < 3 else SUCCESS
            return httpx.Response(200, json={"p-1": entry})

        client = self.make_client({("GET", "/history/p-1"): history})
        self.assertEqual(client.wait("p-1", timeout=5), SUCCESS)
        self.assertEqual(polls["count"], 3)

    def test_wait_raises_execution_error_when_the_server_reports_failure(self) -> None:
        failure = {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", {"exception_message": "OOM"}]],
            }
        }
        client = self.make_client(
            {("GET", "/history/p-1"): lambda request: httpx.Response(200, json={"p-1": failure})}
        )
        with self.assertRaises(WorkflowExecutionError) as caught:
            client.wait("p-1", timeout=5)
        self.assertIn("OOM", str(caught.exception.payload))

    def test_wait_defaults_to_job_timeout_not_the_request_timeout(self) -> None:
        client = self.make_client(
            {("GET", "/history/hang"): lambda request: httpx.Response(200, json={"hang": RUNNING})},
            timeout=0.01,
            job_timeout=0.05,
        )
        started = time.monotonic()
        with self.assertRaises(ComfyUITimeoutError) as caught:
            client.wait("hang")
        self.assertAlmostEqual(caught.exception.budget_seconds, 0.05)
        self.assertGreaterEqual(caught.exception.wait_seconds, 0.04)
        self.assertEqual(caught.exception.prompt_id, "hang")
        self.assertNotIsInstance(caught.exception, WorkflowExecutionError)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wait_accepts_an_explicit_budget(self) -> None:
        client = self.make_client(
            {("GET", "/history/hang"): lambda request: httpx.Response(200, json={"hang": RUNNING})},
            job_timeout=60,
        )
        with self.assertRaises(ComfyUITimeoutError) as caught:
            client.wait("hang", timeout=0.03)
        self.assertAlmostEqual(caught.exception.budget_seconds, 0.03)

    def test_wait_retries_transient_poll_failures(self) -> None:
        polls = {"count": 0}

        def history(request: httpx.Request) -> httpx.Response:
            polls["count"] += 1
            if polls["count"] < 3:
                return httpx.Response(503, text="warming up")
            return httpx.Response(200, json={"p-1": SUCCESS})

        client = self.make_client({("GET", "/history/p-1"): history})
        self.assertEqual(client.wait("p-1", timeout=5), SUCCESS)
        self.assertEqual(polls["count"], 3)

    def test_wait_fails_fast_on_client_errors(self) -> None:
        calls = {"count": 0}

        def history(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(401, text="denied")

        client = self.make_client({("GET", "/history/p-1"): history})
        with self.assertRaises(ComfyUIError) as caught:
            client.wait("p-1", timeout=5)
        self.assertNotIsInstance(caught.exception, ComfyUITimeoutError)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(calls["count"], 1)

    def test_wait_reports_the_last_poll_failure_on_timeout(self) -> None:
        client = self.make_client(
            {("GET", "/history/hang"): lambda request: httpx.Response(503, text="warming up")},
            job_timeout=0.05,
        )
        with self.assertRaises(ComfyUITimeoutError) as caught:
            client.wait("hang")
        self.assertIn("503", str(caught.exception))

    def test_history_returns_none_before_the_record_exists(self) -> None:
        client = self.make_client(
            {("GET", "/history/p-1"): lambda request: httpx.Response(200, json={})}
        )
        self.assertIsNone(client.history("p-1"))

    # -------------------------------------------------------------- failures

    def test_request_timeout_is_reported_separately_from_unreachable(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = self.make_client({("GET", "/queue"): slow})
        with self.assertRaises(ComfyUIError) as caught:
            client.queue()
        self.assertIn("timed out", str(caught.exception))

    def test_invalid_json_is_reported_with_a_bounded_payload(self) -> None:
        client = self.make_client(
            {("GET", "/queue"): lambda request: httpx.Response(200, text="<html>nope</html>")}
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.queue()
        self.assertIn("invalid JSON", str(caught.exception))

    def test_oversized_error_body_is_read_only_up_to_the_limit(self) -> None:
        client = self.make_client(
            {("GET", "/queue"): lambda request: httpx.Response(500, text="x" * 200_000)}
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.queue()
        self.assertLessEqual(len(caught.exception.payload), 4000)

    def test_error_body_is_redacted_and_stripped_of_control_characters(self) -> None:
        client = self.make_client(
            {
                ("GET", "/queue"): lambda request: httpx.Response(
                    500, text="token=secret-key\x1b[31m\x1b]0;owned\x07 end"
                )
            },
            api_key="secret-key",
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.queue()
        message = str(caught.exception)
        self.assertNotIn("secret-key", message)
        self.assertNotIn("\x1b", message)
        self.assertIn("[REDACTED]", message)

    def test_base_url_requires_a_host(self) -> None:
        for value in ("http://", "ftp://host", "", "comfy.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ComfyUIClient(value)

    def test_base_url_is_normalized(self) -> None:
        client = ComfyUIClient("  https://comfy.test/  ")
        self.addCleanup(client.close)
        self.assertEqual(client.base_url, "https://comfy.test")
        self.assertEqual(client._prepare("GET", "/queue").url, "https://comfy.test/queue")

    def test_base_url_rejects_a_query_or_fragment(self) -> None:
        for value in ("https://comfy.test?x=1", "https://comfy.test#frag"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ComfyUIClient(value)

    def test_padded_base_url_still_reaches_the_server(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/queue")
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})

        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        client = ComfyUIClient("  https://comfy.test/  ", client=http)
        self.assertEqual(client.queue(), {"queue_running": [], "queue_pending": []})

    def test_redirects_are_not_followed(self) -> None:
        client = self.make_client(
            {("GET", "/queue"): lambda request: httpx.Response(302, headers={"location": "/x"})}
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.queue()
        self.assertEqual(caught.exception.status, 302)

    # --------------------------------------------------------------- health

    def test_health_returns_system_stats(self) -> None:
        stats = {"system": {"os": "posix"}}
        client = self.make_client(
            {("GET", "/system_stats"): lambda request: httpx.Response(200, json=stats)}
        )
        self.assertEqual(client.health(), stats)

    def test_health_does_not_swallow_auth_errors(self) -> None:
        client = self.make_client(
            {("GET", "/system_stats"): lambda request: httpx.Response(401, text="denied")}
        )
        with self.assertRaises(ComfyUIError) as caught:
            client.health()
        self.assertEqual(caught.exception.status, 401)

    # --------------------------------------------------------------- cancel

    def test_cancel_interrupts_a_running_prompt(self) -> None:
        posts: list[tuple[str, Any]] = []
        queue = {"queue_running": [[0, "run-1", {}, {}, []]], "queue_pending": []}

        def record(request: httpx.Request) -> httpx.Response:
            posts.append((request.url.path, request.read()))
            return httpx.Response(200, json={})

        client = self.make_client(
            {
                ("GET", "/queue"): lambda request: httpx.Response(200, json=queue),
                ("POST", "*"): record,
            }
        )
        result = client.cancel("run-1")
        self.assertTrue(result["interrupted"])
        self.assertFalse(result["deleted_pending"])
        self.assertEqual([path for path, _ in posts], ["/interrupt"])

    def test_cancel_deletes_a_pending_prompt_without_interrupting(self) -> None:
        posts: list[tuple[str, Any]] = []
        queue = {"queue_running": [], "queue_pending": [[1, "wait-1", {}, {}, []]]}

        def record(request: httpx.Request) -> httpx.Response:
            posts.append((request.url.path, request.read()))
            return httpx.Response(200, json={})

        client = self.make_client(
            {
                ("GET", "/queue"): lambda request: httpx.Response(200, json=queue),
                ("POST", "*"): record,
            }
        )
        result = client.cancel("wait-1")
        self.assertFalse(result["interrupted"])
        self.assertTrue(result["deleted_pending"])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "/queue")
        self.assertEqual(posts[0][1], b'{"delete": ["wait-1"]}')

    def test_cancel_unknown_prompt_is_a_noop(self) -> None:
        client = self.make_client(
            {
                ("GET", "/queue"): lambda request: httpx.Response(
                    200, json={"queue_running": [], "queue_pending": []}
                )
            }
        )
        result = client.cancel("missing")
        self.assertEqual(
            result, {"prompt_id": "missing", "interrupted": False, "deleted_pending": False}
        )

    # --------------------------------------------------------------- upload

    def _png(self, directory: str) -> Path:
        path = Path(directory) / "pic.png"
        path.write_bytes(b"\x89PNG\r\n")
        return path

    def test_upload_returns_the_input_relative_name(self) -> None:
        def upload(request: httpx.Request) -> httpx.Response:
            body = request.read()
            self.assertIn(b'name="subfolder"', body)
            self.assertIn(b'name="overwrite"', body)
            self.assertIn(b"\r\n\r\nfalse\r\n", body)
            return httpx.Response(
                200, json={"name": "pic.png", "subfolder": "codex", "type": "input"}
            )

        client = self.make_client({("POST", "/upload/image"): upload})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                client.upload_file(self._png(directory), subfolder="codex"), "codex/pic.png"
            )

    def test_upload_can_request_overwrite(self) -> None:
        def upload(request: httpx.Request) -> httpx.Response:
            self.assertIn(b"\r\n\r\ntrue\r\n", request.read())
            return httpx.Response(200, json={"name": "pic.png", "type": "input"})

        client = self.make_client({("POST", "/upload/image"): upload})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(client.upload_file(self._png(directory), overwrite=True), "pic.png")

    def test_upload_rejects_an_unsafe_subfolder_before_contacting_the_server(self) -> None:
        client = self.make_client({})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                client.upload_file(self._png(directory), subfolder="../elsewhere")

    def test_upload_rejects_a_nested_name_from_the_server(self) -> None:
        client = self.make_client(
            {
                ("POST", "/upload/image"): lambda request: httpx.Response(
                    200, json={"name": "a/b.png", "type": "input"}
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError):
                client.upload_file(self._png(directory))

    # ------------------------------------------------------------- download

    def test_download_records_node_identity_and_bytes(self) -> None:
        def view(request: httpx.Request) -> httpx.Response:
            name = request.url.params.get("filename", "")
            return httpx.Response(200, content=f"bytes:{name}".encode())

        client = self.make_client({("GET", "/view"): view})
        history = {
            "outputs": {
                "7": {"videos": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]}
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            outputs = client.download_outputs(history, directory, prompt_id="p-1")
            self.assertEqual(len(outputs), 1)
            output = outputs[0]
            self.assertEqual(output.node_id, "7")
            self.assertEqual(output.kind, "videos")
            self.assertEqual(output.storage_type, "output")
            self.assertEqual(output.relative_name, "clip.mp4")
            self.assertEqual(output.path.read_bytes(), b"bytes:clip.mp4")
            self.assertEqual(output.to_dict()["node_id"], "7")

    def test_output_file_reports_its_downloaded_path(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            output = client.download_outputs(history, directory)[0]
            self.assertTrue(output.path.is_file())
            self.assertEqual(str(output), output.to_dict()["path"])

    def test_download_deduplicates_identical_references(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        item = {"filename": "same.png", "subfolder": "", "type": "output"}
        history = {"outputs": {"1": {"images": [item, dict(item)]}}}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(len(client.download_outputs(history, directory)), 1)

    def test_download_gives_same_named_outputs_distinct_paths(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        history = {
            "outputs": {
                "1": {
                    "images": [
                        {"filename": "same.png", "subfolder": "a", "type": "output"},
                        {"filename": "same.png", "subfolder": "b", "type": "output"},
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            outputs = client.download_outputs(history, directory)
            self.assertEqual(len({output.path for output in outputs}), 2)

    def test_download_sanitizes_server_supplied_names(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        history = {
            "outputs": {
                "../../escape": {
                    "images": [{"filename": "../../evil.png", "subfolder": "", "type": "output"}]
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            outputs = client.download_outputs(history, directory)
            root = Path(directory).resolve()
            self.assertTrue(outputs[0].path.resolve().is_relative_to(root))

    def test_download_failure_removes_the_temporary_file(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(500, text="nope")}
        )
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError):
                client.download_outputs(history, directory)
            leftovers = [
                path for path in Path(directory).iterdir() if path.name.startswith(".comfy-")
            ]
            self.assertEqual(leftovers, [])

    def test_download_retries_transient_failures(self) -> None:
        calls = {"count": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] < 3:
                return httpx.Response(503, text="warming up")
            return httpx.Response(200, content=b"payload")

        client = self.make_client({("GET", "/view"): flaky})
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            outputs = client.download_outputs(history, directory)
            self.assertEqual(outputs[0].path.read_bytes(), b"payload")
        self.assertEqual(calls["count"], 3)

    def test_download_does_not_retry_client_errors(self) -> None:
        calls = {"count": 0}

        def denied(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(404, text="gone")

        client = self.make_client({("GET", "/view"): denied})
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError):
                client.download_outputs(history, directory)
        self.assertEqual(calls["count"], 1)

    def test_download_rejects_an_empty_output_without_retrying(self) -> None:
        calls = {"count": 0}

        def empty(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, content=b"")

        client = self.make_client({("GET", "/view"): empty})
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError) as caught:
                client.download_outputs(history, directory)
            self.assertIn("empty", str(caught.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(calls["count"], 1)

    def test_download_enforces_the_size_limit(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"0123456789")},
            max_output_bytes=8,
        )
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError) as caught:
                client.download_outputs(history, directory)
            self.assertIn("exceeds", str(caught.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_download_shortens_an_overlong_server_filename(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        name = "A" * 400 + ".png"
        history = {"outputs": {"1": {"videos": [{"filename": name, "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            output = client.download_outputs(history, directory)[0]
            self.assertTrue(output.path.is_file())
            self.assertLessEqual(len(output.path.name), 130)
            self.assertEqual(output.path.suffix, ".png")
            self.assertEqual(output.filename, name)

    def test_download_never_overwrites_an_existing_file(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"fresh")}
        )
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            first = client.download_outputs(history, directory)[0]
            first.path.write_bytes(b"keep")
            second = client.download_outputs(history, directory)[0]
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(first.path.read_bytes(), b"keep")
            self.assertEqual(second.path.read_bytes(), b"fresh")

    def test_download_reports_history_entries_without_a_filename(self) -> None:
        client = self.make_client({})
        history = {"outputs": {"1": {"images": [{"subfolder": ""}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError) as caught:
                client.download_outputs(history, directory)
            self.assertIn("unusable", str(caught.exception.payload))

    def test_download_rejects_an_unknown_storage_type(self) -> None:
        client = self.make_client({})
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "secrets"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComfyUIError):
                client.download_outputs(history, directory)

    def test_download_sanitizes_the_relative_name(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        history = {
            "outputs": {
                "1": {
                    "images": [
                        {"filename": "../../evil.png", "subfolder": "../../etc", "type": "output"}
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            output = client.download_outputs(history, directory)[0]
            self.assertEqual(output.relative_name, "etc/evil.png")
            self.assertTrue(output.path.resolve().is_relative_to(Path(directory).resolve()))

    def test_download_writes_into_a_prompt_subdirectory(self) -> None:
        client = self.make_client(
            {("GET", "/view"): lambda request: httpx.Response(200, content=b"x")}
        )
        history = {"outputs": {"1": {"images": [{"filename": "a.png", "type": "output"}]}}}
        with tempfile.TemporaryDirectory() as directory:
            output = client.download_outputs(history, directory, prompt_id="p-1", subdir="p-1")[0]
            self.assertEqual(output.path.parent, Path(directory).resolve() / "p-1")

    def test_download_rejects_a_traversing_subdirectory(self) -> None:
        client = self.make_client({})
        history = {"outputs": {}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                client.download_outputs(history, directory, subdir="../elsewhere")


class BindingTestCase(unittest.TestCase):
    def test_validate_remote_path_rejects_unsafe_names(self) -> None:
        for value in (
            "a?b.png",
            "a#b.png",
            "a\x01b.png",
            "a//b.png",
            "../a.png",
            "C:/a.png",
            "/absolute.png",
            " a.png",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_remote_path(value, "name")

    def test_validate_remote_path_accepts_a_nested_relative_name(self) -> None:
        self.assertEqual(validate_remote_path("codex\\a.png", "name"), "codex/a.png")

    def test_validate_prompt_id_rejects_traversal(self) -> None:
        for value in ("..", "a/b", "a%2fb", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_prompt_id(value)

    def test_binding_rejects_a_traversing_reference_image(self) -> None:
        with self.assertRaises(BindingError):
            build_graph({"prompt": "p", "reference_images": ["../b.png"]})

    def test_build_graph_writes_the_prompt_duration_and_seed(self) -> None:
        graph = build_graph(
            {"prompt": "p", "length": 9.5, "seed": 7, "reference_images": PICTURES}
        )
        self.assertEqual(graph["28"]["inputs"]["prompt"], "p")
        self.assertEqual(graph["27"]["inputs"]["value"], 9.5)
        self.assertEqual(graph["11"]["inputs"]["noise_seed"], 7)

    def test_build_graph_keeps_the_workflow_defaults_it_is_given(self) -> None:
        graph = build_graph({"prompt": "p", "reference_images": PICTURES})
        self.assertEqual(graph["13"]["inputs"]["size_mode"], "scale_by")
        self.assertEqual(graph["11"]["inputs"]["noise_seed"], 683072603085674)
        self.assertEqual(graph["29"]["inputs"]["aspect_ratio"], "9:16 (Portrait Widescreen)")

    def test_build_graph_maps_the_aspect_ratio_label(self) -> None:
        graph = build_graph(
            {"prompt": "p", "aspect_ratio": "16:9", "reference_images": PICTURES}
        )
        self.assertEqual(graph["29"]["inputs"]["aspect_ratio"], "16:9 (Widescreen)")

    def test_unsupported_aspect_ratio_is_rejected(self) -> None:
        with self.assertRaises(BindingError) as caught:
            build_graph({"prompt": "p", "aspect_ratio": "5:4", "reference_images": PICTURES})
        self.assertIn("supported", str(caught.exception))

    def test_build_graph_binds_nine_reference_slots(self) -> None:
        names = [f"r{index}.png" for index in range(1, 10)]
        graph = build_graph({"prompt": "p", "reference_images": names})
        self.assertEqual(graph["66"]["inputs"]["image"], "r1.png")
        self.assertEqual(graph["908"]["inputs"]["image"], "r9.png")
        self.assertEqual(graph["95"]["inputs"]["ref_images.ref_image_0"], ["66", 0])
        self.assertEqual(graph["95"]["inputs"]["ref_images.ref_image_8"], ["908", 0])

    def test_more_pictures_than_slots_is_rejected(self) -> None:
        names = [f"r{index}.png" for index in range(1, 11)]
        with self.assertRaises(BindingError) as caught:
            build_graph({"prompt": "p", "reference_images": names})
        self.assertIn("9 reference image slots", str(caught.exception))

    def test_unused_picture_slots_and_audio_placeholders_are_removed(self) -> None:
        graph = build_graph({"prompt": "p", "reference_images": PICTURES})
        for node_id in ("902", "903", "904", "905", "906", "907", "908", "107", "108", "109"):
            self.assertNotIn(node_id, graph)
        for input_name in (
            "ref_images.ref_image_2",
            "ref_images.ref_image_8",
            "ref_audios.ref_audio_0",
            "ref_audios.ref_audio_2",
        ):
            self.assertNotIn(input_name, graph["95"]["inputs"])
            self.assertNotIn(input_name, graph["96"]["inputs"])

    def test_missing_reference_images_are_rejected(self) -> None:
        with self.assertRaises(BindingError) as caught:
            build_graph({"prompt": "p"})
        self.assertIn("at least one reference image", str(caught.exception))

    def test_missing_prompt_is_rejected(self) -> None:
        with self.assertRaises(BindingError) as caught:
            build_graph({"reference_images": PICTURES})
        self.assertIn("prompt", str(caught.exception))

    def test_unsupported_value_is_rejected(self) -> None:
        with self.assertRaises(BindingError):
            build_graph({"prompt": "p", "reference_images": PICTURES, "cfg": 7})

    def test_length_accepts_fractional_seconds_but_not_zero(self) -> None:
        graph = build_graph({"prompt": "p", "length": 2.5, "reference_images": PICTURES})
        self.assertEqual(graph["27"]["inputs"]["value"], 2.5)
        for value in ("5", 0, -1):
            with self.subTest(value=value), self.assertRaises(BindingError):
                build_graph({"prompt": "p", "length": value, "reference_images": PICTURES})

    def test_api_graph_returns_a_detached_copy(self) -> None:
        graph = {"1": {"class_type": "X", "inputs": {"image": "a.png"}}}
        clean = api_graph(graph)
        clean["1"]["inputs"]["image"] = "b.png"
        self.assertEqual(graph["1"]["inputs"]["image"], "a.png")

    def test_api_graph_skips_subgraph_definitions(self) -> None:
        graph = {
            "1": {"class_type": "X", "inputs": {}},
            "definitions": {"subgraphs": [{"id": "s"}]},
            "_meta": {"title": "ui only"},
        }
        self.assertEqual(set(api_graph(graph)), {"1"})

    def test_binding_file_with_an_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text(json.dumps({"schema_version": 1, "profiles": {}}), encoding="utf-8")
            with self.assertRaises(BindingError) as caught:
                build_graph({"prompt": "p"}, bindings_file=path)
        self.assertIn("unsupported keys", str(caught.exception))

    def test_binding_file_requires_schema_version_1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            with self.assertRaises(BindingError) as caught:
                build_graph({"prompt": "p"}, bindings_file=path)
        self.assertIn("schema_version", str(caught.exception))

    def test_removing_a_referenced_node_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wf.json").write_text(
                json.dumps(
                    {
                        "1": {"class_type": "LoadImage", "inputs": {"image": ""}},
                        "2": {"class_type": "Node", "inputs": {"image": ["1", 0]}},
                        "3": {"class_type": "Node", "inputs": {"prompt": "", "ratio": ""}},
                        "4": {"class_type": "LoadImage", "inputs": {"image": ""}},
                    }
                ),
                encoding="utf-8",
            )
            (root / "bindings.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workflow": "wf.json",
                        "bindings": {"prompt": [{"node_id": "3", "input": "prompt"}]},
                        "reference_slots": [
                            {"load": {"node_id": "4", "input": "image"}, "conditioning": []}
                        ],
                        "prune": [{"node_id": "1"}],
                        "aspect_ratio_input": {"node_id": "3", "input": "ratio"},
                        "aspect_ratios": {"1:1": "1:1 (Square)"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BindingError) as caught:
                build_graph(
                    {"prompt": "p", "aspect_ratio": "1:1", "reference_images": ["a.png"]},
                    bindings_file=root / "bindings.json",
                )
        self.assertIn("still referenced", str(caught.exception))

    def test_safe_filename_is_length_bounded(self) -> None:
        name = safe_filename("A" * 400 + ".png")
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(safe_filename("CON.png"), "CON.png")


class CommandLineTestCase(unittest.TestCase):
    def test_storyboard_is_the_only_generation_command(self) -> None:
        args = build_parser().parse_args(
            ["storyboard", "--manifest", "m.json", "--dry-run", "--submit-only"]
        )
        self.assertEqual((args.command, args.manifest), ("storyboard", "m.json"))
        self.assertTrue(args.dry_run)
        self.assertTrue(args.submit_only)
        for removed in ("run", "submit", "upload"):
            with self.subTest(command=removed), self.assertRaises(SystemExit):
                build_parser().parse_args([removed])

    def test_job_timeouts_are_separate_from_the_request_timeout(self) -> None:
        args = build_parser().parse_args(["storyboard", "--manifest", "m.json"])
        self.assertEqual(args.request_timeout, 30.0)
        self.assertEqual(args.job_timeout, 3600.0)

    def test_connection_options_work_after_the_subcommand(self) -> None:
        args = build_parser().parse_args(
            ["storyboard", "--manifest", "m.json", "--base-url", "http://x:8188"]
        )
        self.assertEqual(args.base_url, "http://x:8188")

    def test_connection_options_work_before_the_subcommand(self) -> None:
        args = build_parser().parse_args(
            ["--base-url", "http://x:8188", "storyboard", "--manifest", "m.json"]
        )
        self.assertEqual(args.base_url, "http://x:8188")

    def test_connection_defaults_survive_an_omitting_subcommand(self) -> None:
        args = build_parser().parse_args(["storyboard", "--manifest", "m.json"])
        self.assertIsNone(args.base_url)
        self.assertEqual(args.poll_interval, 1.0)

    def test_cancel_takes_a_prompt_id(self) -> None:
        args = build_parser().parse_args(["cancel", "p-1"])
        self.assertEqual((args.command, args.prompt_id), ("cancel", "p-1"))

    def test_wait_defaults_to_the_storyboard_output_dir(self) -> None:
        args = build_parser().parse_args(["wait", "p-1"])
        self.assertEqual((args.output_dir, args.job_timeout), ("runs/storyboard", 3600.0))


if __name__ == "__main__":
    unittest.main()
