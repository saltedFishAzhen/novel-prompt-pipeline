r"""Local ComfyUI HTTP mock for testing H3 values without generating a video.

Example (PowerShell, from the repository root):
  .venv\\Scripts\\python.exe tests\\support\\mock_comfyui.py `
    --port 0 --run-dir runs\h3-live-20261115\mock-backend

The startup JSON prints the selected localhost URL. Pass it as COMFYUI_URL to
comfyui_client. This service accepts uploads and API-format graphs, checks every
LoadImage name, simulates one running history poll and one completion, and serves
a tiny file marked MOCK FIXTURE. It never calls a real ComfyUI or model service.
Audit JSONL, uploads and generated/downloaded fixture files stay in --run-dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not value
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or re.search(r"[?#\x00-\x1f\x7f]", value)
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise ValueError("unsafe ComfyUI input name")
    return normalized


def _make_mock_video(path: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to create the labeled mock video fixture")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required to draw the mock fixture label") from exc
    frame = Image.new("RGB", (320, 180), (48, 16, 48))
    draw = ImageDraw.Draw(frame)
    label = "MOCK VIDEO FIXTURE"
    font = ImageFont.load_default(size=22)
    bounds = draw.textbbox((0, 0), label, font=font)
    x = (frame.width - (bounds[2] - bounds[0])) // 2
    y = (frame.height - (bounds[3] - bounds[1])) // 2
    draw.text((x, y), label, fill=(255, 255, 255), font=font)
    frame_path = path.with_suffix(".frame.png")
    frame.save(frame_path)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "12",
        "-i",
        str(frame_path),
        "-t",
        "1",
        "-an",
        "-c:v",
        "mpeg4",
        "-q:v",
        "8",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-metadata",
        "title=MOCK VIDEO FIXTURE",
        "-metadata",
        "comment=Transport-test fixture; not model output",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    frame_path.unlink(missing_ok=True)
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe is required to validate the mock video fixture")
    result = subprocess.run(
        [probe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    media = json.loads(result.stdout)
    video = next((s for s in media["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("generated mock fixture has no video stream")
    details = {
        "purpose": "mock transport fixture only; not generated model output",
        "marker": "MOCK VIDEO FIXTURE",
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_seconds": float(media["format"].get("duration", 0)),
        "ffprobe": media,
    }
    path.with_suffix(".ffprobe.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return details


class MockState:
    def __init__(self, run_dir: Path, allowed_names: set[str]) -> None:
        self.run_dir = run_dir.resolve()
        self.input_dir = self.run_dir / "input"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.fixture = self.run_dir / "mock-fixture.mp4"
        self.fixture_info = _make_mock_video(self.fixture)
        self.allowed_names = {_safe_relative(name) for name in allowed_names}
        self.allowed_names.update(
            path.relative_to(self.input_dir).as_posix()
            for path in self.input_dir.rglob("*")
            if path.is_file()
        )
        self.lock = threading.Lock()
        self.history_counts: dict[str, int] = {}
        self.prompt_id = "mock-" + uuid.uuid4().hex[:12]
        self.submission: dict[str, Any] | None = None
        self.audit_path = self.run_dir / "audit.jsonl"

    def audit(self, record: dict[str, Any]) -> None:
        record["timestamp_unix"] = time.time()
        with self.lock, self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_handler(state: MockState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalMockComfyUI/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep default logs local and avoid printing header values.
            sys.stderr.write("mock-comfyui " + (fmt % args) + "\n")

        def send_json(self, value: Any, code: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/prompt":
                self.send_json(
                    {"exec_info": {"queue_remaining": 0}, "queue_running": [], "queue_pending": []}
                )
                state.audit({"method": "GET", "path": "/prompt", "result": "health"})
                return
            if parsed.path == "/history/" + state.prompt_id:
                with state.lock:
                    count = state.history_counts.get(state.prompt_id, 0) + 1
                    state.history_counts[state.prompt_id] = count
                if count == 1:
                    entry = {"status": {"status_str": "running", "completed": False}}
                else:
                    entry = {
                        "status": {"status_str": "success", "completed": True, "messages": []},
                        "outputs": {
                            "mock_video": {
                                "videos": [
                                    {
                                        "filename": state.fixture.name,
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                state.audit(
                    {"method": "GET", "path": parsed.path, "poll": count, "status": entry["status"]}
                )
                self.send_json({state.prompt_id: entry})
                return
            if parsed.path == "/view":
                query = parse_qs(parsed.query)
                filename = query.get("filename", [""])[0]
                subfolder = query.get("subfolder", [""])[0]
                media_type = query.get("type", [""])[0]
                if filename != state.fixture.name or subfolder or media_type != "output":
                    state.audit(
                        {
                            "method": "GET",
                            "path": "/view",
                            "result": "rejected",
                            "filename": filename,
                        }
                    )
                    self.send_error(404)
                    return
                data = state.fixture.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                state.audit(
                    {
                        "method": "GET",
                        "path": "/view",
                        "filename": filename,
                        "fixture_sha256": state.fixture_info["sha256"],
                        "bytes": len(data),
                        "purpose": "mock fixture download only",
                    }
                )
                return
            self.send_error(404)
            state.audit({"method": "GET", "path": parsed.path, "result": "not-found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if path == "/upload/image":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    self.send_error(400, "multipart required")
                    return
                from email.parser import BytesParser
                from email.policy import default

                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: "
                    + content_type.encode("ascii")
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + raw
                )
                fields: dict[str, str] = {}
                upload: tuple[str, bytes] | None = None
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    payload = part.get_payload(decode=True) or b""
                    if name == "image":
                        filename = part.get_filename() or "upload.bin"
                        upload = (filename, payload)
                    elif name:
                        fields[name] = payload.decode("utf-8", errors="replace")
                if fields.get("type") != "input" or upload is None:
                    self.send_error(400, "invalid image upload")
                    return
                subfolder = fields.get("subfolder", "")
                try:
                    safe_subfolder = _safe_relative(subfolder) if subfolder else ""
                    basename = Path(upload[0].replace("\\", "/")).name
                    safe_basename = re.sub(r"[^A-Za-z0-9._\-\u0080-\uffff]+", "_", basename).strip(
                        "._"
                    )
                    if not safe_basename:
                        raise ValueError
                except ValueError:
                    self.send_error(400, "unsafe upload name")
                    return
                relative_name = "/".join(part for part in (safe_subfolder, safe_basename) if part)
                target = (state.input_dir / relative_name).resolve()
                if not target.is_relative_to(state.input_dir):
                    self.send_error(400, "unsafe upload path")
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(upload[1])
                state.allowed_names.add(relative_name)
                record = {
                    "method": "POST",
                    "path": path,
                    "remote_name": relative_name,
                    "size_bytes": len(upload[1]),
                    "sha256": hashlib.sha256(upload[1]).hexdigest(),
                }
                state.audit(record)
                self.send_json(
                    {"name": safe_basename, "subfolder": safe_subfolder, "type": "input"}
                )
                return
            if path == "/prompt":
                try:
                    request = json.loads(raw)
                    graph = request.get("prompt")
                    if not isinstance(graph, dict) or not graph:
                        raise ValueError("prompt graph must be a non-empty object")
                    prompts: dict[str, str] = {}
                    image_nodes: dict[str, str] = {}
                    for node_id, node in graph.items():
                        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                            continue
                        inputs = node["inputs"]
                        if "prompt" in inputs and isinstance(inputs["prompt"], str):
                            prompts[str(node_id)] = inputs["prompt"]
                        if node.get("class_type") == "LoadImage":
                            remote = _safe_relative(inputs.get("image", ""))
                            image_nodes[str(node_id)] = remote
                    if not prompts or len(set(prompts.values())) != 1:
                        raise ValueError("graph must provide one consistent non-empty prompt")
                    if not next(iter(prompts.values())).strip():
                        raise ValueError("graph prompt cannot be empty")
                    unknown = sorted(set(image_nodes.values()) - state.allowed_names)
                    if unknown:
                        raise ValueError("unregistered graph image name(s): " + ", ".join(unknown))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    state.audit(
                        {"method": "POST", "path": path, "result": "rejected", "error": str(exc)}
                    )
                    self.send_json(
                        {"node_errors": {"mock": {"errors": [{"message": str(exc)}]}}}, 400
                    )
                    return
                with state.lock:
                    state.submission = {
                        "client_id": request.get("client_id"),
                        "prompt": next(iter(prompts.values())),
                        "prompt_nodes": prompts,
                        "images": image_nodes,
                        "graph_node_count": len(graph),
                    }
                state.audit(
                    {
                        "method": "POST",
                        "path": path,
                        "result": "accepted-mock-only",
                        "prompt_id": state.prompt_id,
                        **state.submission,
                    }
                )
                self.send_json({"prompt_id": state.prompt_id, "number": 1, "node_errors": {}})
                return
            self.send_error(404)
            state.audit({"method": "POST", "path": path, "result": "not-found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="localhost only by default")
    parser.add_argument("--port", type=int, default=0, help="0 chooses an available local port")
    parser.add_argument(
        "--run-dir", required=True, help="audit, upload and fixture output directory"
    )
    parser.add_argument(
        "--allow-name",
        action="append",
        default=[],
        help="pre-registered input name, repeat for each image already in a ComfyUI server",
    )
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state = MockState(run_dir, set(args.allow_name))
    server = ThreadingHTTPServer((args.host, args.port), create_handler(state))
    server.daemon_threads = True
    ready = {
        "server": "LOCAL MOCK ONLY",
        "base_url": f"http://{args.host}:{server.server_address[1]}",
        "pid": os.getpid(),
        "audit": str(state.audit_path),
        "fixture": state.fixture_info,
    }
    (run_dir / "ready.json").write_text(
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(ready, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
