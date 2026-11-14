"""CLI, MCP and shared service for a separately configured Breeze endpoint."""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import anyio
import httpx
import pytest
from filelock import FileLock
from mcp.shared.memory import create_connected_server_and_client_session

from pipeline.cli import main
from pipeline.mcp_server import create_server
from pipeline.service import AppService, WorkspacePathError
from tts import BreezeTTSClient


@pytest.fixture
def fake_tts(monkeypatch):
    calls = []
    secret = "tts-fixture-secret"
    monkeypatch.setenv("BREEZE_TTS_URL", "https://tts.example.invalid")
    monkeypatch.setenv("BREEZE_TTS_API_KEY", secret)

    def handle(request):
        calls.append(request)
        assert request.headers.get("Authorization") == "Bearer " + secret
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"status": "ok", "sample_rate": 24000, "secret": secret}
            )
        if request.url.path == "/v1/model":
            return httpx.Response(
                200,
                json={"model_digest": "a" * 64, "frame_rate": 12, "model_path": "/private/model"},
            )
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(
                200,
                content=b"\x00\x00" * 2400,
                headers={
                    "Content-Type": "audio/pcm",
                    "X-Sample-Rate": "24000",
                    "X-Sample-Format": "s16le",
                },
            )
        return httpx.Response(404)

    def factory(config):
        return BreezeTTSClient(
            "https://tts.example.invalid",
            api_key=secret,
            timeout=config.timeout,
            max_audio_bytes=config.max_audio_bytes,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(BreezeTTSClient, "from_config", staticmethod(factory))
    return calls


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00" * 1200)


def write_values(path: Path, **overrides) -> None:
    data = {"text": "阿青轻声说：你好。", "instruction": "A calm, low adult voice.", "seed": 42}
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def register_asset(service: AppService, *, kind: str = "character") -> str:
    library = service.library()
    try:
        asset = library.register(
            [
                {
                    "asset_id": "person-a",
                    "kind": kind,
                    "name": "阿青",
                    "aliases": "阿青",
                    "description": "A person.",
                }
            ]
        )[0]
        return asset["asset_id"]
    finally:
        library.close()


def test_unconfigured_tts_check_does_not_probe_other_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("BREEZE_TTS_URL", raising=False)
    monkeypatch.setenv("UNRELATED_SERVICE_URL", "https://unrelated.invalid")
    monkeypatch.setattr(BreezeTTSClient, "from_config", lambda _: pytest.fail("must not connect"))
    assert AppService(tmp_path).tts_check() == {"configured": False, "status": "not_configured"}


def test_cli_check_respects_configured_tts_environment_name(
    tmp_path, fake_tts, monkeypatch, capsys
):
    monkeypatch.delenv("BREEZE_TTS_URL")
    monkeypatch.setenv("CUSTOM_TTS_URL", "https://configured.invalid")
    (tmp_path / "config.json").write_text('{"tts":{"url_env":"CUSTOM_TTS_URL"}}', encoding="utf-8")
    assert main(["--workspace", str(tmp_path), "tts", "--config", "config.json", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["configured"] is True
    assert [request.url.path for request in fake_tts] == ["/health", "/v1/model"]


def test_cli_creates_wav_and_character_reference_with_metadata(tmp_path, fake_tts, capsys):
    service = AppService(tmp_path)
    asset_id = register_asset(service)
    write_values(tmp_path / "requests/voice.json")

    def invoke(*args):
        assert main(["--workspace", str(tmp_path), "tts", *args]) == 0
        return json.loads(capsys.readouterr().out)

    health = invoke("check")
    assert health["health"] == {"status": "ok", "sample_rate": 24000}
    assert health["model"] == {"model_digest": "a" * 64, "frame_rate": 12}
    assert "tts-fixture-secret" not in json.dumps(health)
    result = invoke(
        "synthesize",
        "--values",
        "requests/voice.json",
        "--output",
        "voices/a.wav",
        "--asset-id",
        asset_id,
    )
    audio = Path(result["output"])
    metadata = json.loads(Path(result["metadata_file"]).read_text(encoding="utf-8"))
    assert metadata["request"]["text"] == "阿青轻声说：你好。"
    assert metadata["request"]["instruction"] == "A calm, low adult voice."
    assert metadata["request"]["seed"] == 42
    assert metadata["request"]["mode"] == "design"
    assert metadata["audio"]["sha256"] == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert metadata["audio"]["sample_rate"] == 24000
    assert metadata["audio"]["duration_seconds"] == 0.1
    assert result["asset"]["voice"]["role"] == "voice_reference"
    assert result["asset"]["metadata"]["role"] == "voice_reference_metadata"
    assert "tts-fixture-secret" not in json.dumps(metadata)
    assert "/private/model" not in json.dumps(metadata)
    assert [request.url.path for request in fake_tts] == [
        "/health",
        "/v1/model",
        "/v1/model",
        "/v1/audio/speech",
    ]

    again = invoke(
        "synthesize",
        "--values",
        "requests/voice.json",
        "--output",
        "voices/a.wav",
        "--asset-id",
        asset_id,
    )
    assert again["reused"] is True
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1


def test_reference_is_relative_to_values_file_and_hashes_actual_bytes(tmp_path, fake_tts):
    reference = tmp_path / "requests/ref.wav"
    write_wav(reference)
    write_values(
        tmp_path / "requests/voice.json", reference_audio="ref.wav", reference_text="准确转写"
    )
    result = AppService(tmp_path).tts_synthesize("requests/voice.json", "voices/a.wav")
    metadata = json.loads(Path(result["metadata_file"]).read_text(encoding="utf-8"))
    assert (
        metadata["request"]["reference_audio_sha256"]
        == hashlib.sha256(reference.read_bytes()).hexdigest()
    )
    assert metadata["request"]["reference_text"] == "准确转写"
    assert metadata["request"]["mode"] == "direction"
    sent = next(request for request in fake_tts if request.url.path == "/v1/audio/speech")
    assert b'name="ref_audio"' in sent.content
    assert b'name="ref_text"' in sent.content


@pytest.mark.parametrize("existing", ["audio", "metadata"])
def test_existing_output_is_never_overwritten_or_sent(tmp_path, fake_tts, existing):
    write_values(tmp_path / "voice.json")
    target = tmp_path / ("a.wav" if existing == "audio" else "a.voice.json")
    target.write_bytes(b"preserve prior result")
    with pytest.raises(FileExistsError):
        AppService(tmp_path).tts_synthesize("voice.json", "a.wav")
    assert target.read_bytes() == b"preserve prior result"
    assert not fake_tts


def test_invalid_asset_and_unsupported_values_fail_before_server_calls(tmp_path, fake_tts):
    service = AppService(tmp_path)
    asset_id = register_asset(service, kind="scene")
    write_values(tmp_path / "voice.json")
    with pytest.raises(ValueError, match="character"):
        service.tts_synthesize("voice.json", "a.wav", asset_id)
    write_values(tmp_path / "voice.json", base_url="https://other.invalid", api_key="secret")
    with pytest.raises(ValueError, match="unsupported"):
        service.tts_synthesize("voice.json", "a.wav")
    assert not fake_tts


def test_locked_output_rejects_concurrent_request_before_server_calls(tmp_path, fake_tts):
    write_values(tmp_path / "voice.json")
    with FileLock(str(tmp_path / "a.voice.lock"), timeout=0):
        with pytest.raises(RuntimeError, match="Another TTS"):
            AppService(tmp_path).tts_synthesize("voice.json", "a.wav")
    assert not (tmp_path / "a.wav").exists()
    assert not fake_tts


def test_asset_link_failure_can_be_recovered_without_repeat_synthesis(
    tmp_path, fake_tts, monkeypatch
):
    from pipeline.library import AssetLibrary

    service = AppService(tmp_path)
    asset_id = register_asset(service)
    write_values(tmp_path / "voice.json")
    original = AssetLibrary.link_file
    monkeypatch.setattr(
        AssetLibrary,
        "link_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("catalog failure")),
    )
    with pytest.raises(RuntimeError, match="catalog failure"):
        service.tts_synthesize("voice.json", "a.wav", asset_id)
    wav = (tmp_path / "a.wav").read_bytes()
    assert (tmp_path / "a.voice.json").is_file()
    monkeypatch.setattr(AssetLibrary, "link_file", original)
    monkeypatch.delenv("BREEZE_TTS_URL")
    recovered = service.tts_synthesize("voice.json", "a.wav", asset_id)
    assert recovered["reused"] is True
    assert (tmp_path / "a.wav").read_bytes() == wav
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1


def test_failed_synthesis_does_not_publish_wav_or_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("BREEZE_TTS_URL", "https://tts.example.invalid")

    def handle(request):
        if request.url.path == "/v1/model":
            return httpx.Response(200, json={"model_digest": "a" * 64})
        return httpx.Response(
            200,
            content=b"\x00",
            headers={
                "Content-Type": "audio/pcm",
                "X-Sample-Rate": "24000",
                "X-Sample-Format": "s16le",
            },
        )

    def factory(config):
        return BreezeTTSClient(
            "https://tts.example.invalid",
            timeout=config.timeout,
            max_audio_bytes=config.max_audio_bytes,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(BreezeTTSClient, "from_config", staticmethod(factory))
    write_values(tmp_path / "voice.json")
    with pytest.raises(RuntimeError, match="incomplete PCM"):
        AppService(tmp_path).tts_synthesize("voice.json", "a.wav")
    assert not (tmp_path / "a.wav").exists()
    assert not (tmp_path / "a.voice.json").exists()
    assert not list(tmp_path.glob(".tts-stage-*"))


def test_mcp_checks_paths_and_returns_only_relative_files(tmp_path, fake_tts):
    write_values(tmp_path / "requests/voice.json")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    async def exercise():
        server = create_server(tmp_path, config_path="config.json", env_loaded=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("tts_check", {})
            assert not result.isError, result.model_dump()
            assert "tts-fixture-secret" not in result.model_dump_json()
            result = await session.call_tool(
                "tts_synthesize", {"values": "requests/voice.json", "output": "voices/a.wav"}
            )
            assert not result.isError, result.model_dump()
            assert (
                result.structuredContent["output"] == "voices\\a.wav"
                or result.structuredContent["output"] == "voices/a.wav"
            )
            assert not Path(result.structuredContent["metadata_file"]).is_absolute()
            denied = await session.call_tool(
                "tts_synthesize", {"values": "requests/voice.json", "output": "../outside.wav"}
            )
            assert denied.isError

    anyio.run(exercise)
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1


def test_mcp_rejects_reference_outside_workspace(tmp_path):
    write_values(
        tmp_path / "requests/voice.json",
        reference_audio="../../outside.wav",
        reference_text="transcript",
    )
    with pytest.raises(WorkspacePathError):
        AppService(tmp_path, restricted=True).tts_synthesize("requests/voice.json", "a.wav")


@pytest.mark.parametrize("role", ["voice", "metadata"])
def test_missing_managed_files_are_repaired(tmp_path, fake_tts, monkeypatch, role):
    service = AppService(tmp_path)
    asset_id = register_asset(service)
    write_values(tmp_path / "voice.json")
    first = service.tts_synthesize("voice.json", "a.wav", asset_id)
    managed = Path(first["asset"][role]["local_path"])
    managed.unlink()
    monkeypatch.delenv("BREEZE_TTS_URL")
    recovered = service.tts_synthesize("voice.json", "a.wav", asset_id)
    assert recovered["reused"] is True
    assert managed.is_file()
    assert hashlib.sha256(managed.read_bytes()).hexdigest() == first["asset"][role]["sha256"]
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1


def test_corrupt_managed_file_is_rejected(tmp_path, fake_tts):
    service = AppService(tmp_path)
    asset_id = register_asset(service)
    write_values(tmp_path / "voice.json")
    first = service.tts_synthesize("voice.json", "a.wav", asset_id)
    Path(first["asset"]["voice"]["local_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        service.tts_synthesize("voice.json", "a.wav", asset_id)
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1


def test_interrupted_wav_publication_recovers_only_matching_request(
    tmp_path, fake_tts, monkeypatch
):
    import pipeline.service as service_module

    service = AppService(tmp_path)
    write_values(tmp_path / "voice.json")
    publish = service_module._publish_tts_file

    def fail_final(source, destination):
        if destination.name == "a.wav":
            raise OSError("interrupted before WAV publication")
        return publish(source, destination)

    monkeypatch.setattr(service_module, "_publish_tts_file", fail_final)
    with pytest.raises(OSError, match="interrupted"):
        service.tts_synthesize("voice.json", "a.wav")
    assert (tmp_path / "a.voice.json").is_file()
    assert (tmp_path / "a.voice.stage.wav").is_file()
    assert not (tmp_path / "a.wav").exists()
    write_values(tmp_path / "other.json", text="different request")
    monkeypatch.setattr(service_module, "_publish_tts_file", publish)
    with pytest.raises(FileExistsError, match="does not match"):
        service.tts_synthesize("other.json", "a.wav")
    monkeypatch.delenv("BREEZE_TTS_URL")
    recovered = service.tts_synthesize("voice.json", "a.wav")
    assert recovered["reused"] is True
    assert (tmp_path / "a.wav").is_file()
    assert not (tmp_path / "a.voice.stage.wav").exists()
    assert [request.url.path for request in fake_tts].count("/v1/audio/speech") == 1
