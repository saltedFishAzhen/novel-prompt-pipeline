from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from pipeline.cli import main
from pipeline.comfy_servers import ComfyRouter
from pipeline.config import Config
from pipeline.engine import Pipeline
from pipeline.mcp_server import create_server
from pipeline.protocols import I2VA_ANCHOR
from pipeline.service import AppService


def video_prompt(mode: str) -> str:
    sound = "overall_soundscape:\nN/A\nnon_diegetic_music:\nN/A\n时长：6秒"
    if mode == "i2va":
        return (
            I2VA_ANCHOR
            + "\n\nintegrated_multimodal_description:\n[Shot 1] The room remains still.\n"
            + sound
        )
    return (
        "subject_definitions:\n<Picture 1> is the room.\n"
        "summary:\nThe room remains still.\n"
        "retention_analysis:\n<Picture 1>: fully_preserved.\n"
        "detailed_description:\n[Shot 1] <Picture 1> remains still.\n" + sound
    )


@pytest.fixture
def servers(monkeypatch):
    for key in (
        "COMFYUI_URLS",
        "COMFYUI_DEFAULT_SERVER",
        "COMFYUI_IMAGE_SERVERS",
        "COMFYUI_REF2VA_SERVERS",
        "COMFYUI_I2VA_SERVERS",
        "COMFYUI_BALANCE",
        "COMFYUI_GPU_A_API_KEY",
        "COMFYUI_GPU_B_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "COMFYUI_URLS",
        json.dumps({"gpu_a": "http://a.example:8188", "gpu_b": "http://b.example:8188"}),
    )
    return monkeypatch


def test_environment_servers_and_credentials_are_isolated(tmp_path, servers):
    servers.setenv("COMFYUI_API_KEY", "global-secret")
    servers.setenv("COMFYUI_GPU_A_API_KEY", "a-secret")
    router = ComfyRouter(Config(asset_library=str(tmp_path / "assets.sqlite3")))
    assert router.names == ("gpu_a", "gpu_b")
    assert "a.example" not in json.dumps(router.identity())
    assert "global-secret" not in json.dumps(router.identity())
    with router.client("gpu_a") as first, router.client("gpu_b") as second:
        assert first._headers()["Authorization"] == "Bearer a-secret"
        assert "Authorization" not in second._headers()
    servers.setenv("COMFYUI_URLS", "http://a.example:8188,http://b.example:8188")
    assert ComfyRouter(Config(asset_library=str(tmp_path / "assets.sqlite3"))).names == (
        "server1",
        "server2",
    )


def test_explicit_server_environment_names_and_pools(tmp_path, servers):
    servers.setenv("CUSTOM_IMAGE_URL", "http://image.example:8188")
    servers.setenv("COMFYUI_API_KEY", "shared-secret")
    config = Config(
        asset_library=str(tmp_path / "assets.sqlite3"),
        comfyui={
            "servers": {"image": {"url_env": "CUSTOM_IMAGE_URL", "api_key_env": "COMFYUI_API_KEY"}},
            "default_server": "gpu_a",
            "image_servers": ["image"],
            "ref2va_servers": ["gpu_b"],
        },
    )
    router = ComfyRouter(config)
    assert router.pool("image") == ("image",)
    assert router.pool("ref2va") == ("gpu_b",)
    assert router.pool("i2va") == ("gpu_a",)
    with router.client("image") as client:
        assert client._headers()["Authorization"] == "Bearer shared-secret"
    assert "shared-secret" not in json.dumps(config.public_dict())
    assert "image.example" not in json.dumps(router.identity())


@pytest.mark.parametrize("url", ["http://user:secret@example.com", "http://example.com:bad"])
def test_invalid_server_url_does_not_echo_private_values(tmp_path, servers, url):
    servers.setenv("COMFYUI_URLS", json.dumps({"bad": url}))
    router = ComfyRouter(Config(asset_library=str(tmp_path / "assets.sqlite3")))
    with pytest.raises(ValueError, match="Invalid URL for ComfyUI server bad") as exc:
        router.choose("image")
    assert url not in str(exc.value) and "secret" not in str(exc.value)


