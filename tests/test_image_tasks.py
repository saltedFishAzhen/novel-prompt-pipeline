from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from pipeline.images import (
    ImageTaskPending,
    complete_image_task,
    get_image_task,
    list_image_tasks,
    prepare_image_task,
)
from pipeline.storage import RunLock, fingerprint, write_json


def image(path: Path, color: str, size=(8, 6)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def prepare(run: Path, refs=None, **changes):
    options = {
        "prompt": "Make a single coherent room.",
        "prompt_version": "release-123",
        "kind": "scene",
        "width": 1536,
        "height": 864,
        "references": refs or [],
        "context": {"mode": "generate", "purpose": "scene sheet"},
    }
    options.update(changes)
    return prepare_image_task(run, "scene_a", **options)


def pending(run: Path, refs=None, **changes):
    with pytest.raises(ImageTaskPending) as exc:
        prepare(run, refs, **changes)
    assert exc.value.task_id.startswith("scene_a_")
    return get_image_task(run, exc.value.task_id)


def test_request_freezes_ordered_reference_bytes_and_resumes_idempotently(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    first = image(tmp_path / "one.png", "red")
    second = image(tmp_path / "two.png", "blue")
    original = [first.read_bytes(), second.read_bytes()]
    refs = [
        {"path": str(first), "role": "face identity"},
        {"path": str(second), "role": "outfit"},
    ]
    task = pending(run, refs)
    assert task["request"]["mode"] == "generate"
    assert [ref["role"] for ref in task["request"]["references"]] == ["face identity", "outfit"]
    assert [Path(path).read_bytes() for path in task["reference_files"]] == original
    assert task["request"]["references"][0]["path"] == "references/01.png"
    assert Path(task["prompt_file"]).read_text("utf-8") == task["request"]["prompt"]
    assert [item["task_id"] for item in list_image_tasks(run)] == [task["task_id"]]

    # A source file changing after preparation cannot change the task snapshot.
    image(first, "green")
    assert (
        Path(get_image_task(run, task["task_id"])["reference_files"][0]).read_bytes() == original[0]
    )
    with pytest.raises(ValueError, match="inputs changed"):
        prepare(run, refs)
    first.write_bytes(original[0])

    candidate = image(tmp_path / "candidate.png", "yellow", (32, 24))
    completed = complete_image_task(
        run, task["task_id"], candidate, notes="Inspected composition", accepted=True
    )
    assert completed["status"] == "completed"
    assert completed["result"]["review_accepted"] is True
    assert (completed["result"]["width"], completed["result"]["height"]) == (32, 24)
    assert Path(completed["result_file"]).read_bytes() == candidate.read_bytes()
    assert prepare(run, refs) == [completed["result_file"]]
    assert (
        complete_image_task(run, task["task_id"], candidate, notes="Reviewed again", accepted=True)
        == completed
    )
    other = image(tmp_path / "other.png", "purple")
    with pytest.raises(ValueError, match="different result"):
        complete_image_task(run, task["task_id"], other, notes="Another candidate", accepted=True)
    assert get_image_task(run, task["task_id"]) == completed


def test_task_ids_are_safe_and_paths_cannot_escape(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ImageTaskPending) as exc:
        prepare_image_task(
            run,
            "../bad/path",
            prompt="A scene",
            prompt_version="v1",
            kind="scene",
            width=64,
            height=64,
        )
    task = get_image_task(run, exc.value.task_id)
    assert task["task_id"].startswith("image_")
    assert Path(task["task_directory"]).parent == run.resolve() / "image_tasks"
    for bad in ("../other", "C:\\outside", "/absolute", "", ".hidden"):
        with pytest.raises(ValueError, match="task ID"):
            get_image_task(run, bad)
    assert list_image_tasks(tmp_path) == []


@pytest.mark.parametrize("target", ["request", "prompt", "reference", "result"])
def test_tampering_is_detected_before_reuse_or_import(tmp_path, target):
    run = tmp_path / "run"
    run.mkdir()
    source = image(tmp_path / "source.png", "red")
    refs = [{"path": source, "role": "source scene"}]
    task = pending(run, refs)
    if target == "request":
        path = Path(task["task_directory"]) / "request.json"
        value = json.loads(path.read_text("utf-8"))
        value["prompt"] = "Tampered prompt"
        write_json(path, value)
    elif target == "prompt":
        Path(task["prompt_file"]).write_text("Tampered", encoding="utf-8")
    elif target == "reference":
        image(Path(task["reference_files"][0]), "blue")
    else:
        task = complete_image_task(
            run, task["task_id"], image(tmp_path / "out.png", "white"), notes="Seen", accepted=True
        )
        image(Path(task["result_file"]), "black")
    with pytest.raises(ValueError, match="changed|fingerprint|identity"):
        get_image_task(run, task["task_id"])
    with pytest.raises(ValueError, match="changed|fingerprint|identity"):
        prepare(run, refs)


def test_recomputed_manifest_cannot_redirect_references_outside_task(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    outside = image(tmp_path / "source.png", "red")
    task = pending(run, [{"path": outside, "role": "reference"}])
    path = Path(task["task_directory"]) / "request.json"
    request = json.loads(path.read_text("utf-8"))
    request["references"][0]["path"] = "../../source.png"
    content = {k: v for k, v in request.items() if k not in {"task_id", "task_sha256"}}
    forged_id = "scene_a_" + fingerprint(content)[:32]
    request["task_id"] = forged_id
    request["task_sha256"] = fingerprint({k: v for k, v in request.items() if k != "task_sha256"})
    write_json(path, request)
    original = Path(task["task_directory"])
    forged = original.parent / forged_id
    assert original.resolve().parent == forged.resolve().parent == (run / "image_tasks").resolve()
    original.rename(forged)
    with pytest.raises(ValueError, match="inside|escape"):
        get_image_task(run, forged_id)


def test_recomputed_request_and_prompt_are_rejected_by_content_addressed_id(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    directory = Path(task["task_directory"])
    path = directory / "request.json"
    request = json.loads(path.read_text("utf-8"))
    request["prompt"] = "Substituted prompt"
    request["prompt_sha256"] = hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest()
    request["task_sha256"] = fingerprint({k: v for k, v in request.items() if k != "task_sha256"})
    (directory / "prompt.txt").write_text(request["prompt"], encoding="utf-8")
    write_json(path, request)
    with pytest.raises(ValueError, match="identity"):
        get_image_task(run, task["task_id"])
    with pytest.raises(ValueError, match="identity"):
        complete_image_task(
            run, task["task_id"], image(tmp_path / "out.png", "red"), notes="Seen", accepted=True
        )


def test_rejects_invalid_inputs_and_never_accepts_missing_notes(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    source = image(tmp_path / "source.png", "red")
    with pytest.raises(ValueError, match="expected hash"):
        prepare(run, [{"path": source, "role": "reference", "sha256": "0" * 64}])
    with pytest.raises(ValueError, match="mode"):
        prepare(run, context={"mode": "edit"})
    task = pending(run)
    corrupt = tmp_path / "bad.png"
    corrupt.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="safely decoded"):
        complete_image_task(run, task["task_id"], corrupt, notes="Seen", accepted=True)
    with pytest.raises(ValueError, match="notes"):
        complete_image_task(run, task["task_id"], source, notes="  ", accepted=True)
    import pipeline.images.tasks as tasks

    monkeypatch.setattr(tasks, "_MAX_IMAGE_PIXELS", 10)
    with pytest.raises(ValueError, match="pixel limit"):
        complete_image_task(run, task["task_id"], source, notes="Seen", accepted=True)
    assert task["status"] == "pending"
    assert not (Path(task["task_directory"]) / "result.json").exists()


@pytest.mark.parametrize("accepted", [False, None, 1, "true"])
def test_result_requires_explicit_boolean_acceptance_without_mutation(tmp_path, accepted):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = image(tmp_path / "candidate.png", "purple")
    original = candidate.read_bytes()
    with pytest.raises(ValueError, match="explicit visual acceptance"):
        complete_image_task(run, task["task_id"], candidate, notes="Reviewed", accepted=accepted)
    assert candidate.read_bytes() == original
    assert get_image_task(run, task["task_id"])["status"] == "pending"
    assert not (Path(task["task_directory"]) / "results").exists()
    assert not (Path(task["task_directory"]) / "result.json").exists()


@pytest.mark.parametrize("accepted", [False, None, 1, "true"])
def test_result_without_recorded_acceptance_cannot_be_consumed(tmp_path, accepted):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = image(tmp_path / "candidate.png", "purple")
    complete_image_task(run, task["task_id"], candidate, notes="Reviewed", accepted=True)
    result_path = Path(task["task_directory"]) / "result.json"
    result = json.loads(result_path.read_text("utf-8"))
    result["review_accepted"] = accepted
    write_json(result_path, result)
    with pytest.raises(ValueError, match="result metadata"):
        prepare(run)


@pytest.mark.parametrize("target", ["tasks", "request", "reference", "result", "lock"])
def test_symlink_task_paths_are_rejected(tmp_path, target):
    run = tmp_path / "run"
    run.mkdir()
    source = image(tmp_path / "source.png", "red")
    if target == "tasks":
        outside = tmp_path / "outside"
        outside.mkdir()
        link = run / "image_tasks"
        target_path = outside
        is_dir = True
    else:
        task = pending(run, [{"path": source, "role": "reference"}])
        directory = Path(task["task_directory"])
        if target == "request":
            link = directory / "request.json"
            target_path = tmp_path / "metadata.json"
        elif target == "reference":
            link = Path(task["reference_files"][0])
            target_path = source
        elif target == "result":
            link = directory / "result.json"
            target_path = tmp_path / "metadata.json"
        else:
            link = directory.parent / ".scene_a.lock"
            target_path = tmp_path / "lock.txt"
        is_dir = False
        if not target_path.exists():
            target_path.write_text("{}", encoding="utf-8")
        link.unlink(missing_ok=True)
    try:
        link.symlink_to(target_path, target_is_directory=is_dir)
    except OSError as exc:
        pytest.skip(f"Host does not allow symbolic links: {exc}")
    with pytest.raises(ValueError, match="symbolic link"):
        if target in {"tasks", "lock"}:
            prepare(run)
        else:
            get_image_task(run, task["task_id"])


def test_result_hash_identifies_original_bytes(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = image(tmp_path / "candidate.jpg", "orange")
    result = complete_image_task(
        run, task["task_id"], candidate, notes="Checked actual image", accepted=True
    )
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert result["result"]["sha256"] == digest
    assert Path(result["result_file"]).name == digest + ".jpg"


def test_concurrent_preparation_creates_one_immutable_task(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    source = image(tmp_path / "source.png", "red")
    refs = [{"path": source, "role": "identity"}]

    def start():
        try:
            prepare(run, refs)
        except ImageTaskPending as exc:
            return exc.task_id
        pytest.fail("A request without a result must pause")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(lambda _: start(), range(2))
        assert first == second and first.startswith("scene_a_")
    assert len(list_image_tasks(run)) == 1
    task = get_image_task(run, first)
    assert Path(task["reference_files"][0]).read_bytes() == source.read_bytes()


def test_concurrent_different_requests_cannot_replace_the_same_logical_key(tmp_path):
    run = tmp_path / "run"
    run.mkdir()

    def start(prompt):
        try:
            prepare(run, prompt=prompt)
        except ImageTaskPending as exc:
            return "pending", exc.task_id
        except ValueError as exc:
            return "rejected", str(exc)
        pytest.fail("A request without a result must pause")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, ["One scene", "Different scene"]))
    assert sorted(status for status, _ in results) == ["pending", "rejected"]
    assert "inputs changed" in next(value for status, value in results if status == "rejected")
    assert len(list_image_tasks(run)) == 1


def test_completion_respects_run_lock_and_recovers_after_interrupted_metadata_write(
    tmp_path, monkeypatch
):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = image(tmp_path / "candidate.png", "blue")
    with RunLock(run), pytest.raises(RuntimeError, match="locked"):
        complete_image_task(run, task["task_id"], candidate, notes="Inspected", accepted=True)
    assert get_image_task(run, task["task_id"])["status"] == "pending"

    import pipeline.images.tasks as tasks

    original_write = tasks.write_json

    def interrupted(path, value):
        if path.name == "result.json":
            raise OSError("simulated interruption")
        return original_write(path, value)

    with monkeypatch.context() as scoped:
        scoped.setattr(tasks, "write_json", interrupted)
        with pytest.raises(OSError, match="interruption"):
            complete_image_task(run, task["task_id"], candidate, notes="Inspected", accepted=True)
    assert get_image_task(run, task["task_id"])["status"] == "pending"
    result = complete_image_task(run, task["task_id"], candidate, notes="Inspected", accepted=True)
    assert result["status"] == "completed"
    assert Path(result["result_file"]).read_bytes() == candidate.read_bytes()


def test_reviewed_main_region_validates_visible_dimensions_and_is_immutable(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = tmp_path / "rotated.jpg"
    photo = Image.new("RGB", (8, 6), "red")
    exif = Image.Exif()
    exif[274] = 6
    photo.save(candidate, exif=exif)
    original = candidate.read_bytes()
    for bounds in ([0, 0, 7, 8], [0, 0, 6, 9], [0, 0, 0, 1], [0, 0, 5.0, 8], [True, 0, 5, 8]):
        with pytest.raises(ValueError, match="Main region"):
            complete_image_task(
                run, task["task_id"], candidate, notes="Seen", accepted=True, main_region=bounds
            )
    done = complete_image_task(
        run, task["task_id"], candidate, notes="Seen", accepted=True, main_region=[1, 2, 6, 8]
    )
    assert done["result"]["main_region"] == {"bounds": [1, 2, 6, 8], "width": 6, "height": 8}
    assert Path(done["result_file"]).read_bytes() == original
    assert (
        complete_image_task(run, task["task_id"], candidate, notes="Again", accepted=True) == done
    )
    with pytest.raises(ValueError, match="different main region"):
        complete_image_task(
            run, task["task_id"], candidate, notes="Again", accepted=True, main_region=[0, 0, 6, 8]
        )
    path = Path(done["task_directory"]) / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["main_region"]["width"] = 7
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="main region"):
        get_image_task(run, task["task_id"])


def test_main_region_rejected_for_character(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    task = pending(run, kind="character")
    candidate = image(tmp_path / "character.png", "red")
    with pytest.raises(ValueError, match="scene and prop"):
        complete_image_task(
            run, task["task_id"], candidate, notes="Seen", accepted=True, main_region=[0, 0, 4, 4]
        )


def test_directory_publish_retries_permission_error(tmp_path, monkeypatch):
    import os

    import pipeline.storage as storage

    run = tmp_path / "run"
    run.mkdir()
    original = os.replace
    attempts = []

    def flaky(source, destination):
        if Path(source).name.startswith(".staging-"):
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError("sharing violation")
        original(source, destination)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    task = pending(run)
    assert len(attempts) == 2
    assert pending(run)["task_id"] == task["task_id"]
    assert len(attempts) == 2
    assert not list((run / "image_tasks").glob(".staging-*"))


def test_directory_publish_permanent_failure_cleans_staging(tmp_path, monkeypatch):
    import os

    import pipeline.storage as storage

    run = tmp_path / "run"
    run.mkdir()
    original = os.replace
    attempts = []

    def denied(source, destination):
        if Path(source).name.startswith(".staging-"):
            attempts.append((source, destination))
            raise PermissionError("sharing violation")
        original(source, destination)

    monkeypatch.setattr(storage.os, "replace", denied)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="sharing violation"):
        prepare(run)
    assert len(attempts) == 6
    assert list_image_tasks(run) == []
    assert not list((run / "image_tasks").glob(".staging-*"))


def test_result_publish_retries_permission_error(tmp_path, monkeypatch):
    import os

    import pipeline.storage as storage

    run = tmp_path / "run"
    run.mkdir()
    task = pending(run)
    candidate = image(tmp_path / "candidate.png", "white")
    original = os.replace
    attempts = []

    def flaky(source, destination):
        if Path(source).name.startswith(".image-"):
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError("sharing violation")
        original(source, destination)

    monkeypatch.setattr(storage.os, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    completed = complete_image_task(
        run, task["task_id"], candidate, notes="Reviewed", accepted=True
    )
    assert len(attempts) == 2
    assert completed["status"] == "completed"
    assert Path(completed["result_file"]).read_bytes() == candidate.read_bytes()
