import json

import pytest

from pipeline.shuohao.tasks import (
    TextTaskPending,
    complete_text_task,
    get_text_task,
    list_text_tasks,
    prepare_text_task,
)
from pipeline.storage import RunLock


def pending(run, prompt="request"):
    with pytest.raises(TextTaskPending) as exc:
        prepare_text_task(run, stage="story", prompt=prompt, episode_range=[1, 2])
    return exc.value.task_id


def test_submission_and_immutable_tasks(tmp_path):
    task_id = pending(tmp_path)
    assert pending(tmp_path) == task_id
    other_id = pending(tmp_path, "corrected")
    assert other_id != task_id
    assert len(list_text_tasks(tmp_path)) == 2
    source = tmp_path / "candidate.json"
    source.write_bytes(b'{"text": "hello"}\n')
    result = complete_text_task(tmp_path, task_id, source, agent_id="agent-1")
    assert result["status"] == "submitted"
    assert result["response"]["agent_id"] == "agent-1"
    assert complete_text_task(tmp_path, task_id, source, agent_id="another") == result
    assert prepare_text_task(tmp_path, stage="story", prompt="request", episode_range=[1, 2]) == (
        '{"text": "hello"}\n'
    )
    source.write_text('{"text": "different"}', encoding="utf-8")
    with pytest.raises(ValueError, match="different response"):
        complete_text_task(tmp_path, task_id, source, agent_id="agent-1")


def test_input_request_and_response_tamper(tmp_path):
    task_id = pending(tmp_path)
    directory = tmp_path / "text_tasks" / task_id
    input_file = directory / "input.txt"
    input_file.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        get_text_task(tmp_path, task_id)
    input_file.write_text("request", encoding="utf-8")
    request_file = directory / "request.json"
    request = json.loads(request_file.read_text(encoding="utf-8"))
    request["stage"] = "other"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError):
        list_text_tasks(tmp_path)
    request["stage"] = "story"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    source = tmp_path / "candidate.json"
    source.write_text("{}", encoding="utf-8")
    complete_text_task(tmp_path, task_id, source, agent_id="agent")
    (directory / "output.json").write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        get_text_task(tmp_path, task_id)
    with pytest.raises(ValueError):
        prepare_text_task(tmp_path, stage="story", prompt="request", episode_range=[1, 2])


def test_invalid_inputs_and_lock(tmp_path):
    task_id = pending(tmp_path)
    source = tmp_path / "candidate.json"
    for text in ("[1]", "broken", '{"x": NaN}'):
        source.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            complete_text_task(tmp_path, task_id, source, agent_id="agent")
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="agent ID"):
        complete_text_task(tmp_path, task_id, source, agent_id="  ")
    with pytest.raises(ValueError):
        get_text_task(tmp_path, "../escape")
    with RunLock(tmp_path), pytest.raises(RuntimeError, match="locked"):
        complete_text_task(tmp_path, task_id, source, agent_id="agent")


def test_conflicting_task_directory_and_link(tmp_path):
    task_id = pending(tmp_path)
    directory = tmp_path / "text_tasks" / task_id
    (directory / "request.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        pending(tmp_path)
    source = tmp_path / "outside"
    source.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable")
    with pytest.raises(ValueError):
        list_text_tasks(link)


def test_size_limit(tmp_path):
    task_id = pending(tmp_path)
    source = tmp_path / "large.json"
    source.write_bytes(b" " * (20 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        complete_text_task(tmp_path, task_id, source, agent_id="agent")


def test_task_publish_retries_and_reuses_task(tmp_path, monkeypatch):
    import os

    import pipeline.storage as storage

    rename = os.rename
    calls = []

    def flaky(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            raise PermissionError("sharing violation")
        rename(source, destination)

    monkeypatch.setattr(storage.os, "rename", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    task_id = pending(tmp_path)
    assert len(calls) == 2
    assert pending(tmp_path) == task_id
    assert len(calls) == 2
    assert len(list_text_tasks(tmp_path)) == 1
    assert not list((tmp_path / "text_tasks").glob(".staging-*"))


def test_task_publish_failure_cleans_staging(tmp_path, monkeypatch):
    import pipeline.storage as storage

    calls = []

    def denied(source, destination):
        calls.append((source, destination))
        raise PermissionError("sharing violation")

    monkeypatch.setattr(storage.os, "rename", denied)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="sharing violation"):
        pending(tmp_path)
    assert len(calls) == 6
    assert not list((tmp_path / "text_tasks").iterdir())