def test_round_robin_is_shared_across_processes_and_race_safe(tmp_path, servers):
    root = Path(__file__).resolve().parents[1]
    sources = os.pathsep.join(str(root / path) for path in ("src", "prompts/runtime"))
    env = dict(os.environ, PYTHONPATH=sources, PYTHONUTF8="1")
    code = (
        "import sys; from pipeline.comfy_servers import ComfyRouter; "
        "from pipeline.config import Config; "
        "print(ComfyRouter(Config(asset_library=sys.argv[1])).choose('image'))"
    )

    def choose(path):
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    cursor = tmp_path / "serial" / "assets.sqlite3"
    assert [choose(cursor) for _ in range(3)] == ["gpu_a", "gpu_b", "gpu_a"]
    cursor = tmp_path / "parallel" / "assets.sqlite3"
    with ThreadPoolExecutor(max_workers=6) as pool:
        choices = list(pool.map(choose, [cursor] * 6))
    assert choices.count("gpu_a") == choices.count("gpu_b") == 3


class FakeComfy:
    def __init__(self, name, *, fail_once=False):
        self.name = name
        self.fail_once = fail_once
        self.jobs = {}
        self.uploads = []
        self.waits = []
        self.health_checks = 0

    def health(self):
        self.health_checks += 1
        return {"status": "ok"}

    def upload_image(self, path):
        path = Path(path)
        self.uploads.append(path.read_bytes())
        return f"{self.name}/{path.name}"

    def upload_file(self, path, **kwargs):
        return self.upload_image(path)

    def submit(self, graph):
        prompt_id = f"job-{len(self.jobs) + 1}"
        self.jobs[prompt_id] = graph
        return prompt_id

    def wait(self, prompt_id, timeout):
        self.waits.append(prompt_id)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("simulated interrupted wait")
        return {"prompt_id": prompt_id}

    def download_outputs(self, history, directory, prompt_id=None):
        prompt_id = prompt_id or history["prompt_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}-{prompt_id}.png"
        Image.new("RGB", (2, 2), (len(self.jobs), len(prompt_id), len(self.name))).save(path)
        return [path]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_pipeline_cross_server_upload_and_resume_stay_pinned(tmp_path, servers):
    servers.setenv("COMFYUI_IMAGE_SERVERS", "gpu_a")
    servers.setenv("COMFYUI_REF2VA_SERVERS", "gpu_b")
    servers.setenv("COMFYUI_I2VA_SERVERS", "gpu_a")
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b", fail_once=True)
    config = Config(asset_library=str(tmp_path / "catalog/assets.sqlite3"))
    root = tmp_path / "run"
    root.mkdir()
    state = {
        "nodes": {},
        "assets": [],
        "media": {},
        "panels": [
            {
                "panel_index": i,
                "asset_ids": [],
                "first_frame_asset_ids": [],
                "prompt": "A room.",
                "video_prompt": video_prompt(mode),
                "duration_seconds": 6,
                "generation_mode": mode,
            }
            for i, mode in enumerate(("ref2va", "i2va"))
        ],
    }
    first = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    first.root, first.state = root, state
    with pytest.raises(TimeoutError, match="interrupted"):
        first._render()
    saved = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert saved["media"]["panel_0_image"]["server"] == "gpu_a"
    assert saved["media"]["panel_0_video"]["server"] == "gpu_b"
    assert len(a.jobs) == len(b.jobs) == 1 and len(b.uploads) == 1
    assert "gpu_b/" in json.dumps(next(iter(b.jobs.values())))
    assert "gpu_a/" not in json.dumps(next(iter(b.jobs.values())))
    second = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    second.root, second.state = root, saved
    second._render()
    assert len(b.jobs) == 1 and b.waits == ["job-1", "job-1"]
    assert len(b.uploads) == 1  # resumed upload is reused on B only
    assert len(a.jobs) == 3 and len(a.uploads) == 1
    assert second.state["media"]["panel_1_video"]["server"] == "gpu_a"
    assert "gpu_a/" in json.dumps(a.jobs["job-3"])
    assert "a.example" not in json.dumps(second.state)


def test_upload_cache_is_per_server_and_changed_endpoint_fails_closed(tmp_path, servers):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    config = Config(asset_library=str(tmp_path / "assets.sqlite3"))
    pipeline = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    pipeline.root = tmp_path
    pipeline.state = {"media": {}}
    image = tmp_path / "input.png"
    image.write_bytes(b"same pixels")
    pipeline._activate_server("image", "job-a", preferred="gpu_a")
    assert pipeline._upload(image) == "gpu_a/input.png"
    pipeline._activate_server("image", "job-b", preferred="gpu_b")
    assert pipeline._upload(image) == "gpu_b/input.png"
    pipeline._activate_server("image", "job-a", preferred="gpu_a")
    assert pipeline._upload(image) == "gpu_a/input.png"
    assert len(a.uploads) == len(b.uploads) == 1
    servers.setenv(
        "COMFYUI_URLS",
        json.dumps({"gpu_a": "http://different.example:8188", "gpu_b": "http://b.example:8188"}),
    )
    resumed = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    resumed.root, resumed.state = tmp_path, pipeline.state
    with pytest.raises(ValueError, match="assignment changed"):
        resumed._activate_server("image", "job-a", preferred="gpu_a")


def test_remote_audio_requires_known_server_before_any_job(tmp_path, servers):
    config = Config(asset_library=str(tmp_path / "assets.sqlite3"))
    config.media.reference_audios = [
        {"path": "already-on-server.wav", "usage": "ambience", "remote": True}
    ]
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    pipeline = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    pipeline.root = tmp_path
    pipeline.state = {
        "panels": [
            {
                "panel_index": 0,
                "asset_ids": [],
                "generation_mode": "ref2va",
                "video_prompt": "x",
                "duration_seconds": 6,
            }
        ],
        "media": {},
    }
    with pytest.raises(ValueError, match="Remote audio must specify its server"):
        pipeline._render()
    assert not a.jobs and not b.jobs


def test_remote_audio_pins_video_to_its_server(tmp_path, servers):
    servers.setenv("COMFYUI_IMAGE_SERVERS", "gpu_a")
    config = Config(asset_library=str(tmp_path / "assets.sqlite3"))
    config.media.reference_audios = [
        {"path": "existing-audio.wav", "usage": "ambience", "remote": True, "server": "gpu_b"}
    ]
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    pipeline = Pipeline(config, comfy={"gpu_a": a, "gpu_b": b})
    pipeline.root = tmp_path
    pipeline.state = {
        "nodes": {},
        "assets": [],
        "media": {},
        "panels": [
            {
                "panel_index": 0,
                "asset_ids": [],
                "first_frame_asset_ids": [],
                "generation_mode": "ref2va",
                "prompt": "A room.",
                "video_prompt": video_prompt("ref2va"),
                "duration_seconds": 6,
            }
        ],
    }
    pipeline._render()
    assert pipeline.state["media"]["panel_0_video"]["server"] == "gpu_b"
    assert "existing-audio.wav" in json.dumps(b.jobs["job-1"])
    assert len(b.uploads) == 1  # first frame only; the audio is already remote


def test_unsubmitted_video_assignment_can_follow_explicit_mode_change(tmp_path, servers):
    servers.setenv("COMFYUI_REF2VA_SERVERS", "gpu_a")
    servers.setenv("COMFYUI_I2VA_SERVERS", "gpu_b")
    pipeline = Pipeline(Config(asset_library=str(tmp_path / "assets.sqlite3")))
    pipeline.root, pipeline.state = tmp_path, {"media": {}}
    pipeline._activate_server("ref2va", "panel_0_video")
    assert pipeline._active_server == "gpu_a"
    pipeline._activate_server("i2va", "panel_0_video")
    assert pipeline._active_server == "gpu_b"
    pipeline.state["media"]["panel_0_video"] = {"server": "gpu_b"}
    with pytest.raises(ValueError, match="assignment changed"):
        pipeline._activate_server("ref2va", "panel_0_video")


def test_unknown_submission_does_not_retry_or_switch_server(tmp_path, servers):
    class LostResponse(FakeComfy):
        def submit(self, graph):
            super().submit(graph)
            raise TimeoutError("response lost")

    a, b = LostResponse("gpu_a"), FakeComfy("gpu_b")
    pipeline = Pipeline(
        Config(asset_library=str(tmp_path / "assets.sqlite3")), comfy={"gpu_a": a, "gpu_b": b}
    )
    pipeline.root, pipeline.state = tmp_path, {"media": {}}
    pipeline._activate_server("image", "case")
    with pytest.raises(TimeoutError):
        pipeline._job("case", {"prompt": "still scene"})
    record = pipeline.state["media"]["case"]
    assert (record["server"], record["status"]) == ("gpu_a", "submitting")
    with pytest.raises(RuntimeError, match="outcome unknown"):
        pipeline._job("case", {"prompt": "still scene"})
    assert len(a.jobs) == 1 and not b.jobs


def test_run_resume_rejects_changed_server_url(tmp_path, servers, monkeypatch):
    config = Config(asset_library=str(tmp_path / "assets.sqlite3"), generation_mode="ref2va")
    first = Pipeline(config)
    monkeypatch.setattr(first, "_infer", lambda _: first.state.update(panels=[], assets=[]))
    first.run("A short scene.", tmp_path / "run")
    servers.setenv(
        "COMFYUI_URLS",
        json.dumps({"gpu_a": "http://changed.example:8188", "gpu_b": "http://b.example:8188"}),
    )
    with pytest.raises(ValueError, match="Source/config/workflow changed"):
        Pipeline(config).run("A short scene.", tmp_path / "run", resume=True)


def test_legacy_base_url_change_is_part_of_run_identity(tmp_path, servers, monkeypatch):
    servers.delenv("COMFYUI_URLS")
    servers.delenv("COMFYUI_URL", raising=False)
    servers.setenv("COMFYUI_BASE_URL", "http://old.example:8188")
    config = Config(asset_library=str(tmp_path / "assets.sqlite3"), generation_mode="ref2va")
    first = Pipeline(config)
    monkeypatch.setattr(first, "_infer", lambda _: first.state.update(panels=[], assets=[]))
    first.run("A short scene.", tmp_path / "run")
    servers.setenv("COMFYUI_BASE_URL", "http://new.example:8188")
    with pytest.raises(ValueError, match="Source/config/workflow changed"):
        Pipeline(config).run("A short scene.", tmp_path / "run", resume=True)


def test_reused_pipeline_recreates_owned_client_if_legacy_url_changes(
    tmp_path, servers, monkeypatch
):
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://old.example:8188")

    class Client:
        def __init__(self, url):
            self.url = url
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(ComfyRouter, "client", lambda router, _: Client(router.legacy_url))
    pipeline = Pipeline(
        Config(asset_library=str(tmp_path / "assets.sqlite3"), generation_mode="ref2va")
    )
    previous = pipeline._client()
    servers.setenv("COMFYUI_URL", "http://new.example:8188")
    monkeypatch.setattr(pipeline, "_infer", lambda _: pipeline.state.update(panels=[], assets=[]))
    pipeline.run("A short scene.", tmp_path / "run")
    assert previous.closed
    assert pipeline._client().url == "http://new.example:8188"


def test_standalone_known_upload_cannot_be_used_on_another_server(tmp_path, servers, monkeypatch):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: {"gpu_a": a, "gpu_b": b}[name]
    )
    picture = tmp_path / "input.png"
    picture.write_bytes(b"input data")
    uploaded = service.comfy_upload(picture, server="gpu_a")
    assert uploaded["server"] == "gpu_a"
    values = {"prompt": "scene", "reference_images": [uploaded["remote_name"]]}
    with pytest.raises(ValueError, match="different ComfyUI server"):
        service.comfy_submit("qwen", values, server="gpu_b")
    assert not b.jobs
    assert service.comfy_submit("qwen", values, server="gpu_a")["server"] == "gpu_a"
    assert len(a.jobs) == 1
    servers.setenv(
        "COMFYUI_URLS",
        json.dumps({"gpu_a": "http://replacement.example:8188", "gpu_b": "http://b.example:8188"}),
    )
    with pytest.raises(ValueError, match="different ComfyUI server or URL"):
        service.comfy_submit("qwen", values, server="gpu_a")
    assert len(a.jobs) == 1


