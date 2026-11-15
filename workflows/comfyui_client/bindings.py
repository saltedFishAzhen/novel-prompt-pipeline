"""Workflow profile loading, validation and API-graph binding.

A profile declares where each runtime value lands in a specific API-format
workflow. Profiles are data, but the schema is validated eagerly so a typo in
``workflow_bindings.json`` fails loudly instead of silently doing nothing.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from .errors import BindingError
from .validation import is_relative_to, validate_remote_path

#: Wheel-bundled copy wins; the source checkout is the fallback.
PACKAGED_BINDINGS = Path(__file__).resolve().parent / "_data" / "workflow_bindings.json"
SOURCE_BINDINGS = Path(__file__).resolve().parents[1] / "workflow_bindings.json"

#: Values a caller may pass to ``bind_graph``/``build_graph``.
VALUE_FIELDS = frozenset(
    {
        "prompt",
        "seed",
        "width",
        "height",
        "length",
        "first_frame",
        "negative_prompt",
        "reference_images",
        "reference_audios",
    }
)
_BINDABLE_FIELDS = VALUE_FIELDS - {"first_frame", "reference_images", "reference_audios"}

_PROFILE_KEYS = frozenset(
    {
        "workflow",
        "bindings",
        "first_frame",
        "requires_first_frame",
        "requires_reference_images",
        "reference_slots",
        "audio_slots",
        "capabilities",
        "explicit_size_mode",
        "explicit_aspect_ratios",
        "aspect_ratio_input",
        "aspect_ratio_tolerance",
        "note",
    }
)
_CAPABILITY_KEYS = frozenset(
    {
        "has_first_frame",
        "requires_first_frame",
        "requires_reference_images",
        "reference_capacity",
        "audio_reference_capacity",
        "supports_prompt",
        "supports_seed",
        "supports_width",
        "supports_height",
        "supports_length",
        "supports_negative_prompt",
        "supports_reference_images",
        "supports_reference_audios",
        "note",
    }
)
_FIRST_FRAME_HINT = "the opening frame image"
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "any": lambda _: True,
    "str": lambda value: isinstance(value, str),
    "int": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "bool": lambda value: isinstance(value, bool),
}


def default_bindings_file() -> Path:
    """Return the bindings file bundled with the wheel, else the source copy."""
    return PACKAGED_BINDINGS if PACKAGED_BINDINGS.is_file() else SOURCE_BINDINGS


def bind_graph(
    graph: dict[str, Any],
    profile: str | Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
    *,
    bindings_file: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``graph`` with ``values`` applied through a profile.

    ``values`` accepts ``prompt`` (required string), ``seed``, ``width``,
    ``height``, ``length``, ``first_frame``, ``negative_prompt``,
    ``reference_images`` and ``reference_audios``. Media entries are ComfyUI
    input-relative names, normally returned by
    :meth:`ComfyUIClient.upload_file`. ``width``/``height`` are positive
    integers; ``length`` is a positive number of seconds and may be fractional
    because the duration inputs are floats. Unused reference slots are dropped
    so their loader nodes never reach the server. The source graph is not
    mutated.

    Which of ``first_frame`` / ``reference_images`` a profile uses is declared
    by the profile. ``i2va`` takes ``first_frame`` as its real opening frame and
    has no reference slots; ``ref2va`` takes 1-9 ``reference_images`` (Pictures 1
    to 9) and rejects ``first_frame``. Profiles may also require at least one
    reference image (``requires_reference_images``) instead of a first frame.
    """
    if not isinstance(graph, dict) or not graph:
        raise BindingError("workflow graph must be a non-empty object")
    name, selected = _resolve_profile(profile, bindings_file)
    return _apply_values(graph, selected, name, values)


