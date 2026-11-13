from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from comfyui_client import BindingError, WorkflowTemplate
from pipeline.config import Config
from pipeline.engine import Pipeline
from pipeline.library import AssetLibrary
from pipeline.scene_views import _compose_grid
from pipeline.styles import StyleConfig, StylePreset
from prompt_project import PromptRelease


class Model:
    def __init__(self):
        self.calls = []

    def complete(self, prompt, *, correction=""):
        view = ("reverse", "overview", "topdown")[len(self.calls)]
        self.calls.append(prompt)
        return view.upper() + " VIEW"


class Comfy:
    def __init__(self, *, fail_reverse_once=False):
        self.jobs = {}
        self.uploads = {}
        self.fail_reverse_once = fail_reverse_once

    def upload_image(self, path):
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        remote = digest + ".png"
        self.uploads[remote] = str(path)
        return remote

    def submit(self, graph):
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = graph
        return job_id

    def wait(self, job_id, timeout=None):
        text = self.jobs[job_id].get("37", {}).get("inputs", {}).get("prompt", "")
        if self.fail_reverse_once and "REVERSE VIEW" in text:
            self.fail_reverse_once = False
            raise TimeoutError("wait interrupted")
        return {"job_id": job_id}

    def download_outputs(self, history, directory):
        graph = self.jobs[history["job_id"]]
        text = graph.get("37", {}).get("inputs", {}).get("prompt", "")
        colors = {
            "MASTER ROOM": (255, 0, 0),
            "REVERSE VIEW": (0, 255, 0),
            "OVERVIEW VIEW": (0, 0, 255),
            "TOPDOWN VIEW": (255, 255, 0),
        }
        color = next((value for key, value in colors.items() if key in text), (128, 128, 128))
        path = Path(directory) / "image.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), color).save(path)
        return [str(path)]


def setup_pipeline(tmp_path, *, comfy=None, style=None, library=None):
    config = Config(
        asset_library=str(tmp_path / "assets.sqlite3"),
        style=style or StyleConfig(),
    )
    client = comfy or Comfy()
    llm = Model()
    pipeline = Pipeline(config, llm=llm, comfy=client)
    pipeline.root = tmp_path / "run"
    pipeline.root.mkdir(exist_ok=True)
    pipeline.state = {"nodes": {}, "media": {}}
    pipeline.release = PromptRelease.load(config.prompt_project)
    pipeline.library = library or AssetLibrary(config.asset_library)
    asset = pipeline.library.register(
        [
            {
                "asset_id": "scene_a",
                "kind": "scene",
                "name": "room",
                "aliases": "room",
                "description": "MASTER ROOM",
            }
        ]
    )[0]
    return pipeline, asset, client, llm


def test_four_separate_views_references_and_local_grid(tmp_path):
    style = StyleConfig(
        preset="custom",
        presets={"custom": StylePreset(prefix="PREFIX", suffix="SUFFIX", negative="BLUR")},
    )
    pipeline, asset, comfy, model = setup_pipeline(tmp_path, style=style)
    record = pipeline.generate_scene_asset(asset)
    assert len(comfy.jobs) == 4 and len(model.calls) == 3
    mapping = pipeline.state["scene_views"]["scene_a"]
    assert mapping["views"]["topdown"]["references"] == ["master", "reverse", "overview"]
    paths = {name: Path(mapping["views"][name]["file"]["local_path"]) for name in mapping["views"]}
    assert all(path.is_file() for path in paths.values())
    remote = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() + ".png" for name, path in paths.items()
    }
    graphs = list(comfy.jobs.values())
    assert all(graph["37"]["inputs"]["negative_prompt"] == "BLUR" for graph in graphs)
    assert all(graph["37"]["inputs"]["prompt"].startswith("PREFIX\n\n") for graph in graphs)
    assert graphs[1]["44"]["inputs"]["image"] == remote["master"]
    assert graphs[2]["44"]["inputs"]["image"] == remote["master"]
    assert [graphs[3][str(44 + index)]["inputs"]["image"] for index in range(3)] == [
        remote["master"],
        remote["reverse"],
        remote["overview"],
    ]
    with Image.open(record["local_path"]) as grid:
        assert grid.mode == "RGB" and grid.size == (16, 16)
        assert [grid.getpixel(point) for point in ((0, 0), (8, 0), (0, 8), (8, 8))] == [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
        ]
    assert len(pipeline.library.files("scene_a")) == 5
    # A new run reuses a complete group and carries its four-file mapping.
    second, same_asset, second_comfy, _ = setup_pipeline(
        tmp_path, style=style, library=pipeline.library
    )
    second.root = tmp_path / "second"
    second.root.mkdir()
    cached = second.generate_scene_asset(same_asset)
    assert cached["sha256"] == record["sha256"] and not second_comfy.jobs
    assert second.state["scene_views"]["scene_a"]["group"] == mapping["group"]
    pipeline.close()


