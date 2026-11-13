from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from comfyui_client import WorkflowTemplate
from novel_prompt_pipeline.resources import initialize_workspace
from pipeline.cli import main
from pipeline.config import Config, MediaConfig
from pipeline.engine import Pipeline
from pipeline.library import AssetLibrary
from pipeline.mcp_server import create_server
from pipeline.service import AppService, WorkspacePathError


def picture(path: Path, color: str):
    Image.new("RGB", (16, 16), color).save(path)
    return path


def asset_record(kind: str):
    return {
        "asset_id": f"{kind}_one",
        "kind": kind,
        "name": "默认的许青" if kind == "character" else "铜钥匙",
        "aliases": "许青" if kind == "character" else "铜钥匙",
        "description": "A visual description from the source.",
    }


def prepared_pipeline(tmp_path, monkeypatch, kind, *, square=False, config=None):
    config = config or Config(asset_library=str(tmp_path / "assets.sqlite"))
    pipeline = Pipeline(config)
    pipeline.root = tmp_path
    pipeline.release = SimpleNamespace(
        manifest={
            "nodes": {
                "asset.prop": {"output_contract": {"image_aspect_ratio": "1:1"} if square else {}}
            }
        }
    )
    pipeline.library = AssetLibrary(config.asset_library, config.project_id)
    asset = pipeline.library.register([asset_record(kind)])[0]
    pipeline.state = {
        "assets": [asset],
        "panels": [
            {
                "panel_index": 0,
                "generation_mode": "i2va",
                "asset_ids": [asset["asset_id"]],
                "first_frame_asset_ids": [asset["asset_id"]],
                "prompt": "A panel.",
                "video_prompt": (
                    "For the target video, at 0.00 seconds into the target video, "
                    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
                    "integrated_multimodal_description:\n[Shot 1] A person walks.\n\n"
                    "overall_soundscape:\nFootsteps.\n\n"
                    "non_diegetic_music:\nN/A\n时长：6秒"
                ),
                "duration_seconds": 6,
            }
        ],
        "media": {},
    }
    dummy = picture(tmp_path / "output.png", "white")
    uploads = []
    graphs = {}
    monkeypatch.setattr(pipeline, "_save", lambda: None)

    def upload(path, **kwargs):
        uploads.append(Path(path))
        return Path(path).name

    def job(key, graph):
        graphs[key] = graph
        return [str(dummy)]

    monkeypatch.setattr(pipeline, "_upload", upload)
    monkeypatch.setattr(pipeline, "_job", job)
    return pipeline, asset, uploads, graphs


def test_new_contract_uses_square_prop_without_changing_legacy_dimensions(tmp_path, monkeypatch):
    pipeline, asset, _, graphs = prepared_pipeline(tmp_path, monkeypatch, "prop", square=True)
    pipeline._render()
    graph = graphs["asset_" + asset["asset_id"]]
    assert (graph["36"]["inputs"]["width"], graph["36"]["inputs"]["height"]) == (1024, 1024)
    assert pipeline.state["asset_media"][asset["asset_id"]]["role"].startswith("reference:render:")
    pipeline.release.manifest["nodes"]["asset.prop"]["output_contract"] = {}
    assert pipeline._asset_size(asset) == (1024, 576)
    assert "prop_size" not in Config().public_dict()["media"]
    with pytest.raises(ValueError):
        MediaConfig(prop_size=1000)
    pipeline.close()


def test_attached_face_is_first_reference_and_preserves_description(tmp_path, monkeypatch):
    pipeline, asset, uploads, graphs = prepared_pipeline(tmp_path, monkeypatch, "character")
    face = picture(tmp_path / "face.png", "red")
    original = picture(tmp_path / "identity.png", "blue")
    face_record = pipeline.library.link_file(asset["asset_id"], face, role="face")
    original_record = pipeline.library.link_file(asset["asset_id"], original, role="reference")
    pipeline._render()
    graph = graphs["asset_" + asset["asset_id"]]
    assert graph["44"]["inputs"]["image"] == Path(face_record["local_path"]).name
    assert graph["45"]["inputs"]["image"] == Path(original_record["local_path"]).name
    assert uploads[:2] == [Path(face_record["local_path"]), Path(original_record["local_path"])]
    prompt = graph["37"]["inputs"]["prompt"]
    assert "only as the person's facial identity" in prompt
    assert asset["description"] in prompt
    assert pipeline.state["assets"][0]["description"] == "A visual description from the source."
    assert (
        pipeline.state["asset_generation_faces"][asset["asset_id"]]["sha256"]
        == face_record["sha256"]
    )
    assert pipeline.state["asset_media"][asset["asset_id"]]["role"].startswith("reference:render:")
    pipeline.close()


