from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from novel_prompt_pipeline.resources import initialize_workspace
from pipeline.cli import main
from pipeline.config import Config
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


def test_face_reference_binding_and_workspace_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    photo = picture(workspace / "portrait.png", "red")
    config_dir = workspace / "configs"
    config_dir.mkdir()
    config_path = config_dir / "pipeline.json"
    config_path.write_text(
        json.dumps({"asset_library": "../assets.sqlite"}),
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
