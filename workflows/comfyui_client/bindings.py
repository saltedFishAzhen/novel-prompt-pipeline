"""Bind one novel-storyboard segment to the H3 ref2va API workflow.

``workflow_bindings.json`` declares where each runtime value lands in
``h3-ref2va-api.json``: the prompt, the seed, the segment duration, the
aspect-ratio selector and the nine reference-image slots. Values the storyboard
package does not supply keep the workflow's own defaults.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .errors import BindingError
from .validation import is_relative_to, validate_remote_path

#: The declaration sits next to the ``workflows`` package.
BINDINGS_FILE = Path(__file__).resolve().parents[1] / "workflow_bindings.json"

_SCALAR_FIELDS = frozenset({"prompt", "seed", "length"})
_VALUE_FIELDS = _SCALAR_FIELDS | {"aspect_ratio", "reference_images"}
_DEFINITION_KEYS = frozenset(
    {
        "schema_version",
        "workflow",
        "bindings",
        "reference_slots",
        "prune",
        "aspect_ratio_input",
        "aspect_ratios",
    }
)
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "any": lambda _: True,
    "str": lambda value: isinstance(value, str),
    "int": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
}


def build_graph(
    values: Mapping[str, Any],
    *,
    bindings_file: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return ``h3-ref2va-api.json`` with one segment's values written in.

    ``values`` holds ``prompt`` (required), ``seed``, ``aspect_ratio`` (a key of
    the workflow's ratio table), ``length`` (seconds, may be fractional) and
    ``reference_images`` (1-9 ComfyUI input-relative names in Picture order).
    Declared slots without a picture are removed, so the server is never asked
    for an image the segment does not have. The loaded workflow is never
    mutated: every call starts from the JSON on disk.
    """
    definition, root = _load_definition(bindings_file)
    return _apply_values(_load_workflow(definition, root), definition, values)