def test_configured_name_maps_face_to_variants_and_is_frozen_on_resume(tmp_path, monkeypatch):
    first = picture(tmp_path / "first.png", "red")
    second = picture(tmp_path / "second.png", "blue")
    config = Config(asset_library=str(tmp_path / "assets.sqlite"))
    config.media.character_face_references = {"许青": str(first)}
    pipeline, asset, uploads, graphs = prepared_pipeline(
        tmp_path, monkeypatch, "character", config=config
    )

    def interrupted(key, graph):
        if key.startswith("asset_"):
            raise RuntimeError("network interrupted")
        raise AssertionError("unexpected media submission")

    monkeypatch.setattr(pipeline, "_job", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        pipeline._render()
    pinned = pipeline.state["asset_generation_faces"][asset["asset_id"]]
    assert Path(pinned["local_path"]).read_bytes() == first.read_bytes()
    pipeline.library.link_file(asset["asset_id"], second, role="face")
    dummy = picture(tmp_path / "result.png", "white")
    monkeypatch.setattr(
        pipeline, "_job", lambda key, graph: graphs.setdefault(key, graph) and [str(dummy)]
    )
    pipeline._render()
    assert uploads[-3] == Path(pinned["local_path"])
    assert (
        graphs["asset_" + asset["asset_id"]]["44"]["inputs"]["image"]
        == Path(pinned["local_path"]).name
    )
    pipeline.close()


def test_portrait_slots_are_checked_before_any_submission(tmp_path, monkeypatch):
    pipeline, asset, uploads, _ = prepared_pipeline(tmp_path, monkeypatch, "character")
    face = picture(tmp_path / "face.png", "red")
    original = picture(tmp_path / "identity.png", "blue")
    pipeline.library.link_file(asset["asset_id"], face, role="face")
    pipeline.library.link_file(asset["asset_id"], original, role="reference")
    original_template = pipeline.image_template
    profile = original_template.profile
    profile["reference_slots"] = profile["reference_slots"][:1]
    pipeline.image_template = WorkflowTemplate(original_template.graph, profile)
    monkeypatch.setattr(pipeline, "_job", lambda *_: pytest.fail("must not submit"))
    with pytest.raises(ValueError, match="requires 2 image reference slots"):
        pipeline._render()
    assert uploads == []
    pipeline.close()


@pytest.mark.parametrize("has_face", [True, False])
def test_i2va_zero_image_slots_checks_face_before_any_submission(tmp_path, monkeypatch, has_face):
    pipeline, asset, uploads, graphs = prepared_pipeline(tmp_path, monkeypatch, "character")
    if has_face:
        face = picture(tmp_path / "face.png", "red")
        pipeline.library.link_file(asset["asset_id"], face, role="face")
    template = pipeline.image_template
    profile = template.profile
    profile["reference_slots"] = []
    pipeline.image_template = WorkflowTemplate(template.graph, profile)
    if has_face:
        with pytest.raises(ValueError, match="requires 1 image reference slots"):
            pipeline._render()
        assert uploads == [] and graphs == {}
    else:
        pipeline._render()
        assert "asset_" + asset["asset_id"] not in graphs
        assert "panel_0_image" in graphs and "panel_0_video" in graphs
    pipeline.close()


def test_new_run_does_not_reuse_a_different_persons_face(tmp_path, monkeypatch):
    first = picture(tmp_path / "first.png", "red")
    second = picture(tmp_path / "second.png", "blue")
    one, asset, _, first_graphs = prepared_pipeline(tmp_path, monkeypatch, "character")
    one.library.link_file(asset["asset_id"], first, role="face")
    one._render()
    first_role = one.state["asset_media"][asset["asset_id"]]["role"]
    one.close()

    two, asset, uploads, second_graphs = prepared_pipeline(tmp_path, monkeypatch, "character")
    selected = two.library.link_file(asset["asset_id"], second, role="face")
    two._render()
    second_role = two.state["asset_media"][asset["asset_id"]]["role"]
    assert first_role != second_role
    assert "asset_" + asset["asset_id"] in first_graphs
    assert "asset_" + asset["asset_id"] in second_graphs
    assert uploads[0] == Path(selected["local_path"])
    two.close()


def test_face_reference_binding_and_workspace_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    photo = picture(workspace / "portrait.png", "red")
    config_dir = workspace / "configs"
    config_dir.mkdir()
    config_path = config_dir / "pipeline.json"
    config_path.write_text(
        json.dumps({"asset_library": "../assets.sqlite", "prompt_project": "../prompts"}),
        encoding="utf-8",
    )
    service = AppService(workspace, config_path=config_path, restricted=True)
    library = service.library()
    character = library.register([asset_record("character")])[0]
    prop = library.register([asset_record("prop")])[0]
    result = service.asset_link_face(character["asset_id"], "portrait.png")
    assert result["role"] == "face" and not Path(result["local_path"]).is_absolute()
    with pytest.raises(ValueError, match="character assets"):
        service.asset_link_face(prop["asset_id"], "portrait.png")
    with pytest.raises(WorkspacePathError):
        service.asset_link_face(character["asset_id"], "../portrait.png")
    (workspace / "invalid.jpg").write_bytes(b"not a picture")
    with pytest.raises(ValueError, match="cannot be read"):
        service.asset_link_face(character["asset_id"], "invalid.jpg")
    outside = picture(tmp_path / "outside.png", "green")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["media"] = {"character_face_references": {"x": str(outside)}}
    config_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkspacePathError):
        service.config()
    assert photo.exists()
    library.close()


def test_cli_exposes_link_face(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    photo = picture(workspace / "portrait.png", "red")
    config = Config(asset_library=str(workspace / "runs" / "library" / "assets.sqlite3"))
    library = AssetLibrary(config.asset_library)
    asset = library.register([asset_record("character")])[0]
    assert (
        main(["--workspace", str(workspace), "assets", "link-face", asset["asset_id"], str(photo)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["role"] == "face"
    library.close()


def test_mcp_exposes_face_binding_and_confines_photo_path(tmp_path):
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    picture(workspace / "portrait.png", "red")
    library = AppService(workspace).library()
    asset = library.register([asset_record("character")])[0]

    async def exercise():
        async with create_connected_server_and_client_session(create_server(workspace)) as session:
            tools = await session.list_tools()
            assert "asset_link_face" in {tool.name for tool in tools.tools}
            response = await session.call_tool(
                "asset_link_face", {"asset_id": asset["asset_id"], "file": "portrait.png"}
            )
            assert not response.isError
            denied = await session.call_tool(
                "asset_link_face", {"asset_id": asset["asset_id"], "file": "../portrait.png"}
            )
            assert denied.isError

    anyio.run(exercise)
    assert library.files(asset["asset_id"], role="face")
    library.close()
