from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfyui_client import BindingError, WorkflowTemplate, get_default_template
from pipeline.config import Config
from pipeline.engine import Pipeline
from pipeline.library import AssetLibrary
from pipeline.styles import BUILTIN_STYLES, StyleConfig, StylePreset


def selected_style():
    return StyleConfig(
        preset="custom",
        presets={
            "custom": StylePreset(prefix="VISUAL PREFIX", suffix="VISUAL SUFFIX", negative="BLUR")
        },
    )


def test_qwen_negative_is_bound_to_the_sampler_negative_conditioning():
    for profile in ("qwen", "qwen-unlock"):
        graph = get_default_template(profile).prepare("subject", negative_prompt="BLUR")
        assert graph["37"]["inputs"]["negative_prompt"] == "BLUR"
        assert graph["23"]["inputs"]["negative"] == ["37", 1]


@pytest.mark.parametrize("profile", ["i2va", "ref2va"])
def test_current_h3_workflows_reject_unsupported_negative(profile):
    with pytest.raises(BindingError, match="negative_prompt"):
        get_default_template(profile).prepare(
            "video", first_frame="first.png", negative_prompt="BLUR"
        )


def test_style_overrides_and_defaults():
    config = StyleConfig()
    assert config.preset == "cinematic_anime_cg"
    assert config.compose_image("original") == "\n\n".join(
        (config.resolved()["prefix"], "original", config.resolved()["suffix"])
    )
    assert Config().public_dict()["style"]["preset"] == "cinematic_anime_cg"
    assert set(BUILTIN_STYLES) == {"cinematic_anime_cg"}
    with pytest.raises(ValueError, match="Unknown visual style preset"):
        StyleConfig(preset="anime")
    style = selected_style()
    assert style.resolved()["image_negative"] == "BLUR"
    assert style.compose_image("original") == "VISUAL PREFIX\n\noriginal\n\nVISUAL SUFFIX"
    style.image_negative = "NOISE"
    assert style.resolved()["image_negative"] == "NOISE"
    with pytest.raises(ValueError, match="video_negative"):
        StyleConfig.model_validate({"video_negative": "must not be ignored"})


def test_project_default_style_is_consistent_across_config_and_sdk():
    example = Path(__file__).resolve().parents[1] / "configs" / "pipeline.example.json"
    config = Config.load(example)
    assert config.style.preset == "cinematic_anime_cg"
    assert StyleConfig().preset == Config.load().style.preset == "cinematic_anime_cg"
    style = config.style.resolved()
    assert "3D" in style["prefix"] and "anime" in style["prefix"]
    assert "age" in style["suffix"] and "light sources" in style["suffix"]
    assert "flat cel shading" in style["image_negative"]
    assert all(word not in style["prefix"].lower() for word in ("sunset", "school", "young"))
    graph = get_default_template("qwen").prepare(
        config.style.compose_image("A room."), negative_prompt=style["image_negative"]
    )
    assert graph["37"]["inputs"]["negative_prompt"] == style["image_negative"]


def test_project_default_style_can_be_customized():
    style = StyleConfig.model_validate(
        {
            "preset": "cinematic_anime_cg",
            "presets": {"cinematic_anime_cg": {"prefix": "My style."}},
        }
    )
    assert style.compose_image("An object.") == "My style.\n\nAn object."


def test_cinematic_default_preset_targets_reference_face_structure_and_grade():
    style = StyleConfig(preset="cinematic_anime_cg").resolved()
    prefix = style["prefix"].lower()
    suffix = style["suffix"].lower()
    negative = style["image_negative"].lower()
    assert "oval facial plane" in prefix and "upper lid fold" in prefix
    assert "knit stitches" in prefix
    assert "filmic" in suffix and "halation" in suffix
    assert "milky lifted blacks" in negative
    for word in ("oversized anime eyes", "extreme v-shaped chin", "inflated lips"):
        assert word not in negative
    for word in ("sunset", "school", "young", "golden hour"):
        assert word not in prefix
    assert (
        StyleConfig(preset="cinematic_anime_cg")
        .compose_image("A room.")
        .startswith("High-end anime-derived 3D game cinematic character render.")
    )


