"""Generate and cache the four separately rendered views of a scene asset."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from . import protocols
from .storage import fingerprint

LAYOUT_VERSION = "scene-four-views-v1"
VIEW_NAMES = ("master", "reverse", "overview", "topdown")
NODE_NAMES = {
    "reverse": "scene_reverse_view",
    "overview": "scene_overview",
    "topdown": "scene_topdown",
}


def enabled(pipeline) -> bool:
    """Treat a release with no scene-view nodes as a legacy single-image run."""
    release = pipeline.release
    if release is None:
        return False
    nodes = release.manifest.get("nodes", {})
    present = set(NODE_NAMES.values()) & set(nodes)
    if not present:
        return False
    if present != set(NODE_NAMES.values()):
        raise ValueError("Scene four-view prompt release must contain all three view nodes")
    for name in NODE_NAMES.values():
        pipeline.release.text(name)
        entry = nodes[name]
        if (
            entry.get("variables") != ["场景描述"]
            or entry.get("output_contract", {}).get("format") != "single_line_text"
        ):
            raise ValueError(f"Scene view node {name} must accept 场景描述 and return one line")
    if pipeline.image_template.reference_capacity < 3:
        raise ValueError(
            "Scene four-view generation requires an image workflow with 3 reference slots"
        )
    # Merely counting declared slots is insufficient: every loader and target
    # socket must really exist before the first remote generation is submitted.
    pipeline.image_template.prepare(
        "scene preflight",
        reference_images=["preflight-1.png", "preflight-2.png", "preflight-3.png"],
    )
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Pillow is required to compose scene views") from exc
    return True


def _pinned_reference(pipeline, asset):
    refs = pipeline.state.setdefault("asset_generation_refs", {})
    asset_id = asset["asset_id"]
    if asset_id not in refs:
        files = pipeline.library.files(
            asset_id, asset_version=asset.get("asset_version"), role="reference"
        )
        refs[asset_id] = next(
            (
                item
                for item in reversed(files)
                if Path(item["local_path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            ),
            None,
        )
        pipeline._save()
    reference = refs[asset_id]
    if reference is not None:
        pipeline._verify_media(reference)
    return reference


def _cache_prefix(pipeline, reference) -> str:
    style = pipeline.config.style.resolved()
    value = {
        "layout": LAYOUT_VERSION,
        "release": pipeline.release.version,
        "image_workflow": pipeline._workflow_identity(pipeline.image_template),
        "width": pipeline.config.media.asset_width,
        "height": pipeline.config.media.asset_height,
        "seed": pipeline.config.media.seed,
        "prefix": style["prefix"],
        "suffix": style["suffix"],
        "negative": style["image_negative"],
        "negative_override": pipeline.config.style.image_negative,
        "reference_sha256": reference["sha256"] if reference else None,
    }
    return "reference:scene4:" + fingerprint(value)


def _completed_group(pipeline, asset, prefix, desired_group=None):
    """Use only a complete group; never combine views from concurrent runs."""
    groups = {}
    files = pipeline.library.files(asset["asset_id"], asset_version=asset.get("asset_version"))
    marker = prefix + ":"
    grid_order = []
    for item in files:
        role = item.get("role", "")
        if not role.startswith(marker):
            continue
        parts = role[len(marker) :].split(":")
        if len(parts) != 2 or parts[1] not in (*VIEW_NAMES, "grid"):
            continue
        group, view = parts
        if len(group) != 32 or any(ch not in "0123456789abcdef" for ch in group):
            continue
        if desired_group is not None and group != desired_group:
            continue
        groups.setdefault(group, {})[view] = item
        if view == "grid":
            grid_order.append(group)
    for group in reversed(grid_order):
        records = groups[group]
        if all(view in records for view in (*VIEW_NAMES, "grid")):
            for record in records.values():
                pipeline._verify_media(record)
            return group, records
    return None


def _save_mapping(pipeline, asset, reference, group, records):
    references = {
        "master": ["identity"] if reference else [],
        "reverse": ["master"],
        "overview": ["master"],
        "topdown": ["master", "reverse", "overview"],
    }
    mapping = {
        "layout_version": LAYOUT_VERSION,
        "group": group,
        "identity_reference": reference,
        "views": {
            view: {"file": records[view], "references": references[view]} for view in VIEW_NAMES
        },
        "grid": records["grid"],
    }
    pipeline.state.setdefault("scene_views", {})[asset["asset_id"]] = mapping
    pipeline.state.setdefault("asset_media", {})[asset["asset_id"]] = records["grid"]
    pipeline._save()
    return records["grid"]


def _compose_grid(pipeline, asset_key: str, records):
    from PIL import Image

    directory = pipeline.root / "media" / ("scene_" + asset_key)
    if not directory.resolve().is_relative_to(pipeline.root.resolve()):
        raise ValueError("Scene composite directory must remain inside the run")
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(pipeline.root.resolve()):
        raise ValueError("Scene composite directory must remain inside the run")
    images = []
    try:
        for name in VIEW_NAMES:
            with Image.open(records[name]["local_path"]) as original:
                original.load()
                images.append(original.convert("RGB"))
        size = images[0].size
        if any(image.size != size for image in images[1:]):
            raise ValueError("Scene views must have identical image dimensions")
        width, height = size
        grid = Image.new("RGB", (width * 2, height * 2))
        try:
            for index, image in enumerate(images):
                grid.paste(image, ((index % 2) * width, (index // 2) * height))
            fd, temp_name = tempfile.mkstemp(prefix=".scene-grid-", suffix=".png", dir=directory)
            temp = Path(temp_name)
            destination = directory / "scene-grid.png"
            try:
                with os.fdopen(fd, "wb") as target:
                    grid.save(target, format="PNG")
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
            return destination
        finally:
            grid.close()
    finally:
        for image in images:
            image.close()


def generate(pipeline, asset):
    """Return the catalog record for a four-view grid, with all inputs pinned."""
    if asset.get("kind") != "scene":
        raise ValueError("Four-view generation accepts a scene asset only")
    if not enabled(pipeline):
        raise ValueError("The pinned release has no scene four-view nodes; create a new run")
    image_negative = pipeline._style_negative_arguments(pipeline.image_template, "image")
    if image_negative:
        pipeline.image_template.prepare("preflight", **image_negative)
    reference = _pinned_reference(pipeline, asset)
    prefix = _cache_prefix(pipeline, reference)
    asset_id = asset["asset_id"]
    generation = pipeline.state.setdefault("scene_generation", {})
    info = generation.get(asset_id)
    if info and info["cache_prefix"] != prefix:
        raise ValueError("Scene generation inputs changed; create a new run")
    cached = _completed_group(pipeline, asset, prefix, info["group"] if info else None)
    if cached:
        return _save_mapping(pipeline, asset, reference, *cached)

    asset_key = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:24]
    if not info:
        generation[asset_id] = {"group": uuid4().hex, "cache_prefix": prefix}
        pipeline._save()
        info = generation[asset_id]
    group = info["group"]
    prompts = {"master": asset["description"]}
    for view, node in NODE_NAMES.items():
        prompts[view] = pipeline._node(
            f"scene_{asset_key}_{view}",
            node,
            {"场景描述": asset["description"]},
            protocols.single_line,
        )
    records = {}
    for index, view in enumerate(VIEW_NAMES):
        if view == "master":
            references = [reference["local_path"]] if reference else []
        elif view in {"reverse", "overview"}:
            references = [records["master"]["local_path"]]
        else:
            references = [records[name]["local_path"] for name in VIEW_NAMES[:3]]
        graph = pipeline.image_template.prepare(
            pipeline.config.style.compose_image(prompts[view]),
            seed=pipeline.config.media.seed + index,
            width=pipeline.config.media.asset_width,
            height=pipeline.config.media.asset_height,
            reference_images=[pipeline._upload(path) for path in references],
            **image_negative,
        )
        outputs = pipeline._job(f"scene_{asset_key}_{view}", graph)
        records[view] = pipeline.library.link_file(
            asset_id,
            pipeline._first_image(outputs),
            role=f"{prefix}:{group}:{view}",
            asset_version=asset.get("asset_version"),
        )
    composite = _compose_grid(pipeline, asset_key, records)
    records["grid"] = pipeline.library.link_file(
        asset_id, composite, role=f"{prefix}:{group}:grid", asset_version=asset.get("asset_version")
    )
    return _save_mapping(pipeline, asset, reference, group, records)
