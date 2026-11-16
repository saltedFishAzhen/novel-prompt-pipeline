import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, r"D:\work\articles\workflows")
from comfyui_client import balancer as m


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.root = Path(self.t.name)
        self.db = self.root / "x.db"
        m.STOP.clear()
        self.items = {
            f"E01-{i:02}": SimpleNamespace(
                name=f"E01-{i:02}", prompt="x", seconds=1, aspect_ratio="16:9", pictures=()
            )
            for i in range(1, 9)
        }
        m.initialize(self.db, list(self.items.values()), {}, "legacy")
        self.calls = []
        self.lock = threading.Lock()

    def tearDown(self):
        m.STOP.set()
        self.t.cleanup()

    def factory(self, url, **kw):
        outer = self

        class Fake:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def upload_file(self, *a, **kw):
                return "mock.png"

            def submit(self, g):
                with outer.lock:
                    outer.calls.append(url)
                    return "p" + str(len(outer.calls))

            def wait(self, *a, **kw):
                time.sleep(0.04)
                return {}

            def download_outputs(self, *a, **kw):
                return [SimpleNamespace(path=Path("mock.mp4"))]

        return Fake()

    def run_dynamic(self, loader):
        with patch("comfyui_client.bindings.build_graph", return_value={}):
            m.dynamic_dispatch(
                self.db,
                self.root,
                self.items,
                self.root,
                self.root / "locks",
                1,
                self.factory,
                0.005,
                loader,
            )

    def states(self):
        with m.connect(self.db) as c:
            return [r[0] for r in c.execute("select state from jobs")]

    def test_add_and_remove_drains_without_resubmit(self):
        seen = [0]

        def load(_):
            seen[0] += 1
            return (["http://a"] if not self.calls else ["http://b", "http://c"]), None

        self.run_dynamic(load)
        self.assertEqual(self.calls.count("http://a"), 1)
        self.assertEqual(len(self.calls), 8)
        self.assertIn("http://b", self.calls)
        self.assertIn("http://c", self.calls)
        self.assertEqual(set(self.states()), {"completed"})

    def test_empty_then_invalid_then_valid(self):
        n = [0]

        def load(_):
            n[0] += 1
            if n[0] == 1:
                return [], None
            if n[0] == 2:
                raise ValueError("secret")
            return ["http://b"], None

        self.run_dynamic(load)
        self.assertEqual(len(self.calls), 8)

    def test_removed_during_upload_releases_before_submit(self):
        ctrl = m.DispatchControl()
        ctrl.active = {"a"}
        item = self.items["E01-01"]
        item.pictures = (Path("unused"),)
        # initialization predates fake picture; worker fingerprint is not used here.
        outer = self

        def factory(*a, **kw):
            f = outer.factory("http://a")
            original = f.upload_file

            def upload(*a, **kw):
                with ctrl.lock:
                    ctrl.active.clear()
                return original(*a, **kw)

            f.upload_file = upload
            return f

        with patch("comfyui_client.bindings.build_graph", return_value={}):
            m.worker(
                self.db,
                "a",
                "http://a",
                None,
                self.items,
                self.root,
                self.root / "locks",
                1,
                factory,
                ctrl,
            )
        self.assertEqual(self.calls, [])
        self.assertEqual(set(self.states()), {"pending"})

    def test_unknown_is_not_rescheduled(self):
        sid = m.hashlib.sha256(b"http://a").hexdigest()
        row = m.claim(self.db, sid)
        m.update(self.db, row["name"], "unknown")
        self.run_dynamic(lambda _: (["http://a", "http://b"], None))
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.states().count("unknown"), 1)

    def test_wait_failure_not_retried_by_poll_loop(self):
        orig = self.factory

        def factory(*a, **kw):
            f = orig(*a, **kw)

            def wait(*a, **kw):
                raise TimeoutError("secret")

            f.wait = wait
            return f

        self.factory = factory
        self.run_dynamic(lambda _: (["http://a"], None))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.states().count("queued"), 1)

    def test_settings_empty_and_invalid(self):
        (self.root / ".env").write_text("COMFYUI_URLS=[]\nCOMFYUI_API_KEY=secret")
        self.assertEqual(m.load_settings(self.root), ([], "secret"))
        (self.root / ".env").write_text('COMFYUI_URLS=["http://a", "http://a/"]')
        with self.assertRaises(ValueError):
            m.load_settings(self.root)

    def test_shutdown_does_not_cancel_existing(self):
        n = [0]

        def load(_):
            n[0] += 1
            if self.calls:
                m.STOP.set()
            return ["http://a"], None

        self.run_dynamic(load)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.states().count("completed"), 1)


if __name__ == "__main__":
    unittest.main()