def test_health_check_visits_pool_without_advancing_job_round_robin(tmp_path, servers, monkeypatch):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: {"gpu_a": a, "gpu_b": b}[name]
    )
    result = service.comfy_check("qwen", remote=True)
    assert set(result["remote_servers"]) == {"gpu_a", "gpu_b"}
    assert a.health_checks == b.health_checks == 1
    assert service.comfy_submit("qwen", {"prompt": "a room"})["server"] == "gpu_a"


def test_direct_submit_wait_and_uploaded_references_are_server_bound(
    tmp_path, servers, monkeypatch
):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    clients = {"gpu_a": a, "gpu_b": b}
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "_comfy_client", lambda name=None, **kwargs: clients[name])
    first = service.comfy_submit("qwen", {"prompt": "a room"})
    second = service.comfy_submit("qwen", {"prompt": "another room"})
    assert (first["server"], second["server"]) == ("gpu_a", "gpu_b")
    assert first["prompt_id"] == second["prompt_id"] == "job-1"
    with pytest.raises(ValueError, match="Specify the ComfyUI server"):
        service.comfy_wait("job-1")
    assert service.comfy_wait("job-1", server="gpu_b")["server"] == "gpu_b"
    assert b.waits == ["job-1"] and not a.waits
    with pytest.raises(ValueError, match="holding the referenced media"):
        service.comfy_submit("qwen", {"prompt": "room", "reference_images": ["gpu_a/input.png"]})
    with pytest.raises(ValueError, match="Specify the ComfyUI server"):
        service.comfy_wait("external-unknown-id")
    servers.setenv(
        "COMFYUI_URLS",
        json.dumps({"gpu_a": "http://a.example:8188", "gpu_b": "http://changed.example:8188"}),
    )
    with pytest.raises(ValueError, match="server URL changed"):
        service.comfy_wait("job-1", server="gpu_b")


