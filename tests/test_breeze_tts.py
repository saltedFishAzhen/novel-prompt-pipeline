from __future__ import annotations

import hashlib
import json
import wave
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from tts import BreezeTTSClient, TTSConfig, TTSError

PCM = b"\x01\x02\x03\x04" * 120
HEADERS = {
    "Content-Type": "audio/pcm",
    "X-Sample-Rate": "24000",
    "X-Sample-Format": "s16le",
}


def reference_wav(path: Path) -> bytes:
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(24000)
        destination.writeframes(PCM)
    return path.read_bytes()


def parts_of(request: httpx.Request) -> dict[str, bytes]:
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    wrapper = (
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.read()
    )
    parsed = BytesParser(policy=policy.default).parsebytes(wrapper)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in parsed.iter_parts()
    }


@pytest.mark.parametrize("mode", ["design", "clone", "direction"])
def test_synthesis_uses_official_multipart_and_writes_playable_wav(tmp_path, mode):
    reference = tmp_path / "reference.wav"
    original_reference = reference_wav(reference)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/proxy/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer example-secret"
        return httpx.Response(200, headers=HEADERS, content=PCM)

    options = {"instruction": "Calm, clear voice"} if mode != "clone" else {}
    if mode != "design":
        options.update(reference_audio=reference, reference_text="完整准确的参考语音")
    target = tmp_path / f"{mode}.wav"
    with BreezeTTSClient(
        "https://example.invalid/proxy",
        api_key="example-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.synthesize("测试文本", target, cfg_scale=4, seed=123, **options)

    assert len(requests) == 1
    fields = parts_of(requests[0])
    assert fields["text"].decode() == "测试文本"
    assert fields["cfg_scale"] == b"4" and fields["seed"] == b"123"
    assert fields["instruction"].decode() == options.get("instruction", "")
    assert fields["ref_text"].decode() == options.get("reference_text", "")
    assert fields.get("ref_audio") == (original_reference if mode != "design" else None)
    with wave.open(str(target), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 24000)
        assert audio.readframes(audio.getnframes()) == PCM
    assert result["path"] == str(target.resolve())
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["mode"] == mode
    assert result["sample_rate"] == 24000 and result["channels"] == 1
    assert result["duration_seconds"] == len(PCM) / 48000


def test_config_uses_named_environment_without_serializing_credentials(monkeypatch):
    monkeypatch.setenv("BREEZE_TTS_URL", "https://example.invalid/private-prefix")
    monkeypatch.setenv("BREEZE_TTS_API_KEY", "realistic-secret-value")
    config = TTSConfig()
    assert "realistic-secret-value" not in config.model_dump_json()
    assert "private-prefix" not in config.model_dump_json()
    with BreezeTTSClient.from_config(
        config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok", "sample_rate": 24000})
        ),
    ) as client:
        assert client.health() == {"status": "ok", "sample_rate": 24000}
    monkeypatch.delenv("BREEZE_TTS_URL")
    with pytest.raises(ValueError, match="BREEZE_TTS_URL"):
        BreezeTTSClient.from_config(config)
    for values in (
        {"url_env": "http://bad"},
        {"api_key_env": ""},
        {"timeout": -1},
        {"max_audio_bytes": True},
        {"url": "secret"},
    ):
        with pytest.raises(ValidationError):
            TTSConfig.model_validate(values)


def test_health_and_model_metadata_filter_untrusted_extras():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"status": "ok", "sample_rate": 24000, "private": "secret"}
            )
        return httpx.Response(
            200,
            json={
                "model_digest": "a" * 64,
                "frame_rate": 12.5,
                "max_new_tokens": 1500,
                "max_batch_texts": 128,
                "url": "secret",
            },
        )

    with BreezeTTSClient(
        "https://example.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.health() == {"status": "ok", "sample_rate": 24000}
        result = client.model_info()
        assert result == {
            "model_digest": "a" * 64,
            "frame_rate": 12.5,
            "max_new_tokens": 1500,
            "max_batch_texts": 128,
        }
        assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [400, 409, 503])
def test_errors_are_sanitized_and_posts_are_not_retried(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "api-secret https://private.invalid"})

    target = tmp_path / "output.wav"
    with BreezeTTSClient(
        "https://private.invalid", api_key="api-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TTSError) as caught:
            client.synthesize("hello", target, instruction="warm voice")
    assert caught.value.status == status
    assert caught.value.retryable is (status in {409, 503})
    assert "api-secret" not in str(caught.value)
    assert "private.invalid" not in str(caught.value)
    assert len(calls) == 1
    assert not target.exists() and not list(tmp_path.glob(".tts-*.wav"))


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, PCM),
        ({**HEADERS, "X-Sample-Rate": "48000"}, PCM),
        ({**HEADERS, "X-Sample-Format": "f32le"}, PCM),
        ({**HEADERS, "Content-Type": "audio/wav"}, PCM),
        (HEADERS, b""),
        (HEADERS, b"\x00"),
    ],
)
def test_invalid_server_audio_never_creates_result(tmp_path, headers, body):
    output = tmp_path / "output.wav"
    with BreezeTTSClient(
        "https://example.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, content=body)
        ),
    ) as client:
        with pytest.raises(TTSError):
            client.synthesize("hello", output, instruction="warm voice")
    assert not output.exists() and not list(tmp_path.glob(".tts-*.wav"))


