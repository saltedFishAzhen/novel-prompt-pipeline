"""Resumable dependency-aware image batches. Dry-run is the default."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

from .client import DEFAULT_MODEL, ImageRequest, OpenAIImagesClient
from .errors import ImageError, RequestFailure


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_image(path):
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
        return path.stat().st_size > 0
    except (OSError, ValueError, SyntaxError):
        return False


@contextmanager
def file_lock(path):
    """OS-owned lock releases on process death. Never unlink a live lock inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise ImageError("This batch or output is already locked by another process.") from None
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                temp.replace(path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def load_env(path):
    """Read only supported image variables; never evaluate a shell or log values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in {"OPENAI_API_KEY", "OPENAI_IMAGE_MODEL"}:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def read_jobs(manifest):
    raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if raw.get("version") != 1 or not isinstance(raw.get("jobs"), list) or not raw["jobs"]:
        raise ImageError("Manifest needs version=1 and a nonempty jobs array.")
    jobs = {}
    outputs = set()
    for item in raw["jobs"]:
        job = dict(item)
        ident = job.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", ident):
            raise ImageError("Job IDs must be unique ASCII letters/digits/dot/dash/underscore.")
        if ident in jobs:
            raise ImageError("Duplicate job ID.")
        for name in ("promptFile", "outputFile"):
            job[name] = (manifest.parent / job[name]).resolve()
        if job["outputFile"].suffix.lower() != ".png":
            raise ImageError("Batch outputFile must end in .png.")
        if job["outputFile"] in outputs:
            raise ImageError("Duplicate output path.")
        outputs.add(job["outputFile"])
        job["references"] = [(manifest.parent / p).resolve() for p in job.get("references", [])]
        if len(job["references"]) > 16:
            raise ImageError("At most 16 references per job.")
        job["dependsOn"] = list(job.get("dependsOn", []))
        job["prompt"] = job["promptFile"].read_text(encoding="utf-8-sig")
        # Validate all non-file settings now; missing dependent refs are checked later.
        ImageRequest(
            prompt=job["prompt"],
            model=job.get("model", DEFAULT_MODEL),
            size=job.get("size", "auto"),
            quality=job.get("quality", "auto"),
        ).validate()
        jobs[ident] = job
    producers = {job["outputFile"]: ident for ident, job in jobs.items()}
    for ident, job in jobs.items():
        for ref in job["references"]:
            if ref in producers:
                dependency = producers[ref]
                if dependency not in job["dependsOn"]:
                    job["dependsOn"].append(dependency)
            elif not ref.is_file() or not valid_image(ref):
                raise ImageError(f"Missing or invalid external reference for {ident}.")
        if any(dep not in jobs for dep in job["dependsOn"]):
            raise ImageError("Unknown dependency ID.")
        if job["outputFile"] == job["promptFile"] or job["outputFile"] in job["references"]:
            raise ImageError("Output must not overwrite a prompt or reference.")
    done = set()
    while len(done) < len(jobs):
        ready = {i for i, j in jobs.items() if set(j["dependsOn"]) <= done} - done
        if not ready:
            raise ImageError("Dependency cycle.")
        done.update(ready)
    return jobs


class Batch:
    def __init__(
        self,
        manifest,
        *,
        concurrency=50,
        max_attempts=3,
        timeout=900,
        client_factory=OpenAIImagesClient,
        sleep=time.sleep,
    ):
        if not 1 <= concurrency <= 50 or not 1 <= max_attempts <= 10:
            raise ImageError("Concurrency must be 1..50 and max-attempts 1..10.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ImageError("Timeout must be positive and finite.")
        self.manifest = Path(manifest).resolve()
        self.jobs = read_jobs(self.manifest)
        self.root = self.manifest.parent / (self.manifest.stem + ".state")
        self.state_path = self.root / "state.json"
        self.concurrency, self.max_attempts, self.timeout = concurrency, max_attempts, timeout
        self.client_factory, self.sleep = client_factory, sleep
        self.mutex = threading.RLock()
        self.state = {"version": 1, "jobs": {}}

    def update(self, ident, **values):
        with self.mutex:
            self.state["jobs"].setdefault(ident, {}).update(values)
            atomic_json(self.state_path, self.state)

    def run_job(self, ident):
        job = self.jobs[ident]
        output = job["outputFile"]
        lock = output.parent / ("." + output.name + ".image.lock")
        with file_lock(lock):
            refs = tuple(job["references"])
            if any(not valid_image(p) for p in refs):
                self.update(ident, status="blocked", reason="invalid_reference")
                return
            request = ImageRequest(
                prompt=job["prompt"],
                operation="edit" if refs else "generate",
                images=refs,
                model=job.get("model", os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_MODEL),
                size=job.get("size", "auto"),
                quality=job.get("quality", "auto"),
            )
            request.validate()
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        **request.summary(),
                        "references": [digest(p) for p in refs],
                        "output": str(output),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            old = self.state["jobs"].get(ident, {})
            if old.get("fingerprint") not in {None, fingerprint}:
                self.update(ident, status="needs_attention", reason="inputs_changed")
                return
            if output.exists():
                if valid_image(output) and (
                    not old.get("sha256") or old["sha256"] == digest(output)
                ):
                    self.update(
                        ident,
                        status="completed",
                        fingerprint=fingerprint,
                        sha256=digest(output),
                        adopted=not bool(old.get("sha256")),
                    )
                else:
                    self.update(ident, status="needs_attention", reason="output_invalid_or_changed")
                return
            if old.get("status") in {"in_flight", "needs_attention", "failed", "completed"}:
                self.update(
                    ident,
                    status="needs_attention",
                    reason=old.get("reason", "previous_outcome_unresolved"),
                )
                return
            attempts = old.get("attempts", 0)
            self.update(ident, fingerprint=fingerprint)
            while attempts < self.max_attempts:
                delay = max(0, self.state["jobs"][ident].get("retry_at", 0) - time.time())
                if delay > 300:
                    return
                if delay:
                    self.sleep(delay)
                attempts += 1
                # Persist BEFORE dispatch. A crash at any later point must never auto-resubmit.
                self.update(ident, status="in_flight", attempts=attempts, retry_at=0)
                try:
                    result = self.client_factory(request_timeout=self.timeout).run(
                        request, self.root / "responses" / ident, execute=True
                    )
                except RequestFailure as exc:
                    if exc.retryable:
                        delay = (
                            exc.retry_after
                            if exc.retry_after is not None
                            else min(60, 2**attempts) + random.random()
                        )
                        # Do not shorten a server's long Retry-After; leave a resumable wait.
                        self.update(
                            ident,
                            status="retry_wait",
                            reason=exc.category,
                            http_status=exc.status_code,
                            retry_at=time.time() + delay,
                        )
                        if delay > 300 or attempts >= self.max_attempts:
                            return
                        continue
                    self.update(
                        ident,
                        status="failed" if exc.category == "rejected" else "needs_attention",
                        reason=exc.category,
                        http_status=exc.status_code,
                    )
                    return
                except Exception:
                    self.update(
                        ident, status="needs_attention", reason="request_or_save_outcome_unknown"
                    )
                    return
                try:
                    source = Path(result["run_dir"]) / result["outputs"][0]["file"]
                    if not valid_image(source):
                        raise ImageError("Response image cannot be decoded.")
                    # Hard-link is atomic and cannot replace an existing file. Same-volume
                    # response directory is deliberately adjacent to the manifest.
                    output.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source, output)
                    except OSError:
                        # Different volumes: exclusive creation still protects existing files.
                        with output.open("xb") as target, source.open("rb") as src:
                            shutil.copyfileobj(src, target)
                            target.flush()
                            os.fsync(target.fileno())
                    self.update(
                        ident,
                        status="completed",
                        sha256=digest(output),
                        response_dir=str(source.parent),
                    )
                    return
                except Exception:
                    self.update(
                        ident,
                        status="needs_attention",
                        reason="output_publish_failed",
                        response_dir=result.get("run_dir"),
                    )
                    return
            self.update(ident, status="retry_exhausted", reason="attempt_limit")

    def run(self, *, execute=False):
        if not execute:
            return {
                "status": "dry_run",
                "jobs": len(self.jobs),
                "concurrency": self.concurrency,
                "network_requested": False,
                "max_attempts": self.max_attempts,
            }
        with file_lock(self.root / "batch.lock"):
            if self.state_path.exists():
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            pending, running, completed = set(self.jobs), {}, set()
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                while pending or running:
                    ready = [i for i in pending if set(self.jobs[i]["dependsOn"]) <= completed]
                    for ident in ready[: self.concurrency - len(running)]:
                        pending.remove(ident)
                        running[pool.submit(self.run_job, ident)] = ident
                    if not running:
                        for ident in pending:
                            self.update(ident, status="blocked", reason="dependency_not_completed")
                        break
                    finished, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in finished:
                        ident = running.pop(future)
                        try:
                            future.result()
                        except Exception:
                            self.update(
                                ident,
                                status="needs_attention",
                                reason="local_error_or_output_locked",
                            )
                        if self.state["jobs"][ident]["status"] == "completed":
                            completed.add(ident)
            counts = {}
            for ident in self.jobs:
                status = self.state["jobs"][ident]["status"]
                counts[status] = counts.get(status, 0) + 1
            return {
                "status": "completed"
                if counts.get("completed") == len(self.jobs)
                else "incomplete",
                "counts": counts,
                "state_file": str(self.state_path),
            }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=900)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        if args.execute:
            load_env(args.env_file)
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                raise ImageError("Configure OPENAI_API_KEY in the project .env or environment.")
        runner = Batch(
            args.manifest,
            concurrency=args.concurrency,
            max_attempts=args.max_attempts,
            timeout=args.request_timeout,
        )
        result = runner.run(execute=args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"completed", "dry_run"} else 2
    except (ImageError, OSError, ValueError, KeyError, TypeError, ImportError):
        print(
            "Batch stopped: invalid/missing inputs, dependencies or state; no unsafe retry.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
