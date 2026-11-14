from types import SimpleNamespace

import pytest
from PIL import Image

from pipeline.config import MediaConfig
from pipeline.images.tasks import ImageTaskPending, list_image_tasks
from pipeline.library import AssetLibrary
from pipeline.shuohao.media import prepare_assets, prepare_frames, render_videos
from pipeline.styles import StyleConfig


def _production(tmp_path):
    return SimpleNamespace(
        root=tmp_path,
        state={},
        config=SimpleNamespace(media=MediaConfig(), style=StyleConfig()),
        library=AssetLibrary(tmp_path / "library" / "catalog.sqlite"),
        _save=lambda: None,
    )


def _documents():
    outline = {"characters": [{"id": "C01", "name": "小周"}]}
    cast = {"characters": [{"name": "小周", "image": {"promptLocal": "年轻船夫，穿旧衣。"}}]}
    art = {
        "scenes": [{"id": "S01", "name": "渡口", "image": {"promptLocal": "清晨空无一人的渡口。"}}],
        "props": [
            {
                "id": "P01",
                "name": "箱子",
                "scale": "手持级",
                "image": {"promptLocal": "白底旧木箱，无人无手。"},
            }
        ],
    }
    script = {"episodes": [{"ep": 1, "scenes": [{"sceneId": "S01"}]}]}
    cuts = [
        {"frame": "中景，小周站在渡口。", "characters": ["C01"], "props": ["P01"], "seconds": 3},
        {"frame": "特写，木箱置于地面。", "characters": [], "props": ["P01"], "seconds": 4},
    ]
    storyboard = {
        "episodes": [
            {
                "ep": 1,
                "segments": [
                    {
                        "id": "E01-01",
                        "sceneIndex": 1,
                        "cuts": cuts,
                        "h3Prompt": "alignment\nintegrated_multimodal_description:\n[Shot 1] motion\noverall_soundscape: wind\nnon_diegetic_music: N/A",
                    }
                ],
            }
        ]
    }
    return outline, cast, art, script, storyboard


def test_asset_and_cut_handoffs_are_batched(tmp_path):
    p = _production(tmp_path)
    outline, cast, art, script, storyboard = _documents()
    with pytest.raises(ImageTaskPending):
        prepare_assets(p, cast, art, outline)
    tasks = list_image_tasks(p.root)
    assert len(tasks) == 3
    assert all("promptLocal" not in t["request"] for t in tasks)
    assert {t["request"]["kind"] for t in tasks} == {"character", "scene", "prop"}
    # Use locally linked fixture images to test frame scheduling, not image approval.
    image = tmp_path / "fixture.png"
    Image.new("RGB", (64, 64), "white").save(image)
    p.state["asset_media"] = {
        a["asset_id"]: p.library.link_file(a["asset_id"], image, asset_version=a["asset_version"])
        for a in p.state["assets"]
    }
    with pytest.raises(ImageTaskPending):
        prepare_frames(p, storyboard, script)
    frames = [t for t in list_image_tasks(p.root) if t["request"]["kind"] == "frame"]
    assert [t["request"]["key"] for t in frames] == ["panel_1_cut_2_image", "panel_1_image"]
    by_key = {t["request"]["key"]: t for t in frames}
    assert len(by_key["panel_1_image"]["request"]["references"]) == 3
    assert len(by_key["panel_1_cut_2_image"]["request"]["references"]) == 2
    roles = [ref["role"] for ref in by_key["panel_1_image"]["request"]["references"]]
    assert roles == [
        "场景既有的环境、空间布局和材质；资产名：渡口",
        "角色的脸部身份、服装和配饰；资产名：小周",
        "物品既有的造型、材质和当前状态；资产名：箱子",
    ]
    assert by_key["panel_1_image"]["request"]["width"] == 1024
    assert by_key["panel_1_image"]["request"]["height"] == 576