def test_oversized_stream_and_network_interruption_leave_no_partial_output(tmp_path):
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"\x00\x00" * 10
            raise httpx.ReadError("api-secret from https://private.invalid")

    for index, response in enumerate(
        (
            httpx.Response(200, headers=HEADERS, content=PCM),
            httpx.Response(200, headers=HEADERS, stream=InterruptedStream()),
        )
    ):
        output = tmp_path / f"{index}.wav"
        with BreezeTTSClient(
            "https://private.invalid",
            api_key="api-secret",
            max_audio_bytes=64,
            transport=httpx.MockTransport(lambda request: response),
        ) as client:
            with pytest.raises(TTSError) as caught:
                client.synthesize("hello", output, instruction="warm voice")
        assert "api-secret" not in str(caught.value)
        assert "private.invalid" not in str(caught.value)
        assert not output.exists() and not list(tmp_path.glob(".tts-*.wav"))


def test_validate_inputs_and_refuse_overwriting_before_network_call(tmp_path):
    target = tmp_path / "existing.wav"
    target.write_bytes(b"keep this original")
    reference = tmp_path / "valid.wav"
    reference_wav(reference)
    calls = []
    client = BreezeTTSClient(
        "https://example.invalid",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    with client:
        with pytest.raises(FileExistsError):
            client.synthesize("hello", target, instruction="warm")
        with pytest.raises(ValueError, match="together"):
            client.synthesize("hello", tmp_path / "one.wav", reference_audio=reference)
        with pytest.raises(ValueError, match="together"):
            client.synthesize("hello", tmp_path / "two.wav", reference_text="hello")
        with pytest.raises(ValueError, match="instruction"):
            client.synthesize("hello", tmp_path / "three.wav")
        with pytest.raises(ValueError, match="seed"):
            client.synthesize("hello", tmp_path / "four.wav", instruction="warm", seed=2**32)
        with pytest.raises(ValueError, match="cfg_scale"):
            client.synthesize(
                "hello", tmp_path / "five.wav", instruction="warm", cfg_scale=float("nan")
            )
        with pytest.raises(ValueError, match=".wav"):
            client.synthesize("hello", tmp_path / "six.mp3", instruction="warm")
    assert not calls and target.read_bytes() == b"keep this original"


def test_corrupt_or_truncated_reference_is_rejected(tmp_path):
    reference = tmp_path / "reference.wav"
    good = reference_wav(reference)
    client = BreezeTTSClient(
        "https://example.invalid",
        transport=httpx.MockTransport(lambda request: pytest.fail("no POST")),
    )
    with client:
        for raw in (b"not wave", good[:-8]):
            reference.write_bytes(raw)
            with pytest.raises(ValueError, match="WAV"):
                client.synthesize(
                    "hello", tmp_path / "out.wav", reference_audio=reference, reference_text="hello"
                )


def test_reference_and_output_parent_symlinks_are_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not allow symbolic links")
    reference_wav(actual / "reference.wav")
    client = BreezeTTSClient(
        "https://example.invalid",
        transport=httpx.MockTransport(lambda request: pytest.fail("no POST")),
    )
    with client:
        with pytest.raises(ValueError, match="symbolic links"):
            client.synthesize(
                "hello",
                tmp_path / "out.wav",
                reference_audio=link / "reference.wav",
                reference_text="hello",
            )
        with pytest.raises(ValueError, match="symbolic links"):
            client.synthesize("hello", link / "out.wav", instruction="warm")


def test_output_created_during_request_is_not_overwritten(tmp_path):
    output = tmp_path / "race.wav"

    def handler(request):
        output.write_bytes(b"another producer's data")
        return httpx.Response(200, headers=HEADERS, content=PCM)

    with BreezeTTSClient(
        "https://example.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(FileExistsError):
            client.synthesize("hello", output, instruction="warm voice")
    assert output.read_bytes() == b"another producer's data"
    assert not list(tmp_path.glob(".tts-*.wav"))


def test_health_loading_and_untrusted_model_identity_are_safe():
    loading = httpx.MockTransport(lambda request: httpx.Response(503, json={"status": "loading"}))
    with BreezeTTSClient("https://example.invalid", transport=loading) as client:
        with pytest.raises(TTSError) as caught:
            client.health()
    assert caught.value.status == 503 and caught.value.retryable
    invalid = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"model_digest": "not-a-sha256", "url": "secret"})
    )
    with BreezeTTSClient("https://example.invalid", transport=invalid) as client:
        with pytest.raises(TTSError) as caught:
            client.model_info()
    assert "secret" not in str(caught.value)


def test_invalid_service_url_never_exposes_its_content():
    for url in ("https://bad:secret", "http://[private-secret", "https://example.invalid/\nsecret"):
        with pytest.raises(ValueError) as caught:
            BreezeTTSClient(url)
        assert "secret" not in str(caught.value)