def test_wait_resolves_the_unique_saved_server_without_guessing(tmp_path, servers, monkeypatch):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: {"gpu_a": a, "gpu_b": b}[name]
    )
    job = service.comfy_submit("qwen", {"prompt": "a room"}, server="gpu_b")
    assert service.comfy_wait(job["prompt_id"])["server"] == "gpu_b"
    assert b.waits == [job["prompt_id"]] and not a.waits


def test_wait_resolves_job_across_output_directories(tmp_path, servers, monkeypatch):
    client = FakeComfy("gpu_b")
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "_comfy_client", lambda *args, **kwargs: client)
    job = service.comfy_submit("qwen", {"prompt": "a room"}, "runs/first", server="gpu_b")
    assert service.comfy_wait(job["prompt_id"], "runs/second")["server"] == "gpu_b"
    assert client.waits == [job["prompt_id"]]


@pytest.mark.parametrize("requested", [None, "default"])
def test_wait_across_directories_rejects_removed_named_server(
    tmp_path, servers, monkeypatch, requested
):
    named, legacy = FakeComfy("gpu_b"), FakeComfy("legacy")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: named if name else legacy
    )
    job = service.comfy_submit("qwen", {"prompt": "a room"}, "runs/first", server="gpu_b")
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://legacy.example:8188")
    with pytest.raises(ValueError, match="named ComfyUI server is no longer configured"):
        service.comfy_wait(job["prompt_id"], "runs/second", server=requested)
    assert not legacy.waits and not named.waits