def _panels():
    i2va = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> "
        "(from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description:\n[Shot 1] A key turns.\n\n"
        "overall_soundscape:\nA click.\n\nnon_diegetic_music:\nN/A\n时长：6秒"
    )
    ref = (
        "subject_definitions:\n<Picture 1> is a key.\n\nsummary:\nA key.\n\n"
        "retention_analysis:\nKeep the key.\n\ndetailed_description:\n"
        "[Shot 1] A key turns.\n\noverall_soundscape:\nA click.\n\n"
        "non_diegetic_music:\nN/A\n时长：6秒"
    )
    return [
        {
            "panel_index": index,
            "generation_mode": mode,
            "first_frame_asset_ids": ["prop_a"],
            "asset_ids": ["prop_a"],
            "prompt": "FRAME BODY",
            "video_prompt": video,
            "duration_seconds": 6,
        }
        for index, (mode, video) in enumerate((("i2va", i2va), ("ref2va", ref)))
    ]


def test_style_reaches_all_media_without_mutating_inference_output(tmp_path, monkeypatch):
    config = Config(style=selected_style())
    pipeline = Pipeline(config)
    panels = _panels()
    original = json.loads(json.dumps(panels))
    pipeline.state = {
        "panels": panels,
        "assets": [{"asset_id": "prop_a", "description": "ASSET BODY"}],
        "media": {},
    }
    captured = {}

    class Library:
        def files(self, *args, **kwargs):
            return []

        def link_file(self, *args, **kwargs):
            return {"local_path": str(tmp_path / "asset.png")}

    pipeline.library = Library()
    monkeypatch.setattr(pipeline, "_save", lambda: None)
    monkeypatch.setattr(pipeline, "_upload", lambda path, **kwargs: "uploads/asset.png")

    def job(key, graph):
        captured[key] = graph
        return [str(tmp_path / "image.png")]

    monkeypatch.setattr(pipeline, "_job", job)
    pipeline._render()
    for key in ("asset_prop_a", "panel_0_image", "panel_1_image"):
        values = captured[key]["37"]["inputs"]
        assert values["prompt"].startswith("VISUAL PREFIX\n\n")
        assert values["prompt"].endswith("\n\nVISUAL SUFFIX")
        assert values["negative_prompt"] == "BLUR"
    first = captured["panel_0_video"]["139"]["inputs"]["prompt"]
    second = captured["panel_1_video"]["28"]["inputs"]["prompt"]
    assert first.startswith("For the target video, at 0.00 seconds")
    for value, header in (
        (first, "integrated_multimodal_description"),
        (second, "detailed_description"),
    ):
        visual = value.split(header + ":", 1)[1].split("overall_soundscape:", 1)[0]
        assert "VISUAL PREFIX" in visual and "VISUAL SUFFIX" in visual
        assert "A click." in value.split("overall_soundscape:", 1)[1]
    assert captured["panel_0_video"]["903"]["inputs"]["prompt"] == first
    assert panels == original
    assert pipeline.state["assets"][0]["description"] == "ASSET BODY"


def test_style_cannot_inject_h3_sections_before_any_submission(monkeypatch):
    style = StyleConfig(
        preset="broken",
        presets={"broken": StylePreset(prefix="overall_soundscape:\ninjected")},
    )
    pipeline = Pipeline(Config(style=style))
    pipeline.state = {"panels": _panels(), "assets": []}
    monkeypatch.setattr(pipeline, "_job", lambda *args: pytest.fail("must not submit"))
    monkeypatch.setattr(pipeline, "_upload", lambda *args, **kwargs: pytest.fail("must not upload"))
    with pytest.raises(ValueError, match="sections"):
        pipeline._render()


def test_explicit_empty_image_negative_clears_custom_graph_default():
    original = get_default_template("qwen")
    graph = original.graph
    graph["37"]["inputs"]["negative_prompt"] = "DEFAULT NEGATIVE"
    template = WorkflowTemplate(graph, original.profile, name="custom")
    pipeline = Pipeline(Config(style=StyleConfig(image_negative="")))
    arguments = pipeline._style_negative_arguments(template, "image")
    assert template.prepare("subject", **arguments)["37"]["inputs"]["negative_prompt"] == ""


