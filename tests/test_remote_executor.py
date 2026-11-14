import json
from pathlib import Path

import pytest

from pipeline.config import Config
from pipeline.remote import RemoteExecutor


class Client:
    def __init__(self, *, fail_submit=False):
        self.fail_submit = fail_submit
        self.submissions = 0
        self.closed = 0

    def close(self):
        self.closed += 1

    def submit(self, graph):
        self.submissions += 1
        if self.fail_submit:
            raise TimeoutError("lost response")
        return "job-1"

    def wait(self, prompt_id, timeout):
        return {"id": prompt_id}

    def download_outputs(self, history, directory):
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "video.mp4"
        output.write_bytes(b"video")
        return [output]


def test_client_ownership_and_context_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("COMFYUI_URLS", raising=False)
    injected = Client()
    with RemoteExecutor(Config(asset_library=str(tmp_path / "a.db")), comfy=injected) as executor:
        assert executor._client() is injected
    assert injected.closed == 0

    owned = Client()
    executor = RemoteExecutor(Config(asset_library=str(tmp_path / "b.db")))
    monkeypatch.setattr(executor._router, "client", lambda server: owned)
    assert executor._client() is owned
    executor.close()
    executor.close()
    assert owned.closed == 1


def test_sticky_server_and_unknown_submission_never_resubmits(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_URLS", json.dumps({"a": "http://a.test", "b": "http://b.test"}))
    monkeypatch.delenv("COMFYUI_DEFAULT_SERVER", raising=False)
    for name in ("IMAGE", "REF2VA", "I2VA"):
        monkeypatch.delenv(f"COMFYUI_{name}_SERVERS", raising=False)
    clients = {"a": Client(fail_submit=True), "b": Client()}
    config = Config(asset_library=str(tmp_path / "assets.db"))
    executor = RemoteExecutor(config, comfy=clients)
    executor.root = tmp_path / "run"
    executor.state = {"media": {}}
    selected = executor._activate_server("ref2va", "panel_1_video")
    with pytest.raises(TimeoutError):
        executor._job("panel_1_video", {"graph": 1})
    saved = json.loads((executor.root / "state.json").read_text(encoding="utf-8"))
    resumed = RemoteExecutor(config, comfy=clients)
    resumed.root = executor.root
    resumed.state = saved
    assert resumed._activate_server("ref2va", "panel_1_video") == selected
    with pytest.raises(RuntimeError, match="Submission outcome unknown"):
        resumed._job("panel_1_video", {"graph": 1})
    assert clients[selected].submissions == 1


def test_completed_job_is_cached_and_location_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFYUI_URLS", json.dumps({"a": "http://a.test"}))
    monkeypatch.delenv("COMFYUI_DEFAULT_SERVER", raising=False)
    for name in ("IMAGE", "REF2VA", "I2VA"):
        monkeypatch.delenv(f"COMFYUI_{name}_SERVERS", raising=False)
    client = Client()
    executor = RemoteExecutor(
        Config(asset_library=str(tmp_path / "assets.db")), comfy={"a": client}
    )
    executor.root = tmp_path / "run"
    executor.state = {"media": {}}
    executor._activate_server("ref2va", "panel_1_video")
    files = executor._job("panel_1_video", {"graph": 1})
    assert executor._job("panel_1_video", {"graph": 1}) == files
    assert client.submissions == 1
    assert executor._router.job_locations("job-1")[0]["server"] == "a"
    assert Path(files[0]).read_bytes() == b"video"


def test_reference_audio_is_rejected(tmp_path):
    config = Config(
        asset_library=str(tmp_path / "a.db"),
        media={"reference_audios": [{"path": "x.wav", "usage": "voice"}]},
    )
    with pytest.raises(ValueError, match="reference audio"):
        RemoteExecutor(config)