def test_registry_failure_after_submission_preserves_job_record(tmp_path, servers, monkeypatch):
    client = FakeComfy("gpu_b")
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "_comfy_client", lambda *args, **kwargs: client)

    def fail_registry(*args, **kwargs):
        raise OSError("simulated registry failure")

    monkeypatch.setattr(ComfyRouter, "record_job", fail_registry)
    with pytest.raises(RuntimeError, match="do not resubmit"):
        service.comfy_submit("qwen", {"prompt": "a room"}, server="gpu_b")
    records = list((tmp_path / "runs/comfyui").glob("submission-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["prompt_id"] == "job-1" and record["server"] == "gpu_b"
    assert len(client.jobs) == 1 and not client.waits


@pytest.mark.parametrize("requested", [None, "default"])
def test_named_job_cannot_fall_back_to_legacy_server_after_pool_removed(
    tmp_path, servers, monkeypatch, requested
):
    named, legacy = FakeComfy("gpu_b"), FakeComfy("legacy")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: named if name else legacy
    )
    job = service.comfy_submit("qwen", {"prompt": "a room"}, server="gpu_b")
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://legacy.example:8188")
    with pytest.raises(ValueError, match="named ComfyUI server is no longer configured"):
        service.comfy_wait(job["prompt_id"], server=requested)
    assert not legacy.waits and not named.waits


def test_named_upload_cannot_be_submitted_to_legacy_after_pool_removed(
    tmp_path, servers, monkeypatch
):
    named, legacy = FakeComfy("gpu_b"), FakeComfy("legacy")
    service = AppService(tmp_path)
    monkeypatch.setattr(
        service, "_comfy_client", lambda name=None, **kwargs: named if name else legacy
    )
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    uploaded = service.comfy_upload(image, server="gpu_b")
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://legacy.example:8188")
    with pytest.raises(ValueError, match="different ComfyUI server or URL"):
        service.comfy_submit(
            "qwen", {"prompt": "a room", "reference_images": [uploaded["remote_name"]]}
        )
    assert not legacy.jobs and not named.jobs


def test_legacy_upload_is_bound_to_its_original_url(tmp_path, servers, monkeypatch):
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://old.example:8188")
    client = FakeComfy("legacy")
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "_comfy_client", lambda *args, **kwargs: client)
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    uploaded = service.comfy_upload(image)
    servers.setenv("COMFYUI_URL", "http://replacement.example:8188")
    with pytest.raises(ValueError, match="different ComfyUI server or URL"):
        service.comfy_submit(
            "qwen", {"prompt": "a room", "reference_images": [uploaded["remote_name"]]}
        )
    assert not client.jobs


