import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, r"D:\work\articles\workflows")
from comfyui_client import balancer as m


class Tests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.root = Path(self.t.name)
        self.db = self.root / "jobs.sqlite"
        self.items = [
            SimpleNamespace(
                name=f"E01-{i:02}", prompt="test", seconds=1, aspect_ratio="16:9", pictures=()
            )
            for i in range(1, 9)
        ]
        m.STOP.clear()

    def tearDown(self):
        self.t.cleanup()

    def init(self):
        m.initialize(
            self.db,
            self.items,
            {
                "E01-01": {"status": "completed", "prompt_id": "p1"},
                "E01-02": {"status": "completed", "prompt_id": "p2"},
                "E01-03": {"status": "queued", "prompt_id": "p3"},
            },
            "s1",
        )

    def test_import_preserves_and_resume(self):
        self.init()
        r = m.claim(self.db, "s1")
        self.assertEqual((r["name"], r["prompt_id"]), ("E01-03", "p3"))
        self.assertEqual(m.claim(self.db, "s2")["name"], "E01-04")

    def test_parallel_claim_unique(self):
        m.initialize(self.db, self.items, {}, "s1")
        with ThreadPoolExecutor(8) as p:
            rs = list(p.map(lambda i: m.claim(self.db, "s" + str(i)), range(8)))
        self.assertEqual(len({x["name"] for x in rs}), 8)

    def test_unknown_blocks_server(self):
        self.init()
        m.update(self.db, "E01-03", "unknown")
        self.assertIsNone(m.claim(self.db, "s1"))
        self.assertEqual(m.claim(self.db, "s2")["name"], "E01-04")

    def test_mapping_change_rejected(self):
        self.init()
        with self.assertRaises(ValueError):
            m.initialize(self.db, self.items, {}, "different")

    def test_input_change_rejected(self):
        self.init()
        self.items[0].prompt = "changed"
        with self.assertRaises(ValueError):
            m.initialize(self.db, self.items, {}, "s1")

    def test_os_lock(self):
        with m.server_lock(self.root / "locks", "s1") as first:
            self.assertTrue(first)
            with m.server_lock(self.root / "locks", "s1") as second:
                self.assertFalse(second)

    def test_worker_resume_no_submit(self):
        self.init()
        m.STOP.set()
        m.STOP.clear()
        calls = []

        class Fake:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def wait(self, pid, timeout):
                calls.append(pid)
                m.STOP.set()
                return {}

            def download_outputs(self, *a, **kw):
                return [SimpleNamespace(path=Path("mock.mp4"))]

            def submit(self, *a):
                raise AssertionError("must not submit")

        m.worker(
            self.db,
            "s1",
            "unused",
            None,
            {i.name: i for i in self.items},
            self.root,
            self.root / "locks",
            5,
            Fake,
        )
        self.assertEqual(calls, ["p3"])
        with m.connect(self.db) as c:
            self.assertEqual(
                c.execute("SELECT state FROM jobs WHERE name='E01-03'").fetchone()[0], "completed"
            )

    def test_submit_failure_is_unknown_no_retry(self):
        from unittest.mock import patch

        m.initialize(self.db, self.items, {}, "s1")
        calls = []

        class Fake:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def submit(self, *a):
                calls.append(1)
                raise TimeoutError("secret must not leak")

        with patch("comfyui_client.bindings.build_graph", return_value={}):
            m.worker(
                self.db,
                "s2",
                "unused",
                None,
                {i.name: i for i in self.items},
                self.root,
                self.root / "locks",
                5,
                Fake,
            )
        self.assertEqual(len(calls), 1)
        self.assertIsNone(m.claim(self.db, "s2"))
        with m.connect(self.db) as c:
            self.assertEqual(
                c.execute("SELECT state FROM jobs WHERE name='E01-01'").fetchone()[0], "unknown"
            )

    def test_interrupted_restart_idempotent(self):
        self.init()
        legacy = {"E01-03": {"status": "queued", "prompt_id": "p3"}}
        m.initialize(self.db, self.items, legacy, "s1", [("E01-03", "p3")])
        row = m.claim(self.db, "s2")
        self.assertEqual(row["name"], "E01-03")
        m.update(self.db, "E01-03", "queued", prompt_id="new3")
        m.initialize(self.db, self.items, legacy, "s1", [("E01-03", "p3")])
        with m.connect(self.db) as c:
            self.assertEqual(
                c.execute("SELECT prompt_id FROM jobs WHERE name='E01-03'").fetchone()[0], "new3"
            )

    def test_completed_cannot_restart(self):
        self.init()
        with self.assertRaises(ValueError):
            m.initialize(self.db, self.items, {}, "s1", [("E01-01", "p1")])

    def test_dynamic_workers_six_tasks_four_servers(self):
        m.initialize(
            self.db,
            self.items,
            {
                "E01-01": {"status": "completed", "prompt_id": "p1"},
                "E01-02": {"status": "completed", "prompt_id": "p2"},
            },
            "s1",
        )
        counts = {}
        lock = threading.Lock()

        class Fake:
            def __init__(self, url, **kw):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def submit(self, g):
                with lock:
                    counts[self.url] = counts.get(self.url, 0) + 1
                    return self.url + "-" + str(counts[self.url])

            def wait(self, *a, **kw):
                time.sleep(0.01 if self.url == "s0" else 0.12)
                return {}

            def download_outputs(self, *a, **kw):
                return [SimpleNamespace(path=Path("mock.mp4"))]

        from unittest.mock import patch

        with patch("comfyui_client.bindings.build_graph", return_value={}):
            with ThreadPoolExecutor(4) as pool:
                fs = [
                    pool.submit(
                        m.worker,
                        self.db,
                        "s" + str(i),
                        "s" + str(i),
                        None,
                        {x.name: x for x in self.items},
                        self.root,
                        self.root / "locks",
                        5,
                        Fake,
                    )
                    for i in range(4)
                ]
                for f in fs:
                    f.result()
        self.assertEqual(sum(counts.values()), 6)
        self.assertGreaterEqual(counts["s0"], 2)

    def test_empty_download_not_completed(self):
        self.init()

        class Fake:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def wait(self, *a, **kw):
                return {}

            def download_outputs(self, *a, **kw):
                return []

        m.worker(
            self.db,
            "s1",
            "unused",
            None,
            {x.name: x for x in self.items},
            self.root,
            self.root / "locks",
            5,
            Fake,
        )
        with m.connect(self.db) as c:
            self.assertEqual(
                c.execute("SELECT state FROM jobs WHERE name='E01-03'").fetchone()[0], "queued"
            )


if __name__ == "__main__":
    unittest.main()