def test_multi_cut_video_uses_cut_frames_in_order(tmp_path, monkeypatch):
    p = _production(tmp_path)
    _, _, _, script, storyboard = _documents()
    paths = []
    for i in range(2):
        path = tmp_path / f"frame{i}.png"
        Image.new("RGB", (32, 32), (i, 0, 0)).save(path)
        import hashlib

        paths.append(
            {
                "local_path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "task_id": f"task{i}",
                "task_sha256": f"request{i}",
            }
        )
    p.state["shuohao_frame_media"] = {"panel_1_image": paths[0], "panel_1_cut_2_image": paths[1]}
    monkeypatch.setattr(
        "pipeline.shuohao.media.list_image_tasks",
        lambda root: [
            {
                "task_id": record["task_id"],
                "request": {"key": key, "task_sha256": record["task_sha256"]},
                "result": {"sha256": record["sha256"]},
                "result_file": record["local_path"],
            }
            for key, record in p.state["shuohao_frame_media"].items()
        ],
    )
    calls = []

    class Template:
        reference_capacity = 8
        profile = {"bindings": {"width": [{}], "height": [{}], "seed": [{}]}}

        def prepare(self, text, **kwargs):
            calls.append((text, kwargs))
            return kwargs

    p._mode_template = lambda mode: Template()
    p._activate_server = lambda mode, key: calls.append((mode, key))
    p._upload = lambda path: path
    p._job = lambda key, graph: calls.append((key, graph))
    render_videos(p, storyboard, script)
    assert calls[1] == ("ref2va", "panel_1_video")
    text, binding = calls[2]
    assert text.startswith("alignment\n")
    assert binding["first_frame"] == paths[0]["local_path"]
    assert binding["reference_images"] == [paths[1]["local_path"]]
    assert binding["reference_audios"] == []
    assert binding["length"] == 7
    assert (binding["width"], binding["height"], binding["seed"]) == (1024, 576, 1)


def test_accepted_assets_resume_and_crop_then_prepare_all_frames(tmp_path, monkeypatch):
    import hashlib

    from pipeline.images.tasks import complete_image_task

    p = _production(tmp_path)
    outline, cast, art, script, storyboard = _documents()
    old_id = "character_" + hashlib.sha256("小周".encode()).hexdigest()[:16]
    p.library.register(
        [
            {
                "asset_id": old_id,
                "kind": "character",
                "name": "小周",
                "aliases": "小周",
                "description": "旧描述",
            }
        ]
    )
    with pytest.raises(ImageTaskPending):
        prepare_assets(p, cast, art, outline)
    assert p.state["shuohao_asset_plan"]["ids"]["C01"] == old_id
    # Model the timestamp mutation from later writes to the same catalog entry.
    with p.library.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE assets SET updated_at = '2099-01-01'")
    image = tmp_path / "accepted-fixture.png"
    Image.new("RGB", (160, 90), "white").save(image)
    for task in list_image_tasks(p.root):
        region = [5, 6, 100, 60] if task["request"]["kind"] in {"scene", "prop"} else None
        complete_image_task(
            p.root,
            task["task_id"],
            image,
            notes="fixture acceptance",
            accepted=True,
            main_region=region,
        )
    snapshot_calls = []

    def counted_tasks(root):
        snapshot_calls.append(root)
        return list_image_tasks(root)

    monkeypatch.setattr("pipeline.shuohao.media.list_image_tasks", counted_tasks)
    prepare_assets(p, cast, art, outline)
    assert len(snapshot_calls) == 1
    assert len(p.state["asset_media"]) == 3
    for asset in p.state["assets"]:
        main = p.state["asset_media"][asset["asset_id"]]
        board = p.state["asset_sheet_media"][asset["asset_id"]]
        with Image.open(main["local_path"]) as crop:
            assert crop.size == ((160, 90) if asset["kind"] == "character" else (95, 54))
        if asset["kind"] != "character":
            assert ":region:5,6,100,60,160,90" in board["role"]
    prepare_assets(p, cast, art, outline)
    with pytest.raises(ImageTaskPending):
        prepare_frames(p, storyboard, script)
    frames = [t for t in list_image_tasks(p.root) if t["request"]["kind"] == "frame"]
    assert len(frames) == 2
    for task in frames:
        complete_image_task(
            p.root, task["task_id"], image, notes="fixture acceptance", accepted=True
        )
    snapshot_calls.clear()
    prepare_frames(p, storyboard, script)
    assert len(snapshot_calls) == 1
    for cut in (1, 2):
        exported = p.root / "frames" / "E01-01" / f"f{cut}.png"
        assert exported.read_bytes() == image.read_bytes()
    prepare_frames(p, storyboard, script)
    (p.root / "frames" / "E01-01" / "f1.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Existing frame export changed"):
        prepare_frames(p, storyboard, script)


