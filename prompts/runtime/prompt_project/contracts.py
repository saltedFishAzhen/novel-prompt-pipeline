"""Evaluation uses the same structural and business validators as production."""

from __future__ import annotations

import re
from typing import Any

from pipeline import protocols
from pipeline.assets import bindings, parse_assets
from pipeline.schemas import AssetBinding, AssetDescription


def _data(value):
    return protocols.json_value(value) if isinstance(value, str) else value


def _panels(variables: dict, fallback: list[dict]) -> list[dict]:
    value = variables.get("章节文案")
    if value is not None:
        try:
            value = _data(value)
        except ValueError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            return value["results"]
        if isinstance(value, list):
            return value
    ids = [x["panel_index"] for x in fallback]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate output panel indices")
    return fallback


def _runtime(variables):
    value = variables.get("运行约束", {})
    if value in ("无", "", None):
        return {}
    value = _data(value)
    if not isinstance(value, dict):
        raise ValueError("Runtime constraints must be a JSON object")
    return value


def validate_output(
    text: str, contract: dict[str, Any], variables: dict | None = None, node_id: str | None = None
) -> tuple[bool, str]:
    variables = variables or {}
    try:
        kind = contract.get("format")
        if kind == "single_line_text":
            protocols.single_line(text)
        elif kind == "json_asset_array":
            value = protocols.json_value(text)
            if not isinstance(value, list):
                raise ValueError("Expected a bare asset array")
            names = []
            for item in value:
                names.append(AssetDescription.model_validate(item).name)
            if len(names) != len(set(names)):
                raise ValueError("Duplicate asset names")
            if node_id and node_id.startswith("asset."):
                from .validation import validate_assets

                mode = node_id.split(".", 1)[1]
                source = variables.get("小说原文", "")
                parse_assets(text, mode, source)
                errors = validate_assets(text, mode, source)
                if errors:
                    raise ValueError("; ".join(errors))
        elif kind == "json_object":
            value = protocols.json_value(text)
            required = set(contract.get("required_fields", []))
            if not isinstance(value, dict) or set(value) != required:
                raise ValueError("JSON fields do not exactly match the node contract")
            if required == {"story", "chapter"}:
                protocols.summary(text)
            elif required == {"summary"}:
                protocols.continuity(text)
            elif required == {"results"}:
                protocols.rewrites(text, _panels(variables, value["results"]))
            elif required == {"shots"}:
                protocols.shot_plan(text, _panels(variables, value["shots"]))
            elif required == {"asset_ids", "first_frame_asset_ids"}:
                candidates = _data(variables.get("候选资产", []))
                if "候选资产" in variables:
                    bindings(text, candidates)
                else:
                    selection = AssetBinding.model_validate(value)
                    bindings(text, [{"asset_id": item} for item in selection.asset_ids])
            else:
                raise ValueError("Unknown JSON node contract")
        elif kind in {"record_delimited_text", "single_record_text"}:
            schema = contract.get("record_schema")
            if kind == "record_delimited_text" and schema not in {"storyboard", "storyboard_i2va"}:
                protocols.script(text)
                return True, "Production script validation passed"
            mode = "i2va" if schema == "storyboard_i2va" else "ref2va"
            body = text.replace("\r\n", "\n").strip()
            if kind == "single_record_text":
                parts = protocols.unwrap(body).split("===")
                if len(parts) != 2:
                    raise ValueError("Expected exactly two single-shot fields")
                body = "0" + protocols.FIELD + parts[0].strip() + protocols.FIELD + parts[1].strip()
            rows = re.split(r"\n" + re.escape(protocols.RECORD) + r"\n", body)
            expected = []
            for row in rows:
                fields = row.split(protocols.FIELD)
                if len(fields) != 3 or not fields[0].strip().isdigit():
                    raise ValueError("Expected numbered storyboard records with three fields")
                expected.append({"panel_index": int(fields[0].strip()), "text": "evaluation"})
            runtime = _runtime(variables)
            selected = None
            if "panel_assets" in runtime:
                selected = {}
                for item in expected:
                    index = item["panel_index"]
                    panel = runtime["panel_assets"].get(
                        str(index), runtime["panel_assets"].get(index, {})
                    )
                    first_ids = panel.get("first_frame_asset_ids", [])
                    selected[index] = [
                        dict(a, first_frame=a["asset_id"] in first_ids)
                        for a in panel.get("assets", [])
                    ]
            protocols.storyboards(
                body,
                _panels(variables, expected),
                mode=mode,
                min_seconds=runtime.get("min_seconds", 4),
                max_seconds=runtime.get("max_seconds", 15),
                image_capacity=runtime.get("image", {}).get("reference_capacity", 10),
                video_capacity=1
                if mode == "i2va"
                else 1 + runtime.get("video", {}).get("reference_capacity", 8),
                audio_capacity=0 if mode == "i2va" else len(runtime.get("audio_references", [])),
                selected=selected,
            )
        else:
            raise ValueError(f"Unknown output format: {kind}")
        return True, "Production output validation passed"
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return False, str(exc)