def build_graph(
    profile: str | Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
    *,
    bindings_file: str | os.PathLike[str] | None = None,
    workflows_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the profile's API graph and return it with ``values`` bound."""
    name, selected = _resolve_profile(profile, bindings_file)
    return _apply_values(_load_workflow(selected, workflows_dir), selected, name, values)


def api_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Return a detached copy of an API-format graph without UI-only metadata.

    Keys starting with ``_`` and the ``definitions`` block that newer ComfyUI
    exports add for subgraphs are dropped, so binding and submission never
    mutate the caller's dict. Every remaining key must be a numeric node id with
    a ``class_type`` and an ``inputs`` object; only flat graphs are supported.
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
    graph: dict[str, Any],
    profile: Mapping[str, Any],
    name: str,
    values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(values or {})
    unknown = set(raw) - VALUE_FIELDS
    if unknown:
        raise BindingError("unsupported workflow value(s): " + ", ".join(sorted(unknown)))
    bindings = profile.get("bindings", {})
    if not isinstance(bindings, Mapping):
        raise BindingError("profile bindings must be an object")
    prompt = raw.get("prompt")
    if prompt is None:
        # Only profiles that declare a prompt binding require one; a profile
        # without it stays usable for seed/size/media-only calls.
        if "prompt" in bindings:
            raise BindingError(f"profile {name!r} requires a prompt")
    elif not isinstance(prompt, str):
        raise BindingError("prompt must be a string")
    for field in ("width", "height"):
        value = raw.get(field)
        if value is not None and not _is_positive_int(value):
            raise BindingError(f"{field} must be a positive integer")
    # Profiles type `length` as a number and the workflow inputs are floats.
    length = raw.get("length")
    if length is not None and not _is_positive_number(length):
        raise BindingError("length must be a positive number")
    first_frame = raw.get("first_frame")
    if first_frame is not None:
        first_frame = _remote_name(first_frame, "first_frame")
    reference_images = _remote_names(raw.get("reference_images"), "reference_images")
    reference_audios = _remote_names(raw.get("reference_audios"), "reference_audios")

    if first_frame is not None and profile.get("first_frame") is None:
        raise BindingError(f"profile {name!r} does not support first_frame")
    if profile.get("requires_first_frame") and not first_frame:
        raise BindingError(f"profile {name!r} requires first_frame ({_FIRST_FRAME_HINT})")
    if profile.get("requires_reference_images") and not reference_images:
        raise BindingError(f"profile {name!r} requires at least one reference image")

    bound = copy.deepcopy(graph)
    for field in ("prompt", "seed", "negative_prompt", "length"):
        value = raw.get(field)
        if value is not None:
            _bind_scalar(bound, profile, name, field, value)
    _bind_size(bound, profile, name, raw.get("width"), raw.get("height"))
    if first_frame is not None:
        _bind_scalar(bound, profile, name, "first_frame", first_frame)
    _bind_slots(bound, profile.get("reference_slots", []), reference_images, kind="reference image")
    _bind_slots(bound, profile.get("audio_slots", []), reference_audios, kind="reference audio")
    return bound


def _bind_size(
    graph: dict[str, Any],
    profile: Mapping[str, Any],
    name: str,
    width: int | None,
    height: int | None,
) -> None:
    """Bind width/height, honouring an explicit size mode when one is declared."""
    mode = profile.get("explicit_size_mode")
    if mode is None or (width is None and height is None):
        for field, value in (("width", width), ("height", height)):
            if value is not None:
                _bind_scalar(graph, profile, name, field, value)
        return
    if not isinstance(mode, Mapping):
        raise BindingError("explicit_size_mode must be an object")
    width_input = mode.get("width_input", "target_width")
    height_input = mode.get("height_input", "target_height")
    # Read the workflow defaults before touching the graph, so the ratio check
    # never depends on the order in which individual dimensions were bound.
    size_inputs = _node_inputs(graph, str(mode.get("node_id", "")), "explicit_size_mode")
    for declared in (width_input, height_input):
        if declared not in size_inputs:
            raise BindingError(
                f"explicit_size_mode declares {declared!r}, which node "
                f"{mode.get('node_id')!r} does not expose"
            )
    bindings = profile.get("bindings", {})
    for field in ("width", "height"):
        if not isinstance(bindings, Mapping) or field not in bindings:
            raise BindingError(
                f"profile {name!r} declares explicit_size_mode but no {field!r} binding"
            )
    explicit_width = width is not None
    explicit_height = height is not None
    effective_width = width if width is not None else size_inputs.get(width_input)
    effective_height = height if height is not None else size_inputs.get(height_input)
    for field, value in (("width", effective_width), ("height", effective_height)):
        if not _is_positive_int(value):
            raise BindingError(f"explicit {field} must be a positive integer")
    _set_input(graph, mode, mode.get("value"), field="explicit_size_mode")
    _bind_scalar(graph, profile, name, "width", effective_width)
    _bind_scalar(graph, profile, name, "height", effective_height)
    _bind_aspect_ratio(
        graph,
        profile,
        int(effective_width),
        int(effective_height),
        defaulted_width=not explicit_width,
        defaulted_height=not explicit_height,
    )


def _bind_aspect_ratio(
    graph: dict[str, Any],
    profile: Mapping[str, Any],
    width: int,
    height: int,
    *,
    defaulted_width: bool = False,
    defaulted_height: bool = False,
) -> None:
    ratios = profile.get("explicit_aspect_ratios")
    target = profile.get("aspect_ratio_input")
    if ratios is None:
        return
    if not isinstance(ratios, Mapping) or not ratios:
        raise BindingError("explicit_aspect_ratios must be a non-empty mapping")
    tolerance = profile.get("aspect_ratio_tolerance", 1.03)
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance < 1:
        raise BindingError("aspect_ratio_tolerance must be a number >= 1")
    requested = width / height
    best: tuple[float, str] | None = None
    for ratio, label in ratios.items():
        reference = _parse_ratio(ratio)
        if not isinstance(label, str) or not label:
            raise BindingError(f"invalid label for aspect ratio {ratio!r}")
        distance = max(requested / reference, reference / requested)
        if best is None or distance < best[0]:
            best = (distance, label)
    if best is None:
        raise BindingError("explicit_aspect_ratios must be a non-empty mapping")
    if best[0] > tolerance:
        supported = ", ".join(sorted(str(ratio) for ratio in ratios))
        hint = ""
        if defaulted_width or defaulted_height:
            source = "width" if defaulted_width else "height"
            hint = (
                f"; the {source} came from the workflow default, so pass both width "
                "and height to keep the aspect ratio consistent"
            )
        raise BindingError(
            f"explicit dimensions {width}x{height} do not match a supported aspect "
            f"ratio ({supported}){hint}"
        )
    _set_input(graph, target, best[1], field="aspect_ratio")


def _bind_scalar(
    graph: dict[str, Any], profile: Mapping[str, Any], name: str, field: str, value: Any
) -> None:
    if field == "first_frame":
        mappings: Any = [profile.get("first_frame")]
    else:
        bindings = profile.get("bindings", {})
        if not isinstance(bindings, Mapping):
            raise BindingError("profile bindings must be an object")
        mappings = bindings.get(field)
    if mappings is None:
        raise BindingError(f"profile {name!r} does not support {field!r}")
    if not isinstance(mappings, list) or not mappings:
        raise BindingError(f"profile binding {field!r} must be a non-empty list")
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise BindingError(f"invalid mapping for {field!r}")
        expected = mapping.get("type", "any")
        if not _matches_type(value, expected):
            raise BindingError(f"{field} must have type {expected}, got {type(value).__name__}")
        _set_input(graph, mapping, value, field=field)


def _bind_slots(graph: dict[str, Any], slots: Any, names: list[str], *, kind: str) -> None:
    if not isinstance(slots, list):
        raise BindingError(f"{kind} slots must be a list")
    if len(names) > len(slots):
        raise BindingError(f"profile supports {len(slots)} {kind}(s); received {len(names)}")
    for index, remote_name in enumerate(names):
        load = _slot_load(slots[index], kind, index)
        _set_input(graph, load, remote_name, field=f"{kind}[{index}]")
        for target in _slot_targets(slots[index], kind, index):
            _check_input(graph, target, field=f"{kind} slot {index}")
    removed: set[str] = set()
    for index in range(len(names), len(slots)):
        for target in _slot_targets(slots[index], kind, index):
            if not isinstance(target, Mapping):
                continue
            node = graph.get(str(target.get("node_id", "")))
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"].pop(target.get("input"), None)
        load = _slot_load(slots[index], kind, index)
        load_id = str(load.get("node_id", ""))
        graph.pop(load_id, None)
        removed.add(load_id)
    _assert_no_dangling_links(graph, removed)


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
                    f"profile removes node {value[0]!r} but {node_id}.{input_name} still "
                    "references it; list that input under the slot's conditioning"
                )