def test_asset_images_are_cached_by_style_and_identity_reference(tmp_path, monkeypatch):
    library = AssetLibrary(tmp_path / "assets.sqlite3")
    asset = library.register(
        [
            {
                "asset_id": "prop_a",
                "kind": "prop",
                "name": "key",
                "aliases": "key",
                "description": "key",
            }
        ]
    )[0]
    source = tmp_path / "identity.png"
    source.write_bytes(b"first reference")
    library.link_file("prop_a", source)
    asset_jobs = []
    uploaded = []

    def run(style_name):
        style = StyleConfig(presets={"cinematic_anime_cg": StylePreset(prefix=style_name)})
        pipeline = Pipeline(Config(style=style))
        pipeline.library = library
        pipeline.state = {"panels": _panels()[:1], "assets": [asset], "media": {}}
        monkeypatch.setattr(pipeline, "_save", lambda: None)

        def upload(path, **kwargs):
            uploaded.append(Path(path).read_bytes())
            return "uploads/asset.png"

        def job(key, graph):
            path = tmp_path / f"{style_name}-{len(asset_jobs)}.png"
            if key.startswith("asset_"):
                asset_jobs.append((style_name, graph))
            path.write_bytes((key + style_name).encode())
            return [str(path)]

        monkeypatch.setattr(pipeline, "_upload", upload)
        monkeypatch.setattr(pipeline, "_job", job)
        pipeline._render()

    run("revision-one")
    run("revision-two")
    run("revision-one")
    assert [item[0] for item in asset_jobs] == ["revision-one", "revision-two"]
    assert all(item[1]["37"]["inputs"]["images.image_1"] == ["44", 0] for item in asset_jobs)
    assert uploaded.count(b"first reference") == 2
    source.write_bytes(b"new reference")
    library.link_file("prop_a", source)
    run("revision-one")
    assert [item[0] for item in asset_jobs] == ["revision-one", "revision-two", "revision-one"]
    assert b"new reference" in uploaded
    library.close()


@pytest.mark.parametrize("initial_reference", [True, False])
def test_asset_guidance_is_pinned_across_interrupted_generation(
    tmp_path, monkeypatch, initial_reference
):
    library = AssetLibrary(tmp_path / "assets.sqlite3")
    asset = library.register(
        [
            {
                "asset_id": "prop_a",
                "kind": "prop",
                "name": "key",
                "aliases": "key",
                "description": "key",
            }
        ]
    )[0]
    source = tmp_path / "identity.png"
    if initial_reference:
        source.write_bytes(b"first")
        library.link_file("prop_a", source)
    pipeline = Pipeline(Config())
    pipeline.library = library
    pipeline.state = {"panels": _panels()[:1], "assets": [asset], "media": {}}
    monkeypatch.setattr(pipeline, "_save", lambda: None)
    monkeypatch.setattr(
        pipeline, "_upload", lambda path, **kwargs: pipeline._file_hash(path) + ".png"
    )
    first_attempt = []

    def interrupted(key, graph):
        first_attempt.append(graph)
        raise RuntimeError("simulated interrupted submission")

    monkeypatch.setattr(pipeline, "_job", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        pipeline._render()
    pinned = pipeline.state["asset_generation_refs"]["prop_a"]
    assert bool(pinned) is initial_reference
    source.write_bytes(b"later")
    library.link_file("prop_a", source)
    retry_attempt = []

    def completed(key, graph):
        if key.startswith("asset_"):
            retry_attempt.append(graph)
        path = tmp_path / f"{key}.png"
        path.write_bytes(key.encode())
        return [str(path)]

    monkeypatch.setattr(pipeline, "_job", completed)
    pipeline._render()
    assert first_attempt[0] == retry_attempt[0]
    assert pipeline.state["asset_generation_refs"]["prop_a"] == pinned
    library.close()


def test_changed_style_cannot_resume_existing_run(tmp_path, monkeypatch):
    pipeline = Pipeline(Config())
    monkeypatch.setattr(
        pipeline,
        "_infer",
        lambda source: pipeline.state.update({"panels": [], "assets": []}),
    )
    pipeline.run("input text", tmp_path / "run")
    state = json.loads((tmp_path / "run" / "state.json").read_text(encoding="utf-8"))
    assert state["identity"]["config"]["style"]["preset"] == "cinematic_anime_cg"
    assert state["identity"]["style"] == StyleConfig().resolved()
    pipeline.run("input text", tmp_path / "run", resume=True)
    pipeline.config.style = StyleConfig(
        presets={"cinematic_anime_cg": StylePreset(prefix="An updated style.")}
    )
    with pytest.raises(ValueError, match="config/workflow changed"):
        pipeline.run("input text", tmp_path / "run", resume=True)


def test_default_workflow_identity_includes_negative_binding():
    pipeline = Pipeline(Config())
    identity = pipeline._workflow_identity(pipeline.image_template)
    assert "negative_prompt" in identity["profile"]["bindings"]
    assert Path("workflows/workflow_bindings.json").is_file()
