"""Translate validated shuohao documents into immutable media handoffs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..config import verify_face_photo
from ..images.layout import _main_reference, _prompt
from ..images.tasks import ImageTaskPending, list_image_tasks, prepare_image_task
from ..storage import fingerprint

_LAYOUTS = {
    "character": "横向纯白底设定板。左侧约34%为正面半身肖像和面部基准；右上为同一人物、同一服装、等比例全身正面、标准侧面、背面；右下三个细节格仅放大已知可见细节，不足时重复截取已知细节，未知背面不补造装饰。",
    "scene": "横向L形板，左上主格约占宽度72%、高度70%，只展示一个标准机位；右侧两格、底部约三格仅裁切放大主图已见细节，不生成新机位，不编造隐藏区域。画面无人、无手。",
    "prop": "横向纯白底L形板，左上主格约占宽度72%、高度70%，已知完整形态用当前状态四分之三角度；右侧和底部细节格展示有依据的细节、其他状态或侧视图，不补造未知部分。画面无人、无手、无手指。",
}
_ROLES = {
    "character": "角色的脸部身份、服装和配饰",
    "scene": "场景既有的环境、空间布局和材质",
    "prop": "物品既有的造型、材质和当前状态",
}
_VERSION = "shuohao-v1"


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify(record):
    path = Path(record["local_path"])
    if path.is_symlink() or not path.is_file() or _hash(path) != record["sha256"]:
        raise ValueError("Pinned media is missing or changed")


def _asset_id(kind, name):
    return kind + "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _assets(cast, art, outline):
    chars = {item["name"]: item for item in cast["characters"]}
    if len(chars) != len(cast["characters"]):
        raise ValueError("Duplicate character names")
    rows, ids = [], {}
    for item in outline["characters"]:
        if item["name"] not in chars:
            raise ValueError("Outline character missing from cast")
        ids[item["id"]] = _asset_id("character", item["name"])
    for kind, items in (
        ("character", cast["characters"]),
        ("scene", art["scenes"]),
        ("prop", art.get("props", [])),
    ):
        for item in items:
            name = item["name"]
            local = item.get("image", {}).get("promptLocal")
            if (
                not isinstance(local, str)
                or not local.strip()
                or not re.search(r"[\u3400-\u9fff]", local)
            ):
                raise ValueError(f"{name} requires Chinese image.promptLocal")
            aid = _asset_id(kind, name)
            if kind != "character":
                if item["id"] in ids:
                    raise ValueError("Duplicate upstream asset ID")
                ids[item["id"]] = aid
            aliases = item.get("aliases") or []
            rows.append(
                {
                    "asset_id": aid,
                    "kind": kind,
                    "name": name,
                    "aliases": ",".join(aliases) or name,
                    "description": local,
                    "local": local,
                    "scale": item.get("scale"),
                }
            )
    if len({a["asset_id"] for a in rows}) != len(rows):
        raise ValueError("Duplicate assets")
    return rows, ids


def _face(p, asset):
    names = {asset["name"], *asset["aliases"].split(",")}
    refs = {
        str(Path(v).expanduser().resolve())
        for k, v in p.config.media.character_face_references.items()
        if k in names
    }
    scopes = [v for k, v in p.config.media.character_face_reference_scopes.items() if k in names]
    if len(refs) > 1 or any(v != scopes[0] for v in scopes):
        raise ValueError("Conflicting character face references or scopes")
    scope = scopes[0] if scopes else ["full_face"]
    if scopes and not refs:
        raise ValueError("Character face scope requires a reference photo")
    if not refs:
        return []
    source = verify_face_photo(next(iter(refs)))
    selected = "、".join(
        {"full_face": "脸部身份", "face_shape": "脸型", "hair": "发型"}[x] for x in scope
    )
    return [
        {
            "path": str(source),
            "sha256": _hash(source),
            "role": f"仅参考用户指定的{selected}；不参考照片中其他外观、服装、姿势、背景或摄影风格",
        }
    ]


def _pending(p, pending):
    p.state["pending_image_tasks"] = pending
    p._save()
    if pending:
        raise ImageTaskPending(pending[0])


def _task_snapshot(root):
    tasks = {}
    for task in list_image_tasks(root):
        key = task["request"]["key"]
        if key in tasks:
            raise ValueError("Multiple image tasks for the same key")
        tasks[key] = task
    return tasks


def _completed_task(tasks, key, result):
    task = tasks.get(key)
    if (
        not task
        or task["status"] != "completed"
        or not task["result"]
        or result != [task["result_file"]]
    ):
        raise ValueError("Completed image task does not match the verified snapshot")
    return task


def prepare_assets(p, cast, art, outline):
    if p.config.media.asset_width * 9 != p.config.media.asset_height * 16:
        raise ValueError("Asset sheets require a 16:9 canvas")
    rows, ids = _assets(cast, art, outline)
    raw = [{k: a[k] for k in ("asset_id", "kind", "name", "aliases", "description")} for a in rows]
    previous = p.state.get("shuohao_asset_plan")
    stable_fields = (
        "asset_id",
        "kind",
        "name",
        "aliases",
        "description",
        "asset_version",
        "archived",
    )
    if previous is None:
        registered = p.library.register(raw, reuse_existing=False)
        if len(registered) != len(rows):
            raise ValueError("Archived asset cannot be used")
        assets = [{key: item[key] for key in stable_fields} for item in registered]
    else:
        assets = [{key: item[key] for key in stable_fields} for item in previous["assets"]]
        old_raw = [
            {key: item[key] for key in ("asset_id", "kind", "name", "aliases", "description")}
            for item in assets
        ]
        if previous["ids"] != ids or old_raw != raw:
            raise ValueError("Asset mapping changed; create a new run")
        for asset in assets:
            current = p.library.get(asset["asset_id"])
            if {key: current[key] for key in stable_fields} != asset or current["archived"]:
                raise ValueError("Pinned asset version changed or was archived")
    mapping = {a["asset_id"]: a for a in assets}
    plan = {"assets": assets, "ids": ids}
    p.state["shuohao_asset_plan"] = plan
    p.state["assets"] = assets
    tasks = _task_snapshot(p.root)
    pending = []
    for row in rows:
        asset = mapping[row["asset_id"]]
        refs = _face(p, asset) if row["kind"] == "character" else []
        style = (
            (p.config.style.resolved_identity() if refs else p.config.style.resolved())
            if row["kind"] == "character"
            else p.config.style.resolved_material()
        )
        content = row["local"]
        if row["kind"] == "prop":
            scale = {"手持级": "手持尺度", "桌面级": "桌面尺度", "家具级": "家具尺度"}.get(
                row["scale"], "未知尺度"
            )
            content += " " + scale
        prompt = _prompt(content, _LAYOUTS[row["kind"]], style, refs)
        try:
            result = prepare_image_task(
                p.root,
                "asset_" + asset["asset_id"],
                prompt=prompt,
                prompt_version=_VERSION,
                kind=row["kind"],
                width=p.config.media.asset_width,
                height=p.config.media.asset_height,
                references=refs,
                context={
                    "mode": "generate",
                    "stage": "asset_sheet",
                    "layout": _VERSION,
                    "asset_id": asset["asset_id"],
                    "asset_version": asset["asset_version"],
                },
            )
        except ImageTaskPending as exc:
            pending.append(exc.task_id)
            continue
        task = _completed_task(tasks, "asset_" + asset["asset_id"], result)
        region = task["result"].get("main_region") if row["kind"] != "character" else None
        role_id = fingerprint({"request": task["request"]["task_sha256"]})
        role = f"sheet:{_VERSION}:{role_id}"
        if region:
            role += ":region:" + ",".join(
                str(x) for x in [*region["bounds"], region["width"], region["height"]]
            )
        board = p.library.link_file(
            asset["asset_id"], result[0], role=role, asset_version=asset["asset_version"]
        )
        main_role = f"main:{_VERSION}:{role_id}:{fingerprint(region)}:{board['sha256']}"

        # The crop helper verifies the board and retains reviewed region bounds.
        class CropContext:
            root, library = p.root, p.library
            _verify_media = staticmethod(_verify)

        main = _main_reference(CropContext(), asset, board, main_role, region)
        for key, value in (("asset_sheet_media", board), ("asset_media", main)):
            records = p.state.setdefault(key, {})
            if asset["asset_id"] in records and records[asset["asset_id"]] != value:
                raise ValueError("Pinned asset media changed")
            records[asset["asset_id"]] = value
    _pending(p, pending)


def _segments(storyboard, script):
    scenes = {ep["ep"]: ep["scenes"] for ep in script["episodes"]}
    index = 0
    seen = set()
    for episode in storyboard["episodes"]:
        for segment in episode["segments"]:
            index += 1
            if (
                not isinstance(segment["id"], str)
                or not re.fullmatch(r"E[0-9]{2,}-[0-9]{2,}", segment["id"])
                or segment["id"] in seen
                or not 1 <= len(segment["cuts"]) <= 9
            ):
                raise ValueError("Invalid or duplicate segment")
            seen.add(segment["id"])
            yield index, segment, scenes[episode["ep"]][segment["sceneIndex"] - 1]


def _frame_dimensions(p, storyboard):
    aspect = storyboard.get("aspectRatio", "16:9")
    if aspect not in {"16:9", "9:16"}:
        raise ValueError("Unsupported storyboard aspect ratio")
    width, height = sorted((p.config.media.frame_width, p.config.media.frame_height), reverse=True)
    if width * 9 != height * 16:
        raise ValueError("Frame dimensions must have a 16:9 or 9:16 aspect ratio")
    if aspect == "9:16":
        width, height = height, width
    return width, height


def _export_frame(root, segment_id, cut_index, record):
    _verify(record)
    root = Path(root).resolve(strict=True)
    directory = root
    for part in ("frames", segment_id):
        directory = directory / part
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ValueError("Frame export directories cannot be symlinks or junctions")
        directory.mkdir(exist_ok=True)
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            raise ValueError("Invalid frame export directory")
    destination = directory / f"f{cut_index}.png"
    if destination.is_symlink() or getattr(destination, "is_junction", lambda: False)():
        raise ValueError("Frame export cannot be a symlink or junction")
    data = Path(record["local_path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("Pinned cut frame changed")
    try:
        with destination.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if not destination.is_file() or _hash(destination) != record["sha256"]:
            raise ValueError("Existing frame export changed") from None


def prepare_frames(p, storyboard, script):
    width, height = _frame_dimensions(p, storyboard)
    ids = p.state["shuohao_asset_plan"]["ids"]
    assets = {a["asset_id"]: a for a in p.state["assets"]}
    media = p.state.get("asset_media", {})
    segments = list(_segments(storyboard, script))
    # Validate all referenced assets before creating any frames.
    plans = []
    for index, segment, scene in segments:
        for cut_index, cut in enumerate(segment["cuts"], 1):
            upstream = [
                scene["sceneId"],
                *[c for c in cut["characters"] if c != "VO"],
                *cut.get("props", []),
            ]
            selected = list(dict.fromkeys(ids[x] for x in upstream))
            refs = []
            for aid in selected:
                record = media[aid]
                _verify(record)
                refs.append(
                    {
                        "path": record["local_path"],
                        "sha256": record["sha256"],
                        "role": f"{_ROLES[assets[aid]['kind']]}；资产名：{assets[aid]['name']}",
                    }
                )
            if len(refs) > 10:
                raise ValueError("Cut exceeds image reference capacity")
            plans.append((index, segment, cut_index, cut, refs))
    tasks = _task_snapshot(p.root)
    pending = []
    for index, segment, cut_index, cut, refs in plans:
        key = f"panel_{index}_image" if cut_index == 1 else f"panel_{index}_cut_{cut_index}_image"
        prompt = _prompt(cut["frame"], "", p.config.style.resolved_material(), refs)
        try:
            result = prepare_image_task(
                p.root,
                key,
                prompt=prompt,
                prompt_version=_VERSION,
                kind="frame",
                width=width,
                height=height,
                references=refs,
                context={
                    "mode": "generate",
                    "stage": "first_frame",
                    "panel_index": index,
                    "segment_id": segment["id"],
                    "cut_index": cut_index,
                },
            )
        except ImageTaskPending as exc:
            pending.append(exc.task_id)
            continue
        task = _completed_task(tasks, key, result)
        record = {
            "task_id": task["task_id"],
            "task_sha256": task["request"]["task_sha256"],
            "sha256": task["result"]["sha256"],
            "local_path": result[0],
        }
        records = p.state.setdefault("shuohao_frame_media", {})
        if key in records and records[key] != record:
            raise ValueError("Pinned cut frame changed")
        _export_frame(p.root, segment["id"], cut_index, record)
        records[key] = record
    _pending(p, pending)


def _video_graph(template, text, segment, paths, width, height, seed):
    profile = template.profile
    bindings = profile.get("bindings", {})
    if not bindings.get("width") or not bindings.get("height"):
        raise ValueError("Video workflow must support width and height bindings")
    options = {"width": width, "height": height}
    if bindings.get("seed"):
        options["seed"] = seed
    graph = template.prepare(
        text,
        length=sum(c["seconds"] for c in segment["cuts"]),
        first_frame=paths[0],
        reference_images=paths[1:],
        reference_audios=[],
        **options,
    )
    aspect = profile.get("shuohao_aspect_ratio")
    if aspect is not None:
        node = graph.get(aspect.get("node_id"), {})
        field = aspect.get("input")
        if (
            node.get("class_type") != "ResolutionSelector"
            or field != "aspect_ratio"
            or field not in node.get("inputs", {})
        ):
            raise ValueError("Invalid video workflow aspect ratio binding")
        node["inputs"][field] = (
            "16:9 (Widescreen)" if width > height else "9:16 (Portrait Widescreen)"
        )
    return graph


def render_videos(p, storyboard, script):
    segments = list(_segments(storyboard, script))
    width, height = _frame_dimensions(p, storyboard)
    tasks = {task["request"]["key"]: task for task in list_image_tasks(p.root)}
    ready = []
    for index, segment, _ in segments:
        paths = []
        for cut_index in range(1, len(segment["cuts"]) + 1):
            key = (
                f"panel_{index}_image" if cut_index == 1 else f"panel_{index}_cut_{cut_index}_image"
            )
            record = p.state["shuohao_frame_media"][key]
            task = tasks.get(key)
            if (
                not task
                or not task.get("result")
                or task["task_id"] != record["task_id"]
                or task["request"]["task_sha256"] != record["task_sha256"]
                or task["result"]["sha256"] != record["sha256"]
                or task["result_file"] != record["local_path"]
            ):
                raise ValueError("Pinned cut frame task or result changed")
            _verify(record)
            paths.append(record["local_path"])
        mode = "i2va" if len(paths) == 1 else "ref2va"
        template = p._mode_template(mode)
        if len(paths) - 1 > template.reference_capacity:
            raise ValueError("Segment exceeds video reference capacity")
        if re.search(r"<\s*audio\b", segment["h3Prompt"], re.I):
            raise ValueError("Reference audio is unsupported")
        # Both upstream multi-cut and single-cut H3 use the integrated section.
        text = p.config.style.compose_video(segment["h3Prompt"], "i2va")
        _video_graph(
            template,
            text,
            segment,
            ["preflight.png"] * len(paths),
            width,
            height,
            p.config.media.seed,
        )
        ready.append((index, segment, paths, mode, template, text))
    for index, segment, paths, mode, template, text in ready:
        key = f"panel_{index}_video"
        p._activate_server(mode, key)
        first = p._upload(paths[0])
        refs = [p._upload(path) for path in paths[1:]] if mode == "ref2va" else []
        graph = _video_graph(
            template, text, segment, [first, *refs], width, height, p.config.media.seed
        )
        p._job(key, graph)