def _slot_load(slot: Any, kind: str, index: int) -> Mapping[str, Any]:
    if not isinstance(slot, Mapping) or not isinstance(slot.get("load"), Mapping):
        raise BindingError(f"{kind} slot {index} needs a load mapping")
    return slot["load"]


def _slot_targets(slot: Any, kind: str, index: int) -> list[Any]:
    targets = slot.get("conditioning", []) if isinstance(slot, Mapping) else []
    if not isinstance(targets, list):
        raise BindingError(f"{kind} slot {index} conditioning must be a list")
    return targets


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


def _node_inputs(graph: dict[str, Any], node_id: str, field: str) -> Mapping[str, Any]:
    node = graph.get(node_id)
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, Mapping):
        raise BindingError(f"{field} targets missing node {node_id!r}")
    return cast(Mapping[str, Any], inputs)


def _matches_type(value: Any, type_name: Any) -> bool:
    check = _TYPE_CHECKS.get(type_name) if isinstance(type_name, str) else None
    if check is None:
        raise BindingError(f"unsupported binding type: {type_name!r}")
    return bool(check(value))


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _parse_ratio(ratio: Any) -> float:
    try:
        horizontal, vertical = str(ratio).split(":")
        reference = int(horizontal) / int(vertical)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise BindingError(f"invalid aspect ratio {ratio!r}") from exc
    if reference <= 0:
        raise BindingError(f"invalid aspect ratio {ratio!r}")
    return reference


