import os

import pytest

from pipeline.storage import write_json


def test_write_json_retries_transient_permission_errors(tmp_path, monkeypatch):
    import pipeline.storage as storage

    destination = tmp_path / "state.json"
    destination.write_text('{"old": true}', encoding="utf-8")
    replace = os.replace
    calls = []
    sleeps = []

    def flaky(source, target):
        calls.append((source, target))
        if len(calls) <= 2:
            raise PermissionError("sharing violation")
        replace(source, target)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)
    write_json(destination, {"new": True})
    assert len(calls) == 3
    assert sleeps == [0.05, 0.1]
    assert '"new": true' in destination.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_write_json_failure_preserves_destination_and_cleans_temp(tmp_path, monkeypatch):
    import pipeline.storage as storage

    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")
    calls = []

    def denied(source, target):
        calls.append((source, target))
        raise PermissionError("sharing violation")

    monkeypatch.setattr(storage.os, "replace", denied)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="sharing violation"):
        write_json(destination, {"new": True})
    assert len(calls) == 6
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))


def test_write_json_does_not_retry_other_os_errors(tmp_path, monkeypatch):
    import pipeline.storage as storage

    calls = []

    def failed(source, target):
        calls.append((source, target))
        raise OSError("other error")

    monkeypatch.setattr(storage.os, "replace", failed)
    monkeypatch.setattr(storage.time, "sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(OSError, match="other error"):
        write_json(tmp_path / "state.json", {})
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.tmp"))