def api_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Return a detached copy of an API-format graph without UI-only metadata.

    Keys starting with ``_`` and the ``definitions`` block that newer ComfyUI
    exports add for subgraphs are dropped. Every remaining key must be a numeric
    node id with a ``class_type`` and an ``inputs`` object.
    """
    if not isinstance(graph, dict):
        raise ValueError("graph must be an object")
    clean: dict[str, Any] = {}
    for raw_id, node in graph.items():
        node_id = str(raw_id)
        if node_id.startswith("_") or node_id == "definitions":
            continue
        if not node_id.isdigit():
            raise ValueError(f"node id {node_id!r} must be numeric (ComfyUI API format)")
        if (
            not isinstance(node, dict)
            or "class_type" not in node
            or not isinstance(node.get("inputs"), dict)
        ):
            raise ValueError(f"node {node_id!r} must contain class_type and an inputs object")
        clean[node_id] = copy.deepcopy(node)
    if not clean:
        raise ValueError("graph contains no API nodes")
    return clean


def _apply_values(
    graph: dict[str, Any], definition: Mapping[str, Any], values: Mapping[str, Any] | None
) -> dict[str, Any]:
    raw = dict(values or {})
    unknown = set(raw) - _VALUE_FIELDS
    if unknown:
        raise BindingError("unsupported value(s): " + ", ".join(sorted(unknown)))
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BindingError("prompt must be a non-empty string")
    seed = raw.get("seed")
    if seed is not None and not _is_int(seed):
        raise BindingError("seed must be an integer")
    length = raw.get("length")
    if length is not None and not _is_positive_number(length):
        raise BindingError("length must be a positive number of seconds")
    reference_images = _remote_names(raw.get("reference_images"), "reference_images")
    if not reference_images:
        raise BindingError("at least one reference image is required")

    bindings = definition["bindings"]
    bound = copy.deepcopy(graph)
    for field, value in (("prompt", prompt), ("seed", seed), ("length", length)):
        if value is not None:
            _bind_scalar(bound, bindings[field], field, value)
    aspect_ratio = raw.get("aspect_ratio")
    if aspect_ratio is not None:
        _bind_aspect_ratio(bound, definition, aspect_ratio)
    removed = _bind_slots(bound, definition.get("reference_slots", []), reference_images)
    removed |= _prune(bound, definition.get("prune", []))
    _assert_no_dangling_links(bound, removed)
    return bound


def _bind_aspect_ratio(
    graph: dict[str, Any], definition: Mapping[str, Any], aspect_ratio: Any
) -> None:
    ratios = definition["aspect_ratios"]
    if not isinstance(aspect_ratio, str) or aspect_ratio not in ratios:
        supported = ", ".join(sorted(str(item) for item in ratios))
        raise BindingError(f"unsupported aspect_ratio {aspect_ratio!r}; supported: {supported}")
    _set_input(graph, definition["aspect_ratio_input"], ratios[aspect_ratio], field="aspect_ratio")


def _bind_scalar(
    graph: dict[str, Any], mappings: Any, field: str, value: Any
) -> None:
    for mapping in mappings:
        expected = mapping.get("type")
        if expected is not None and not _matches_type(value, expected):
            raise BindingError(f"{field} must have type {expected}, got {type(value).__name__}")
        _set_input(graph, mapping, value, field=field)


def _bind_slots(
    graph: dict[str, Any], slots: Any, names: list[str]
) -> set[str]:
    if len(names) > len(slots):
        raise BindingError(
            f"the workflow has {len(slots)} reference image slots; received {len(names)}"
        )
    for index, remote_name in enumerate(names):
        _set_input(graph, slots[index]["load"], remote_name, field=f"reference_images[{index}]")
        for target in slots[index].get("conditioning", []):
            _check_input(graph, target, field=f"reference image slot {index}")
    return _prune(graph, [_slot_prune(slot) for slot in slots[len(names) :]])


def _slot_prune(slot: Mapping[str, Any]) -> dict[str, Any]:
    return {"node_id": slot["load"]["node_id"], "conditioning": slot.get("conditioning", [])}


def _prune(graph: dict[str, Any], entries: Any) -> set[str]:
    """Drop loader nodes that the package does not fill, and their links."""
    removed: set[str] = set()
    for entry in entries:
        for target in entry.get("conditioning", []):
            node = graph.get(str(target["node_id"]))
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"].pop(target["input"], None)
        node_id = str(entry["node_id"])
        if graph.pop(node_id, None) is not None:
            removed.add(node_id)
    return removed


def _assert_no_dangling_links(graph: Mapping[str, Any], removed: set[str]) -> None:
    """Fail loudly instead of shipping a graph with links to pruned nodes."""
    if not removed:
        return
    for node_id, node in graph.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0] in removed
            ):
                raise BindingError(
                    f"removed node {value[0]!r} is still referenced by {node_id}.{input_name}; "
                    "declare that input under the slot's conditioning"
                )


def _set_input(graph: dict[str, Any], mapping: Any, value: Any, *, field: str) -> None:
    if not isinstance(mapping, Mapping):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    if not node_id or not isinstance(input_name, str) or not input_name:
        raise BindingError(f"mapping for {field!r} requires node_id and input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    inputs = node["inputs"]
    if input_name not in inputs:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")
    if not _matches_type(value, mapping.get("type", "any")):
        raise BindingError(f"mapped value for {field!r} has an unexpected type")
    inputs[input_name] = value


def _check_input(graph: dict[str, Any], mapping: Any, *, field: str) -> None:
    if not isinstance(mapping, Mapping):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    if not isinstance(input_name, str) or input_name not in node["inputs"]:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")


def _matches_type(value: Any, type_name: Any) -> bool:
    if type_name is None:
        return True
    check = _TYPE_CHECKS.get(type_name) if isinstance(type_name, str) else None
    if check is None:
        raise BindingError(f"unsupported binding type: {type_name!r}")
    return bool(check(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _remote_names(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise BindingError(f"{field} must be a list of ComfyUI-relative names")
    names: list[str] = []
    for index, item in enumerate(value):
        try:
            names.append(validate_remote_path(item, f"{field}[{index}]"))
        except ValueError as exc:
            raise BindingError(str(exc)) from None
    return names


def _load_definition(
    bindings_file: str | os.PathLike[str] | None,
) -> tuple[dict[str, Any], Path]:
    """Read and validate ``workflow_bindings.json``; returned per call, never cached."""
    path = Path(bindings_file) if bindings_file is not None else BINDINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load binding file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BindingError(f"binding file {path} must contain a JSON object")
    unknown = set(data) - _DEFINITION_KEYS
    if unknown:
        raise BindingError("binding file has unsupported keys: " + ", ".join(sorted(unknown)))
    if data.get("schema_version") != 1:
        raise BindingError(f"binding file {path} must declare schema_version 1")
    _relative_workflow(data.get("workflow"))
    bindings = data.get("bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        raise BindingError("binding file declares no bindings")
    for field, mappings in bindings.items():
        if field not in _SCALAR_FIELDS:
            raise BindingError(f"binding file declares unsupported binding {field!r}")
        _validate_mappings(mappings, f"binding {field!r}")
    if "prompt" not in bindings:
        raise BindingError("binding file must declare the prompt binding")
    _validate_slots(data.get("reference_slots"))
    _validate_prune(data.get("prune"))
    _validate_aspect_ratio(data)
    return data, path.expanduser().resolve().parent


def _validate_aspect_ratio(data: Mapping[str, Any]) -> None:
    ratios = data.get("aspect_ratios")
    target = data.get("aspect_ratio_input")
    if ratios is None or target is None:
        raise BindingError("binding file must declare aspect_ratios and aspect_ratio_input")
    if not isinstance(ratios, Mapping) or not ratios:
        raise BindingError("aspect_ratios must be a non-empty object")
    for ratio, label in ratios.items():
        _parse_ratio(ratio)
        if not isinstance(label, str) or not label:
            raise BindingError(f"aspect_ratios[{ratio!r}] must be a non-empty label")
    _validate_mapping(target, "aspect_ratio_input")


def _parse_ratio(ratio: Any) -> None:
    try:
        horizontal, vertical = str(ratio).split(":")
        valid = int(horizontal) > 0 and int(vertical) > 0
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise BindingError(f"invalid aspect ratio {ratio!r}; expected 'width:height'")


def _validate_slots(value: Any) -> None:
    if not isinstance(value, list):
        raise BindingError("reference_slots must be a list")
    for index, slot in enumerate(value):
        field = f"reference_slots[{index}]"
        if not isinstance(slot, Mapping):
            raise BindingError(f"{field} must be an object")
        unknown = set(slot) - {"load", "conditioning"}
        if unknown:
            raise BindingError(f"{field} has unsupported keys: " + ", ".join(sorted(unknown)))
        _validate_mapping(slot.get("load"), f"{field}.load")
        targets = slot.get("conditioning", [])
        if not isinstance(targets, list):
            raise BindingError(f"{field}.conditioning must be a list")
        for target_index, target in enumerate(targets):
            _validate_mapping(target, f"{field}.conditioning[{target_index}]", allow_type=False)


def _validate_prune(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise BindingError("prune must be a list")
    for index, entry in enumerate(value):
        field = f"prune[{index}]"
        if not isinstance(entry, Mapping):
            raise BindingError(f"{field} must be an object")
        unknown = set(entry) - {"node_id", "conditioning"}
        if unknown:
            raise BindingError(f"{field} has unsupported keys: " + ", ".join(sorted(unknown)))
        node_id = entry.get("node_id")
        if isinstance(node_id, bool) or not isinstance(node_id, (str, int)) or str(node_id) == "":
            raise BindingError(f"{field} requires a node_id")
        targets = entry.get("conditioning", [])
        if not isinstance(targets, list):
            raise BindingError(f"{field}.conditioning must be a list")
        for target_index, target in enumerate(targets):
            _validate_mapping(target, f"{field}.conditioning[{target_index}]", allow_type=False)


def _validate_mappings(mappings: Any, field: str) -> None:
    if not isinstance(mappings, list) or not mappings:
        raise BindingError(f"{field} must be a non-empty list")
    for index, mapping in enumerate(mappings):
        _validate_mapping(mapping, f"{field}[{index}]")


def _validate_mapping(mapping: Any, field: str, *, allow_type: bool = True) -> None:
    if not isinstance(mapping, Mapping):
        raise BindingError(f"{field} must be an object")
    allowed = {"node_id", "input"} | ({"type"} if allow_type else set())
    unknown = set(mapping) - allowed
    if unknown:
        raise BindingError(f"{field} has unsupported keys: " + ", ".join(sorted(unknown)))
    node_id = mapping.get("node_id")
    if isinstance(node_id, bool) or not isinstance(node_id, (str, int)) or str(node_id) == "":
        raise BindingError(f"{field} requires a node_id")
    input_name = mapping.get("input")
    if not isinstance(input_name, str) or not input_name:
        raise BindingError(f"{field} requires an input name")
    if mapping.get("type") is not None and mapping["type"] not in _TYPE_CHECKS:
        raise BindingError(f"{field} has unsupported type {mapping['type']!r}")


def _load_workflow(definition: Mapping[str, Any], root: Path) -> dict[str, Any]:
    target = (root / _relative_workflow(definition.get("workflow"))).resolve()
    if not is_relative_to(target, root):
        raise BindingError(f"workflow escapes {root}")
    try:
        graph = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load workflow JSON {target}: {exc}") from exc
    if not isinstance(graph, dict) or not graph:
        raise BindingError(f"workflow {target} must contain a non-empty JSON object")
    return graph


def _relative_workflow(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise BindingError("binding file does not declare a workflow")
    windows, posix = PureWindowsPath(value), PurePosixPath(value)
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or ".." in windows.parts
        or ".." in posix.parts
    ):
        raise BindingError("workflow must be a relative path")
    return value