@pytest.mark.parametrize("initial_reference", [True, False])
def test_resume_preserves_job_ids_and_original_reference(tmp_path, initial_reference):
    pipeline, asset, comfy, model = setup_pipeline(tmp_path, comfy=Comfy(fail_reverse_once=True))
    reference = tmp_path / "reference.png"
    if initial_reference:
        Image.new("RGB", (8, 8), "white").save(reference)
        pipeline.library.link_file("scene_a", reference)
    with pytest.raises(TimeoutError, match="interrupted"):
        pipeline.generate_scene_asset(asset)
    assert len(comfy.jobs) == 2
    first_reference = pipeline.state["asset_generation_refs"]["scene_a"]
    Image.new("RGB", (8, 8), "black").save(reference)
    pipeline.library.link_file("scene_a", reference)
    pipeline.generate_scene_asset(asset)
    assert len(comfy.jobs) == 4 and len(model.calls) == 3
    assert pipeline.state["asset_generation_refs"]["scene_a"] == first_reference
    assert pipeline.state["scene_views"]["scene_a"]["identity_reference"] == first_reference
    pipeline.close()


def test_fewer_than_three_reference_slots_fails_before_any_post(tmp_path):
    pipeline, asset, comfy, model = setup_pipeline(tmp_path)
    original = pipeline.image_template
    profile = original.profile
    profile["reference_slots"] = profile["reference_slots"][:2]
    pipeline.image_template = WorkflowTemplate(original.graph, profile, name="limited")
    with pytest.raises(ValueError, match="3 reference slots"):
        pipeline.generate_scene_asset(asset)
    assert not comfy.jobs and not model.calls
    pipeline.close()


def test_broken_declared_reference_slot_fails_before_any_post(tmp_path):
    pipeline, asset, comfy, model = setup_pipeline(tmp_path)
    original = pipeline.image_template
    profile = original.profile
    profile["reference_slots"][2]["load"]["node_id"] = "missing"
    pipeline.image_template = WorkflowTemplate(original.graph, profile, name="broken")
    with pytest.raises(BindingError, match="missing"):
        pipeline.generate_scene_asset(asset)
    assert not comfy.jobs and not model.calls
    pipeline.close()


def test_pipeline_checks_scene_slots_before_unrelated_asset_jobs(tmp_path):
    from test_styles import _panels

    pipeline, asset, comfy, model = setup_pipeline(tmp_path)
    original = pipeline.image_template
    profile = original.profile
    profile["reference_slots"] = profile["reference_slots"][:2]
    pipeline.image_template = WorkflowTemplate(original.graph, profile, name="limited")
    panel = _panels()[0]
    panel["asset_ids"] = ["character_a", "scene_a"]
    panel["first_frame_asset_ids"] = ["character_a", "scene_a"]
    pipeline.state["panels"] = [panel]
    pipeline.state["assets"] = [
        {"asset_id": "character_a", "kind": "character", "description": "person"},
        asset,
    ]
    with pytest.raises(ValueError, match="3 reference slots"):
        pipeline._render()
    assert not comfy.jobs and not model.calls
    pipeline.close()


def test_incomplete_node_release_fails_before_post(tmp_path):
    pipeline, asset, comfy, model = setup_pipeline(tmp_path)
    pipeline.release.manifest["nodes"].pop("scene_topdown")
    with pytest.raises(ValueError, match="all three view nodes"):
        pipeline.generate_scene_asset(asset)
    assert not comfy.jobs and not model.calls
    pipeline.close()


def test_pipeline_render_uses_scene_composite_as_asset_reference(tmp_path):
    from test_styles import _panels

    pipeline, asset, comfy, model = setup_pipeline(tmp_path)
    panel = _panels()[0]
    panel["asset_ids"] = ["scene_a"]
    panel["first_frame_asset_ids"] = ["scene_a"]
    pipeline.state["panels"] = [panel]
    pipeline.state["assets"] = [asset]
    pipeline._render()
    assert len(comfy.jobs) == 6 and len(model.calls) == 3
    mapping = pipeline.state["scene_views"]["scene_a"]
    grid = mapping["grid"]
    assert pipeline.state["asset_media"]["scene_a"]["sha256"] == grid["sha256"]
    frame = list(comfy.jobs.values())[4]
    assert frame["44"]["inputs"]["image"] == grid["sha256"] + ".png"
    pipeline.close()


def test_grid_rejects_escaped_directory_before_creating_it(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    target = root / "media" / "scene_asset"
    outside = tmp_path / "outside" / "scene_asset"
    original_resolve = Path.resolve
    original_mkdir = Path.mkdir
    attempted = []

    def resolve(path, *args, **kwargs):
        return outside if path == target else original_resolve(path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        if path == target:
            attempted.append(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(ValueError, match="remain inside the run"):
        _compose_grid(SimpleNamespace(root=root), "asset", {})
    assert not attempted
    assert not outside.exists()


def test_grid_rechecks_directory_after_creation(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    target = root / "media" / "scene_asset"
    outside = tmp_path / "outside" / "scene_asset"
    original_resolve = Path.resolve
    calls = []

    def resolve(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            if len(calls) > 1:
                return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="remain inside the run"):
        _compose_grid(SimpleNamespace(root=root), "asset", {})
    assert len(calls) == 2
    assert not outside.exists()
