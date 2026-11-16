"""Conservative multi-server dispatcher. Dry-run is default; no health probes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

STOP = threading.Event()


def fingerprint(item):
    h = hashlib.sha256(
        json.dumps([item.name, item.prompt, item.seconds, item.aspect_ratio]).encode()
    )
    for p in item.pictures:
        h.update(p.read_bytes())
    return h.hexdigest()


@contextlib.contextmanager
def connect(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    try:
        with c:
            yield c
    finally:
        c.close()


def initialize(db, items, legacy, legacy_server, restarts=()):
    with connect(db) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, server TEXT, prompt_id TEXT, error TEXT)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS restarts(name TEXT, old_prompt TEXT, old_record TEXT, PRIMARY KEY(name,old_prompt))"
        )
        c.execute("BEGIN IMMEDIATE")
        previous = c.execute("SELECT value FROM metadata WHERE key='legacy_server'").fetchone()
        if previous and previous[0] != legacy_server:
            raise ValueError("Legacy server mapping changed")
        c.execute("INSERT OR IGNORE INTO metadata VALUES('legacy_server',?)", (legacy_server,))
        for item in items:
            digest = fingerprint(item)
            old = c.execute("SELECT * FROM jobs WHERE name=?", (item.name,)).fetchone()
            if old and old["digest"] != digest:
                raise ValueError("Input changed for existing job " + item.name)
            c.execute(
                "INSERT OR IGNORE INTO jobs(name,digest,state) VALUES(?,?,'pending')",
                (item.name, digest),
            )
        for name, rec in legacy.items():
            old = c.execute("SELECT * FROM jobs WHERE name=?", (name,)).fetchone()
            if not old:
                continue
            if c.execute("SELECT 1 FROM restarts WHERE name=?", (name,)).fetchone():
                continue
            pid = rec.get("prompt_id")
            if old["prompt_id"] and old["prompt_id"] != pid:
                raise ValueError("Conflicting submission records for " + name)
            if old["state"] == "pending":
                state = (
                    "completed"
                    if rec.get("status") == "completed"
                    else ("queued" if pid else "unknown")
                )
                c.execute(
                    "UPDATE jobs SET state=?,server=?,prompt_id=? WHERE name=?",
                    (state, legacy_server, pid, name),
                )
        for name, pid in restarts:
            if c.execute(
                "SELECT 1 FROM restarts WHERE name=? AND old_prompt=?", (name, pid)
            ).fetchone():
                continue
            old = c.execute("SELECT * FROM jobs WHERE name=?", (name,)).fetchone()
            if not old or old["prompt_id"] != pid or old["state"] == "completed":
                raise ValueError("Interrupted restart does not match unfinished attempt")
            c.execute("INSERT INTO restarts VALUES(?,?,?)", (name, pid, json.dumps(dict(old))))
            c.execute(
                "UPDATE jobs SET state='pending',server=NULL,prompt_id=NULL,error=NULL WHERE name=?",
                (name,),
            )


def records(output):
    found = {}
    for p in output.glob("comfy-*.json"):
        r = json.loads(p.read_text(encoding="utf-8-sig"))
        name = r.get("segment")
        if not name:
            continue
        if name in found and found[name].get("prompt_id") != r.get("prompt_id"):
            raise ValueError("Multiple existing submissions: " + name)
        found[name] = r
    return found


def claim(db, server, allow_pending=True):
    with connect(db) as c:
        c.execute("BEGIN IMMEDIATE")
        # Resume known server jobs without submission. Unknown blocks this server.
        row = c.execute(
            "SELECT * FROM jobs WHERE server=? AND state NOT IN ('completed','pending') ORDER BY name LIMIT 1",
            (server,),
        ).fetchone()
        if row:
            return dict(row) if row["state"] == "queued" and row["prompt_id"] else None
        if not allow_pending:
            return None
        row = c.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY name LIMIT 1").fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE jobs SET state='claimed',server=? WHERE name=? AND state='pending'",
            (server, row["name"]),
        )
        result = dict(row)
        result.update(state="claimed", server=server)
        return result


def update(db, name, state, **fields):
    with connect(db) as c:
        pairs = {"state": state, **fields}
        c.execute(
            "UPDATE jobs SET " + ",".join(k + "=?" for k in pairs) + " WHERE name=?",
            (*pairs.values(), name),
        )


@contextlib.contextmanager
def server_lock(folder, server):
    folder.mkdir(parents=True, exist_ok=True)
    f = (folder / (server + ".lock")).open("a+b")
    f.seek(0, 2)
    if f.tell() == 0:
        try:
            f.write(b"0")
            f.flush()
        except OSError:
            pass
    f.seek(0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def worker(db, server, url, key, items, output, locks, timeout, factory, control=None):
    with server_lock(locks, server) as acquired:
        if not acquired:
            return "busy"
        with factory(url, api_key=key, download_retries=0) as client:
            while not STOP.is_set():
                if control is None:
                    row = claim(db, server)
                else:
                    with control.lock:
                        row = claim(db, server, control.enabled(server))
                if not row:
                    with connect(db) as c:
                        blocked = c.execute(
                            "SELECT 1 FROM jobs WHERE server=? AND state NOT IN ('completed','pending','queued')",
                            (server,),
                        ).fetchone()
                    return "stopped" if blocked else "drained"
                name = row["name"]
                item = items[name]
                pid = row["prompt_id"]
                try:
                    if not pid:
                        update(db, name, "uploading")
                        batch = uuid.uuid4().hex
                        refs = [
                            client.upload_file(p, subfolder=f"storyboard/{batch}/{name}")
                            for p in item.pictures
                        ]
                        if STOP.is_set():
                            update(db, name, "failed", error="StoppedBeforeSubmit")
                            return
                        from comfyui_client.bindings import build_graph

                        graph = build_graph(
                            {
                                "prompt": item.prompt,
                                "length": item.seconds,
                                "aspect_ratio": item.aspect_ratio,
                                "reference_images": refs,
                            }
                        )
                        # Removal/config errors may release only definitely unsubmitted work.
                        with control.lock if control else contextlib.nullcontext():
                            if control and not control.enabled(server):
                                update(db, name, "pending", server=None, prompt_id=None, error=None)
                                return "drained"
                            update(db, name, "submitting")  # durable before network
                        pid = client.submit(graph)
                        update(db, name, "queued", prompt_id=pid)
                    history = client.wait(pid, timeout=timeout)
                    files = client.download_outputs(history, output, prompt_id=pid, subdir=name)
                    if not files:
                        raise ValueError("No downloaded outputs")
                    result = {
                        "segment": name,
                        "prompt_id": pid,
                        "status": "completed",
                        "server_fingerprint": server,
                        "files": [str(x.path) for x in files],
                    }
                    dst = output / f"multi-{name}.json"
                    temp = dst.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    temp.replace(dst)
                    update(db, name, "completed", error=None)
                    print(json.dumps({"segment": name, "status": "completed"}), flush=True)
                except Exception as exc:
                    # Exception text can contain URL/token; store class only.
                    with connect(db) as c:
                        state = c.execute(
                            "SELECT state FROM jobs WHERE name=?", (name,)
                        ).fetchone()[0]
                    update(
                        db,
                        name,
                        "queued" if pid else ("unknown" if state == "submitting" else "failed"),
                        error=type(exc).__name__,
                    )
                    print(
                        json.dumps(
                            {
                                "segment": name,
                                "status": "stopped_no_retry",
                                "error_type": type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
                    return "stopped"


def load_settings(root):
    vals = {}
    for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        if k in ("COMFYUI_URLS", "COMFYUI_API_KEY"):
            vals[k] = v.strip().strip("\"'")
    raw = vals.get("COMFYUI_URLS", "")
    try:
        urls = json.loads(raw)
    except json.JSONDecodeError:
        urls = [x for x in re.split(r"[,;\s]+", raw) if x]
    if not isinstance(urls, list) or not all(isinstance(x, str) for x in urls):
        raise ValueError("Invalid server configuration")
    urls = [x.strip().rstrip("/") for x in urls]
    if any(
        not x or urlsplit(x).scheme not in {"http", "https"} or not urlsplit(x).hostname
        for x in urls
    ):
        raise ValueError("Invalid server URL")
    if len(urls) != len(set(urls)):
        raise ValueError("Duplicate server configuration")
    return urls, vals.get("COMFYUI_API_KEY")


class DispatchControl:
    """Serializes config removal against claims and the durable submit boundary."""

    def __init__(self):
        self.lock = threading.RLock()
        self.active = set()
        self.valid = True

    def enabled(self, server):
        return self.valid and server in self.active and not STOP.is_set()


def dynamic_dispatch(
    db, root, items, output, locks, timeout, factory, interval=2.0, settings_loader=load_settings
):
    """Poll local configuration only. Removed in-flight workers drain; no health checks.

    Empty/invalid config pauses NEW claims. Known submitted jobs keep their original
    connection until completion. Errors block that worker for this invocation; no
    automatic retry of uploads/submits/waits. Re-add after restart to resume a known pid.
    """
    control = DispatchControl()
    threads = {}
    outcomes = {}
    blocked = set()
    last_status = None

    def work(server, url, key):
        try:
            outcomes[server] = worker(
                db, server, url, key, items, output, locks, timeout, factory, control
            )
        except Exception:
            outcomes[server] = "stopped"  # never log URLs or exception text

    try:
        while not STOP.is_set():
            try:
                urls, key = settings_loader(root)
                configured = {hashlib.sha256(u.encode()).hexdigest(): u for u in urls}
                with control.lock:
                    control.active = set(configured)
                    control.valid = True
                status = ("active" if configured else "paused_empty", len(configured))
            except Exception:
                configured = {}
                key = None
                with control.lock:
                    control.valid = False
                status = ("paused_invalid_config", 0)
            if status != last_status:
                print(json.dumps({"configuration": status[0], "servers": status[1]}), flush=True)
                last_status = status
            for server, thread in list(threads.items()):
                if not thread.is_alive():
                    thread.join()
                    if outcomes.pop(server, None) == "stopped":
                        blocked.add(server)
                    del threads[server]
            with connect(db) as c:
                states = [r[0] for r in c.execute("SELECT state FROM jobs")]
                queued_servers = {
                    r[0]
                    for r in c.execute(
                        "SELECT server FROM jobs WHERE state='queued' AND prompt_id IS NOT NULL"
                    )
                }
            if all(x == "completed" for x in states):
                break
            pending = "pending" in states
            for server, url in configured.items():
                if (
                    server not in threads
                    and server not in blocked
                    and (pending or server in queued_servers)
                ):
                    thread = threading.Thread(target=work, args=(server, url, key), daemon=False)
                    threads[server] = thread
                    thread.start()
            if not threads and control.valid and configured:
                # Unknown/failed jobs are never automatically moved or retried.
                break
            if not threads and not pending:
                break
            STOP.wait(interval)
    finally:
        # Ctrl+C does not cancel any remote task; workers already waiting can drain.
        for thread in threads.values():
            thread.join()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument(
        "--legacy-server-index",
        type=int,
        default=1,
        help="1-based server used by old sequential script",
    )
    p.add_argument(
        "--restart-interrupted",
        action="append",
        default=[],
        metavar="SEGMENT:PROMPT_ID",
        help="Explicit owner-authorized restart only; archives exact interrupted attempt, idempotent",
    )
    p.add_argument("--execute", action="store_true")
    p.add_argument("--job-timeout", type=float, default=7200)
    p.add_argument(
        "--config-poll-seconds",
        type=float,
        default=2.0,
        help="Poll local .env COMFYUI_URLS; [] pauses claims, removed nodes drain existing jobs",
    )
    a = p.parse_args()
    if not 0.1 <= a.config_poll_seconds <= 3600:
        raise ValueError("Invalid config poll interval")
    restarts = [tuple(x.split(":", 1)) for x in a.restart_interrupted]
    if any(len(x) != 2 for x in restarts):
        raise ValueError("Invalid restart specification")
    sys.path.insert(0, str(a.root / "workflows"))
    from comfyui_client.storyboard import load_storyboard_bundle
    from comfyui_client.transport import ComfyUIClient

    items = load_storyboard_bundle(a.manifest)
    urls, key = load_settings(a.root)
    if urls and not 1 <= a.legacy_server_index <= len(urls):
        raise ValueError("Invalid legacy server index")
    ids = [hashlib.sha256(x.encode()).hexdigest() for x in urls]
    output = a.output_dir.resolve()
    existing = records(output)
    db = output / "multi-dispatch.sqlite3"
    statuses = {
        n: ("completed" if r.get("status") == "completed" else "protected_existing_submission")
        for n, r in existing.items()
    }
    applied = set()
    persisted = {}
    if db.exists():
        with contextlib.closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
            persisted = {
                r[0]: r for r in c.execute("SELECT name,state,prompt_id FROM jobs").fetchall()
            }
            statuses.update({k: r[1] for k, r in persisted.items()})
            applied = set(c.execute("SELECT name,old_prompt FROM restarts").fetchall())
    for name, pid in restarts:
        if (name, pid) in applied:
            continue
        rec = persisted.get(name)
        actual_pid = rec[2] if rec else existing.get(name, {}).get("prompt_id")
        if actual_pid != pid or statuses.get(name) == "completed":
            raise ValueError("Interrupted restart does not match unfinished attempt")
        statuses[name] = "pending_explicit_restart"

    print(
        json.dumps(
            {
                "mode": "execute" if a.execute else "dry-run",
                "servers": len(urls),
                "explicit_interrupted_restarts": [x[0] for x in restarts],
                "tasks": [
                    {"segment": i.name, "state": statuses.get(i.name, "pending")} for i in items
                ],
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    if not a.execute:
        return 0
    output.mkdir(parents=True, exist_ok=True)
    with server_lock(output / ".dispatch-batch-lock", "batch") as batch_acquired:
        if not batch_acquired:
            raise ValueError("Another dispatcher owns this output batch")
        legacy_id = ids[a.legacy_server_index - 1] if ids else "no-legacy-server"
        if db.exists():
            with connect(db) as c:
                previous = c.execute(
                    "SELECT value FROM metadata WHERE key='legacy_server'"
                ).fetchone()
                if previous:
                    legacy_id = previous[0]
        if existing and legacy_id == "no-legacy-server":
            raise ValueError("Legacy records require an explicit configured server")
        initialize(db, items, existing, legacy_id, restarts)
        # Keep locks shared across all packages that use this project dispatcher.
        locks = a.root / "workflows" / ".comfy-dispatch-locks"

        def stop(signum, frame):
            STOP.set()
            print(
                "Stopping new claims; submitted server jobs are not cancelled. Waiting for active workers up to job-timeout; server jobs are not cancelled.",
                flush=True,
            )

        signal.signal(signal.SIGINT, stop)
        dynamic_dispatch(
            db,
            a.root,
            {i.name: i for i in items},
            output,
            locks,
            a.job_timeout,
            ComfyUIClient,
            a.config_poll_seconds,
        )
    with connect(db) as c:
        final = {r["name"]: r["state"] for r in c.execute("SELECT name,state FROM jobs")}
    print(
        json.dumps(
            {"final_states": final, "complete": all(x == "completed" for x in final.values())}
        ),
        flush=True,
    )
    return 0 if all(x == "completed" for x in final.values()) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"status": "stopped", "error_type": type(e).__name__}), flush=True)
        raise SystemExit(1)
