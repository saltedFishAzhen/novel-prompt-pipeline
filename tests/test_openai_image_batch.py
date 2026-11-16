"""Offline tests only: no API credentials or real requests."""

from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from openai_images.batch import Batch, file_lock, read_jobs  # noqa: E402
from openai_images.errors import ImageError, RequestFailure, request_failure  # noqa: E402


def hold_lock(path, ready, finish):
    with file_lock(Path(path)):
        ready.set()
        finish.wait(10)


class BatchTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture.png"
        Image.new("RGB", (16, 16), "blue").save(self.fixture)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("offline prompt", encoding="utf-8")
        self.manifest = self.root / "jobs.json"
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def write_jobs(self, count=1):
        jobs = [
            {"id": f"j{i}", "promptFile": "prompt.txt", "outputFile": f"out{i}.png"}
            for i in range(count)
        ]
        self.write(jobs)
        return jobs

    def write(self, jobs):
        self.manifest.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")

    def factory(self, **kwargs):
        outer = self

        class Client:
            def run(self, request, output_dir, execute=False):
                with outer.lock:
                    outer.calls += 1
                    outer.active += 1
                    outer.peak = max(outer.active, outer.peak)
                time.sleep(0.01)
                output_dir.mkdir(parents=True, exist_ok=True)
                image = output_dir / "response.png"
                image.write_bytes(outer.fixture.read_bytes())
                with outer.lock:
                    outer.active -= 1
                return {"run_dir": str(output_dir), "outputs": [{"file": image.name}]}

        return Client()

    def runner(self, **kwargs):
        return Batch(self.manifest, client_factory=self.factory, **kwargs)

    def test_dry_run_no_files_no_calls(self):
        self.write_jobs()
        result = self.runner().run()
        self.assertFalse(result["network_requested"])
        self.assertFalse((self.root / "jobs.state").exists())
        self.assertEqual(self.calls, 0)

    def test_parallel_resume_and_atomic_state(self):
        self.write_jobs(70)
        runner = self.runner()
        self.assertEqual(runner.run(execute=True)["status"], "completed")
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, 50)
        self.assertEqual(len(json.loads(runner.state_path.read_text())["jobs"]), 70)
        self.assertEqual(self.runner().run(execute=True)["status"], "completed")
        self.assertEqual(self.calls, 70)

    def test_dependency_and_output_adoption(self):
        jobs = self.write_jobs(2)
        jobs[1]["references"] = ["out0.png"]
        self.write(jobs)
        (self.root / "out0.png").write_bytes(self.fixture.read_bytes())
        self.assertEqual(self.runner().run(execute=True)["status"], "completed")
        self.assertEqual(self.calls, 1)

    def test_crash_inflight_not_retried(self):
        self.write_jobs()
        runner = self.runner()
        runner.update("j0", status="in_flight", attempts=1)
        self.assertEqual(self.runner().run(execute=True)["counts"], {"needs_attention": 1})
        self.assertEqual(self.calls, 0)

    def test_429_bounded_and_resume_no_extra_calls(self):
        self.write_jobs()

        class Client:
            calls = 0

            def run(self, *args, **kwargs):
                self.calls += 1
                raise RequestFailure(
                    "rate limit", retryable=True, retry_after=0, category="rate_limited"
                )

        client = Client()
        runner = Batch(self.manifest, client_factory=lambda **kw: client)
        runner.run(execute=True)
        self.assertEqual(client.calls, 3)
        Batch(self.manifest, client_factory=lambda **kw: client).run(execute=True)
        self.assertEqual(client.calls, 3)

    def test_rate_limit_then_success(self):
        self.write_jobs()
        fake = self.factory()
        calls = []

        class Client:
            def run(inner, *args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise RequestFailure(
                        "rate limit", retryable=True, retry_after=0, category="rate_limited"
                    )
                return fake.run(*args, **kwargs)

        result = Batch(self.manifest, client_factory=lambda **kw: Client()).run(execute=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 2)

    def test_unknown_failure_no_retry_and_blocks_dependency(self):
        jobs = self.write_jobs(2)
        jobs[1]["dependsOn"] = ["j0"]
        self.write(jobs)
        calls = []

        class Client:
            def run(self, *args, **kwargs):
                calls.append(1)
                raise RequestFailure("unknown")

        for _ in range(2):
            result = Batch(self.manifest, client_factory=lambda **kw: Client()).run(execute=True)
            self.assertEqual(result["counts"], {"needs_attention": 1, "blocked": 1})
        self.assertEqual(len(calls), 1)

    def test_corrupt_output_not_overwritten(self):
        self.write_jobs()
        output = self.root / "out0.png"
        output.write_bytes(b"keep me")
        self.runner().run(execute=True)
        self.assertEqual(output.read_bytes(), b"keep me")
        self.assertEqual(self.calls, 0)

    def test_changed_prompt_not_regenerated(self):
        self.write_jobs()
        self.runner().run(execute=True)
        self.prompt.write_text("changed", encoding="utf-8")
        self.assertEqual(self.runner().run(execute=True)["counts"], {"needs_attention": 1})
        self.assertEqual(self.calls, 1)

    def test_cross_process_lock_and_recovery(self):
        lock = self.root / "process.lock"
        context = multiprocessing.get_context("spawn")
        ready, finish = context.Event(), context.Event()
        process = context.Process(target=hold_lock, args=(str(lock), ready, finish))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(ImageError), file_lock(lock):
                pass
        finally:
            finish.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        with file_lock(lock):
            pass

    def test_cycles_and_duplicate_outputs_rejected(self):
        jobs = self.write_jobs(2)
        jobs[0]["dependsOn"] = ["j1"]
        jobs[1]["dependsOn"] = ["j0"]
        self.write(jobs)
        with self.assertRaises(ImageError):
            read_jobs(self.manifest)
        jobs[0]["dependsOn"] = []
        jobs[1]["outputFile"] = jobs[0]["outputFile"]
        self.write(jobs)
        with self.assertRaises(ImageError):
            read_jobs(self.manifest)

    def test_api_classifier_redacts_and_stops_unknowns(self):
        import httpx
        import openai

        request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
        for status, code, retry in [
            (429, "rate_limit_exceeded", True),
            (429, "insufficient_quota", False),
            (429, None, False),
            (500, None, False),
            (401, None, False),
        ]:
            response = httpx.Response(status, request=request, headers={"retry-after": "7"})
            error = openai.APIStatusError(
                "fake-secret private-prompt", response=response, body={"code": code}
            )
            failure = request_failure(error)
            self.assertEqual(failure.retryable, retry)
            self.assertNotIn("fake-secret", str(failure))
            if retry:
                self.assertEqual(failure.retry_after, 7)
        self.assertFalse(request_failure(openai.APITimeoutError(request=request)).retryable)
        self.assertFalse(request_failure(openai.APIConnectionError(request=request)).retryable)

    def test_client_preserves_structured_error(self):
        from types import SimpleNamespace

        import httpx
        import openai

        from openai_images.client import ImageRequest, OpenAIImagesClient

        request = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
        error = openai.RateLimitError(
            "secret",
            response=httpx.Response(429, request=request),
            body={"code": "rate_limit_exceeded"},
        )

        def fail(**kwargs):
            raise error

        client = SimpleNamespace(images=SimpleNamespace(generate=fail), close=lambda: None)
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key"}),
            patch("openai_images.client._create_sdk", return_value=client),
        ):
            with self.assertRaises(RequestFailure) as caught:
                OpenAIImagesClient().run(
                    ImageRequest(prompt="test"), self.root / "runs", execute=True
                )
        self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