def test_legacy_standalone_job_rejects_changed_endpoint(tmp_path, servers, monkeypatch):
    servers.delenv("COMFYUI_URLS")
    servers.setenv("COMFYUI_URL", "http://old.example:8188")
    client = FakeComfy("legacy")
    service = AppService(tmp_path)
    monkeypatch.setattr(service, "_comfy_client", lambda *args, **kwargs: client)
    job = service.comfy_submit("qwen", {"prompt": "a room"})
    servers.setenv("COMFYUI_URL", "http://new.example:8188")
    with pytest.raises(ValueError, match="server URL changed"):
        service.comfy_wait(job["prompt_id"])
    assert not client.waits


def test_cli_and_mcp_expose_server_selection_without_credentials(tmp_path, servers, capsys):
    servers.setenv("COMFYUI_GPU_A_API_KEY", "a-private-test-key")
    assert main(["--workspace", str(tmp_path), "comfy", "servers"]) == 0
    output = capsys.readouterr().out
    assert [item["name"] for item in json.loads(output)["servers"]] == ["gpu_a", "gpu_b"]
    assert "a-private-test-key" not in output and "a.example" not in output
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "comfy",
                "check",
                "--profile",
                "qwen",
                "--server",
                "gpu_a",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["server"] == "gpu_a"

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"prompt_project": "prompts", "asset_library": "runs/library/assets.sqlite3"}),
        encoding="utf-8",
    )

    async def exercise_mcp():
        server = create_server(tmp_path, config_path=config_path, env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            tools = {item.name: item for item in (await session.list_tools()).tools}
            assert "comfy_servers" in tools
            for name in ("comfy_check", "comfy_upload", "comfy_submit", "comfy_wait"):
                assert "server" in tools[name].inputSchema["properties"]
            result = await session.call_tool("comfy_servers", {})
            assert not result.isError
            assert [item["name"] for item in result.structuredContent["servers"]] == [
                "gpu_a",
                "gpu_b",
            ]
            assert "a-private-test-key" not in result.model_dump_json()
            assert "a.example" not in result.model_dump_json()
            result = await session.call_tool("comfy_check", {"profile": "qwen", "server": "gpu_b"})
            assert not result.isError and result.structuredContent["server"] == "gpu_b"

    anyio.run(exercise_mcp)


def test_cli_and_mcp_upload_submit_and_wait_stay_on_explicit_server(
    tmp_path, servers, monkeypatch, capsys
):
    a, b = FakeComfy("gpu_a"), FakeComfy("gpu_b")
    clients = {"gpu_a": a, "gpu_b": b}
    monkeypatch.setattr(AppService, "_comfy_client", lambda self, name=None, **kw: clients[name])
    (tmp_path / "input.png").write_bytes(b"source image")
    assert (
        main(["--workspace", str(tmp_path), "comfy", "upload", "input.png", "--server", "gpu_b"])
        == 0
    )
    uploaded = json.loads(capsys.readouterr().out)
    assert uploaded["server"] == "gpu_b"
    (tmp_path / "values.json").write_text(
        json.dumps({"prompt": "A room.", "reference_images": [uploaded["remote_name"]]}),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "comfy",
                "submit",
                "--profile",
                "qwen",
                "--values",
                "values.json",
                "--server",
                "gpu_b",
            ]
        )
        == 0
    )
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["server"] == "gpu_b"
    assert (
        main(
            [
                "--workspace",
                str(tmp_path),
                "comfy",
                "wait",
                submitted["prompt_id"],
                "--server",
                "gpu_b",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["server"] == "gpu_b"
    assert not a.jobs and len(b.jobs) == 1 and b.waits == ["job-1"]

    workspace = tmp_path / "mcp"
    workspace.mkdir()
    (workspace / "input.png").write_bytes(b"mcp image")
    (workspace / "config.json").write_text(
        json.dumps({"prompt_project": "prompts", "asset_library": "runs/library/assets.sqlite3"}),
        encoding="utf-8",
    )

    async def exercise_mcp():
        server = create_server(workspace, config_path="config.json", env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "comfy_upload", {"file": "input.png", "server": "gpu_a"}
            )
            assert not result.isError and result.structuredContent["server"] == "gpu_a"
            remote = result.structuredContent["remote_name"]
            result = await session.call_tool(
                "comfy_submit",
                {
                    "profile": "qwen",
                    "values": {"prompt": "A room.", "reference_images": [remote]},
                    "server": "gpu_a",
                },
            )
            assert not result.isError and result.structuredContent["server"] == "gpu_a"
            prompt_id = result.structuredContent["prompt_id"]
            result = await session.call_tool(
                "comfy_wait", {"prompt_id": prompt_id, "server": "gpu_a"}
            )
            assert not result.isError and result.structuredContent["server"] == "gpu_a"

    anyio.run(exercise_mcp)
    assert len(a.jobs) == 1 and a.waits == ["job-1"]
