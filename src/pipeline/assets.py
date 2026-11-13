from __future__ import annotations

import hashlib

from pydantic import TypeAdapter

from .protocols import json_value
from .schemas import AssetBinding, AssetDescription

KINDS = ("character", "scene", "prop")
_ASSET_LIST = TypeAdapter(list[AssetDescription])


def parse_assets(text: str, kind: str, source: str) -> list[dict]:
    data = _ASSET_LIST.validate_python(json_value(text))
    seen = set()
    result = []
    for asset in data:
        item = asset.model_dump()
        if any(not v.strip() for v in item.values()):
            raise ValueError("Asset fields must be nonempty strings")
        if item["name"] in seen:
            raise ValueError("Duplicate asset name in category")
        aliases = [x.strip() for x in item["aliases"].split(",")]
        if not any(a and a in source for a in aliases):
            raise ValueError(f"Asset has no original source alias: {item['name']}")
        seen.add(item["name"])
        asset_id = kind + "_" + hashlib.sha256(item["name"].encode("utf-8")).hexdigest()[:16]
        result.append(dict(item, asset_id=asset_id, kind=kind))
    return result


def bindings(text: str, assets: list[dict]) -> list[dict]:
    data = AssetBinding.model_validate(json_value(text))
    ids = data.asset_ids
    if len(ids) != len(set(ids)):
        raise ValueError("Asset IDs must be distinct strings")
    if not set(ids).issubset({a["asset_id"] for a in assets}):
        raise ValueError("Unknown asset ID; use only provided candidates")
    first = data.first_frame_asset_ids if "first_frame_asset_ids" in data.model_fields_set else ids
    if len(first) != len(set(first)) or not set(first).issubset(ids):
        raise ValueError("first_frame_asset_ids must be a distinct subset of asset_ids")
    # Canonical registry order is character -> scene -> prop, then extraction order.
    return [dict(a, first_frame=a["asset_id"] in first) for a in assets if a["asset_id"] in ids]