def test_segment_id_cannot_escape_frame_directory(tmp_path):
    p = _production(tmp_path)
    outline, cast, art, script, storyboard = _documents()
    with pytest.raises(ImageTaskPending):
        prepare_assets(p, cast, art, outline)
    storyboard["episodes"][0]["segments"][0]["id"] = "../outside"
    with pytest.raises(ValueError, match="Invalid or duplicate segment"):
        prepare_frames(p, storyboard, script)


def test_changed_photo_rejected_and_video_without_frames_never_submits(tmp_path):
    p = _production(tmp_path)
    outline, cast, art, script, storyboard = _documents()
    photo = tmp_path / "face.png"
    Image.new("RGB", (64, 64), "white").save(photo)
    p.config.media.character_face_references = {"小周": str(photo)}
    p.config.media.character_face_reference_scopes = {"小周": ["face_shape"]}
    with pytest.raises(ImageTaskPending):
        prepare_assets(p, cast, art, outline)
    Image.new("RGB", (64, 64), "black").save(photo)
    with pytest.raises(ValueError, match="inputs changed"):
        prepare_assets(p, cast, art, outline)
    submitted = []
    p._job = lambda *args: submitted.append(args)
    with pytest.raises((KeyError, ValueError)):
        render_videos(p, storyboard, script)
    assert submitted == []


@pytest.mark.parametrize("mode", ["i2va", "ref2va"])
@pytest.mark.parametrize("width,height", [(1024, 576), (576, 1024)])
def test_default_video_workflow_dimensions_and_seed(mode, width, height):
    from comfyui_client import get_default_template
    from pipeline.shuohao.media import _video_graph

    template = get_default_template(mode)
    segment = {"cuts": [{"seconds": 3}]}
    paths = ["frame1.png"] if mode == "i2va" else ["frame1.png", "frame2.png"]
    graph = _video_graph(template, "test", segment, paths, width, height, 37)
    if mode == "i2va":
        for node in ("122", "902"):
            assert (graph[node]["inputs"]["自定义宽"], graph[node]["inputs"]["自定义高"]) == (
                width,
                height,
            )
        assert (
            graph["908"]["inputs"]["target_width"],
            graph["908"]["inputs"]["target_height"],
        ) == (width, height)
        assert graph["137"]["inputs"]["noise_seed"] == 37
    else:
        assert (graph["13"]["inputs"]["target_width"], graph["13"]["inputs"]["target_height"]) == (
            width,
            height,
        )
        assert graph["29"]["inputs"]["aspect_ratio"] == (
            "16:9 (Widescreen)" if width > height else "9:16 (Portrait Widescreen)"
        )
        assert graph["11"]["inputs"]["noise_seed"] == 37


def test_video_workflow_missing_size_binding_rejected():
    from pipeline.shuohao.media import _video_graph

    template = SimpleNamespace(profile={"bindings": {"width": [{}]}})
    with pytest.raises(ValueError, match="width and height"):
        _video_graph(template, "test", {"cuts": [{"seconds": 3}]}, ["frame.png"], 1024, 576, 1)