def _remote_name(value: Any, field: str) -> str:
    try:
        return validate_remote_path(value, field)
    except ValueError as exc:
        raise BindingError(str(exc)) from None


def _remote_names(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BindingError(f"{field} must be a list of ComfyUI-relative names")
    return [_remote_name(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _load_bindings(bindings_file: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Read and validate every profile.

    Deliberately uncached: profiles are read once per bind call, so editing the
    file takes effect immediately and a broken profile keeps failing loudly
    instead of being masked by a warm cache.
    """
    path = Path(bindings_file) if bindings_file is not None else default_bindings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load binding profiles {path}: {exc}") from exc
    schema = data.get("schema_version") if isinstance(data, dict) else None
    if schema is not None and schema != 1:
        raise BindingError(f"binding file {path} uses unsupported schema_version {schema!r}")
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise BindingError(f"binding file {path} declares no profiles")
    for profile_name, profile in profiles.items():
        _validate_profile(profile_name, profile)
    return profiles


def _resolve_profile(
    profile: str | Mapping[str, Any], bindings_file: str | os.PathLike[str] | None
) -> tuple[str, dict[str, Any]]:
    if isinstance(profile, Mapping):
        selected = dict(profile)
        _validate_profile("custom", selected)
        return "custom", selected
    if not isinstance(profile, str) or not profile:
        raise BindingError("profile must be a profile name or object")
    profiles = _load_bindings(bindings_file)
    selected = profiles.get(profile)
    if not isinstance(selected, dict):
        known = ", ".join(sorted(profiles))
        raise BindingError(f"unknown workflow profile: {profile} (known: {known})")
    return profile, selected


def _load_workflow(
    profile: Mapping[str, Any], workflows_dir: str | os.PathLike[str] | None
) -> dict[str, Any]:
    workflow_name = _relative_workflow(profile.get("workflow"), "profile")
    root = (
        Path(workflows_dir).expanduser().resolve()
        if workflows_dir is not None
        else default_bindings_file().parent
    )
    target = (root / workflow_name).resolve()
    if not is_relative_to(target, root):
        raise BindingError(f"profile workflow escapes {root}")
    try:
        graph = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot load workflow JSON {target}: {exc}") from exc
    if not isinstance(graph, dict) or not graph:
        raise BindingError(f"workflow {target} must contain a non-empty JSON object")
    return graph


def _relative_workflow(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BindingError(f"{name} does not declare a workflow")
    windows, posix = PureWindowsPath(value), PurePosixPath(value)
    if (
        windows.drive
        or windows.is_absolute()
        or posix.is_absolute()
        or ".." in windows.parts
        or ".." in posix.parts
    ):
        raise BindingError("profile workflow must be a relative path")
    return value


def _validate_profile(name: str, profile: Any) -> None:
    if not isinstance(profile, Mapping):
        raise BindingError(f"profile {name!r} must be an object")
    unknown = set(profile) - _PROFILE_KEYS
    if unknown:
        raise BindingError(f"profile {name!r} has unsupported keys: " + ", ".join(sorted(unknown)))
    workflow = profile.get("workflow")
    if workflow is not None:
        _relative_workflow(workflow, f"profile {name!r}")
    if profile.get("requires_first_frame") and profile.get("first_frame") is None:
        raise BindingError(f"profile {name!r} requires first_frame but declares no mapping")
    if profile.get("requires_reference_images") and not profile.get("reference_slots"):
        raise BindingError(
            f"profile {name!r} requires reference images but declares no reference slot"
        )
    bindings = profile.get("bindings", {})
    if not isinstance(bindings, Mapping):
        raise BindingError(f"profile {name!r} bindings must be an object")
    for field, mappings in bindings.items():
        if field not in _BINDABLE_FIELDS:
            raise BindingError(f"profile {name!r} declares unknown binding {field!r}")
        _validate_mappings(mappings, f"profile {name!r} binding {field!r}")
    first_frame = profile.get("first_frame")
    if first_frame is not None:
        _validate_mapping(first_frame, f"profile {name!r} first_frame")
    slots = _validate_slots(profile.get("reference_slots"), f"profile {name!r} reference_slots")
    audio_slots = _validate_slots(profile.get("audio_slots"), f"profile {name!r} audio_slots")
    mode = profile.get("explicit_size_mode")
    if mode is not None:
        _validate_mapping(
            mode,
            f"profile {name!r} explicit_size_mode",
            extra={"value", "width_input", "height_input"},
        )
        if not isinstance(mode.get("value"), str) or not mode["value"]:
            raise BindingError(f"profile {name!r} explicit_size_mode needs a string value")
        for key in ("width_input", "height_input"):
            if key in mode and (not isinstance(mode[key], str) or not mode[key]):
                raise BindingError(f"profile {name!r} explicit_size_mode {key} must be a name")
    _validate_aspect_ratio_settings(name, profile)
    capabilities = profile.get("capabilities")
    if capabilities is not None:
        _validate_capabilities(name, profile, bindings, slots, audio_slots, capabilities)


def _validate_aspect_ratio_settings(name: str, profile: Mapping[str, Any]) -> None:
    ratios = profile.get("explicit_aspect_ratios")
    target = profile.get("aspect_ratio_input")
    if ratios is None and target is None:
        return
    if ratios is None or target is None:
        raise BindingError(
            f"profile {name!r} must declare explicit_aspect_ratios and aspect_ratio_input together"
        )
    if not isinstance(ratios, Mapping) or not ratios:
        raise BindingError(f"profile {name!r} explicit_aspect_ratios must be a non-empty mapping")
    for ratio, label in ratios.items():
        _parse_ratio(ratio)
        if not isinstance(label, str) or not label:
            raise BindingError(f"profile {name!r} has an invalid label for ratio {ratio!r}")
    _validate_mapping(target, f"profile {name!r} aspect_ratio_input")
    tolerance = profile.get("aspect_ratio_tolerance", 1.03)
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance < 1:
        raise BindingError(f"profile {name!r} aspect_ratio_tolerance must be a number >= 1")


def _validate_capabilities(
    name: str,
    profile: Mapping[str, Any],
    bindings: Mapping[str, Any],
    slots: list[Any],
    audio_slots: list[Any],
    capabilities: Any,
) -> None:
    if not isinstance(capabilities, Mapping):
        raise BindingError(f"profile {name!r} capabilities must be an object")
    unknown = set(capabilities) - _CAPABILITY_KEYS
    if unknown:
        raise BindingError(
            f"profile {name!r} capabilities has unsupported keys: " + ", ".join(sorted(unknown))
        )
    expectations = (
        ("reference_capacity", len(slots)),
        ("audio_reference_capacity", len(audio_slots)),
    )
    for key, actual in expectations:
        declared = capabilities.get(key)
        if declared is not None and declared != actual:
            raise BindingError(
                f"profile {name!r} declares {key}={declared} but has {actual} slot(s)"
            )
    has_first_frame = capabilities.get("has_first_frame")
    if has_first_frame is not None and bool(has_first_frame) != (
        profile.get("first_frame") is not None
    ):
        raise BindingError(f"profile {name!r} has_first_frame disagrees with its first_frame map")
    requires = capabilities.get("requires_first_frame")
    if requires is not None and bool(requires) != bool(profile.get("requires_first_frame")):
        raise BindingError(
            f"profile {name!r} capabilities.requires_first_frame disagrees with the profile"
        )
    requires_images = capabilities.get("requires_reference_images")
    if requires_images is not None and bool(requires_images) != bool(
        profile.get("requires_reference_images")
    ):
        raise BindingError(
            f"profile {name!r} capabilities.requires_reference_images disagrees with the profile"
        )
    for cap_key, declared_slots, label in (
        ("supports_reference_images", slots, "reference"),
        ("supports_reference_audios", audio_slots, "audio"),
    ):
        claim = capabilities.get(cap_key)
        if claim is not None and bool(claim) != bool(declared_slots):
            raise BindingError(f"profile {name!r} {cap_key} disagrees with its {label} slots")
    for key, claim in capabilities.items():
        if not key.startswith("supports_"):
            continue
        field = key.removeprefix("supports_")
        if field in {"reference_images", "reference_audios"}:
            continue
        if field not in _BINDABLE_FIELDS:
            raise BindingError(f"profile {name!r} has unsupported capability {key!r}")
        if bool(claim) and field not in bindings:
            raise BindingError(f"profile {name!r} claims {key} but declares no {field!r} binding")
        if claim is False and field in bindings:
            raise BindingError(
                f"profile {name!r} declares a {field!r} binding but claims {key}=false"
            )


def _validate_slots(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BindingError(f"{field} must be a list")
    for index, slot in enumerate(value):
        if not isinstance(slot, Mapping):
            raise BindingError(f"{field}[{index}] must be an object")
        unknown = set(slot) - {"load", "conditioning"}
        if unknown:
            raise BindingError(
                f"{field}[{index}] has unsupported keys: " + ", ".join(sorted(unknown))
            )
        _validate_mapping(slot.get("load"), f"{field}[{index}].load")
        targets = slot.get("conditioning", [])
        if not isinstance(targets, list):
            raise BindingError(f"{field}[{index}].conditioning must be a list")
        for target_index, target in enumerate(targets):
            _validate_mapping(
                target, f"{field}[{index}].conditioning[{target_index}]", allow_type=False
            )
    return value


def _validate_mappings(mappings: Any, field: str) -> None:
    if not isinstance(mappings, list) or not mappings:
        raise BindingError(f"{field} must be a non-empty list")
    for index, mapping in enumerate(mappings):
        _validate_mapping(mapping, f"{field}[{index}]")


def _validate_mapping(
    mapping: Any,
    field: str,
    *,
    allow_type: bool = True,
    extra: set[str] | frozenset[str] = frozenset(),
) -> None:
    if not isinstance(mapping, Mapping):
        raise BindingError(f"{field} must be an object")
    allowed = {"node_id", "input"} | set(extra)
    if allow_type:
        allowed.add("type")
    unknown = set(mapping) - allowed
    if unknown:
        raise BindingError(f"{field} has unsupported keys: " + ", ".join(sorted(unknown)))
    node_id = mapping.get("node_id")
    if isinstance(node_id, bool) or not isinstance(node_id, (str, int)) or str(node_id) == "":
        raise BindingError(f"{field} requires a node_id")
    input_name = mapping.get("input")
    if not isinstance(input_name, str) or not input_name:
        raise BindingError(f"{field} requires an input name")
    if "type" in mapping:
        _check_type_name(mapping["type"], field)


def _check_type_name(type_name: Any, field: str) -> None:
    if not isinstance(type_name, str) or type_name not in _TYPE_CHECKS:
        raise BindingError(f"{field} has unsupported type {type_name!r}")
