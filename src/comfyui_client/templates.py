"""Safe parameter binding for the checked-in ComfyUI API graphs."""

from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from novel_prompt_pipeline.resources import workflow_root

WORKFLOWS_DIR = workflow_root()
BINDINGS_FILE = WORKFLOWS_DIR / "workflow_bindings.json"


class BindingError(ValueError):
    """A requested runtime value cannot be applied to this graph/profile."""


class WorkflowTemplate:
    """A workflow graph plus an explicit profile describing mutable inputs.

    Use :meth:`from_file` with a profile name or :func:`get_default_template`.
    ``prepare`` returns an independent graph; neither this template nor its
    source JSON file is mutated.
    """

    def __init__(
        self, graph: dict[str, Any], profile: dict[str, Any], *, name: str = "custom"
    ) -> None:
        if not isinstance(graph, dict) or not graph:
            raise BindingError("workflow graph must be a non-empty object")
        if not isinstance(profile, dict):
            raise BindingError("workflow profile must be an object")
        self.name = name
        self._graph = copy.deepcopy(graph)
        self._profile = copy.deepcopy(profile)
        raw_capabilities = profile.get("capabilities", {})
        if not isinstance(raw_capabilities, dict):
            raise BindingError("profile capabilities must be an object")
        self.capabilities = {
            **raw_capabilities,
            "reference_capacity": self.reference_capacity,
            "audio_reference_capacity": self.audio_reference_capacity,
            "has_first_frame": self.has_first_frame,
        }

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        profile: str | dict[str, Any],
        *,
        bindings_file: str | Path | None = None,
    ) -> WorkflowTemplate:
        path = Path(path)
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BindingError(f"cannot load workflow JSON {path}: {exc}") from exc
        selected = profile
        name = path.stem
        if isinstance(profile, str):
            profile_path = Path(bindings_file) if bindings_file else BINDINGS_FILE
            try:
                profiles = json.loads(profile_path.read_text(encoding="utf-8")).get("profiles", {})
            except (OSError, json.JSONDecodeError) as exc:
                raise BindingError(f"cannot load binding profiles {profile_path}: {exc}") from exc
            if profile not in profiles:
                raise BindingError(f"unknown workflow profile: {profile}")
            selected = profiles[profile]
            expected = selected.get("workflow")
            if expected and Path(expected).name.casefold() != path.name.casefold():
                raise BindingError(
                    f"profile {profile!r} expects workflow {expected!r}, got {path.name!r}"
                )
            name = profile
        return cls(graph, selected, name=name)

    @classmethod
    def from_profile(
        cls,
        profile: str,
        *,
        bindings_file: str | Path | None = None,
        workflows_dir: str | Path | None = None,
    ) -> WorkflowTemplate:
        profile_path = Path(bindings_file) if bindings_file else BINDINGS_FILE
        try:
            profiles = json.loads(profile_path.read_text(encoding="utf-8")).get("profiles", {})
        except (OSError, json.JSONDecodeError) as exc:
            raise BindingError(f"cannot load binding profiles {profile_path}: {exc}") from exc
        if profile not in profiles:
            raise BindingError(f"unknown workflow profile: {profile}")
        workflow_name = profiles[profile].get("workflow")
        if not workflow_name:
            raise BindingError(f"profile {profile!r} does not declare a workflow")
        root = Path(workflows_dir) if workflows_dir else WORKFLOWS_DIR
        windows, posix = PureWindowsPath(workflow_name), PurePosixPath(workflow_name)
        if (
            windows.drive
            or windows.is_absolute()
            or posix.is_absolute()
            or ".." in windows.parts
            or ".." in posix.parts
        ):
            raise BindingError("Profile workflow must be a relative path within workflows_dir")
        target = (root / workflow_name).resolve()
        if not target.is_relative_to(root.resolve()):
            raise BindingError("Profile workflow escapes workflows_dir")
        return cls.from_file(target, profile, bindings_file=profile_path)

    @property
    def reference_capacity(self) -> int:
        slots = self._profile.get("reference_slots", [])
        if not isinstance(slots, list):
            raise BindingError("reference_slots must be a list")
        return len(slots)

    @property
    def has_first_frame(self) -> bool:
        return bool(self._profile.get("first_frame"))

    @property
    def graph(self) -> dict[str, Any]:
        """Return an unmodified copy of the checked-in API graph."""
        return copy.deepcopy(self._graph)

    @property
    def profile(self) -> dict[str, Any]:
        """Return an unmodified copy of the binding profile."""
        return copy.deepcopy(self._profile)

    @property
    def audio_reference_capacity(self) -> int:
        slots = self._profile.get("audio_slots", [])
        if not isinstance(slots, list):
            raise BindingError("audio_slots must be a list")
        return len(slots)

    def prepare(
        self,
        prompt: str,
        *,
        seed: int | None = None,
        width: int | None = None,
        height: int | None = None,
        length: int | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | tuple[str, ...] | None = None,
        reference_audios: list[str] | tuple[str, ...] | None = None,
        negative_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Bind supported values to existing node inputs and return a fresh graph."""
        if not isinstance(prompt, str):
            raise BindingError("prompt must be a string")
        values: dict[str, Any] = {"prompt": prompt}
        optional = {
            "seed": seed,
            "width": width,
            "height": height,
            "length": length,
            "first_frame": first_frame,
            "negative_prompt": negative_prompt,
        }
        for field, value in optional.items():
            if value is not None:
                if field in {"width", "height", "length"} and value <= 0:
                    raise BindingError(f"{field} must be positive")
                if field in {"first_frame"}:
                    _validate_remote_image(value, field)
                values[field] = value
        if reference_images is not None:
            if isinstance(reference_images, (str, bytes)):
                raise BindingError("reference_images must be a list of remote image names")
            values["reference_images"] = list(reference_images)
        if reference_audios is not None:
            if isinstance(reference_audios, (str, bytes)):
                raise BindingError("reference_audios must be a list of remote audio names")
            values["reference_audios"] = list(reference_audios)
        reference_audios = values.pop("reference_audios", None)

        graph = copy.deepcopy(self._graph)
        if self._profile.get("requires_first_frame") and not values.get("first_frame"):
            raise BindingError(f"profile {self.name!r} requires first_frame")
        for field, value in values.items():
            if field in {"reference_images", "reference_audios"}:
                continue
            if field != "first_frame" or self._profile.get("first_frame") is not None:
                self._bind_scalar(graph, field, value)
        self._bind_references(graph, values.get("reference_images", []))
        self._bind_audios(graph, reference_audios or [])
        return graph

    def _bind_scalar(self, graph: dict[str, Any], field: str, value: Any) -> None:
        mappings = (
            [self._profile.get("first_frame")]
            if field == "first_frame" and self._profile.get("first_frame") is not None
            else self._profile.get("bindings", {}).get(field)
        )
        if mappings is None:
            raise BindingError(f"profile {self.name!r} does not support {field!r}")
        if not isinstance(mappings, list) or not mappings:
            raise BindingError(f"profile binding {field!r} must be a non-empty list")
        for mapping in mappings:
            expected_type = mapping.get("type", "any")
            if not _matches_type(value, expected_type):
                raise BindingError(
                    f"{field} must have type {expected_type}, got {type(value).__name__}"
                )
            _set_input(graph, mapping, value, field=field)

    def _bind_references(self, graph: dict[str, Any], images: list[str]) -> None:
        slots = self._profile.get("reference_slots", [])
        if len(images) > len(slots):
            raise BindingError(
                f"profile {self.name!r} supports {len(slots)} additional reference image(s); "
                f"received {len(images)}. Extend the profile and graph with real image nodes and inputs."
            )
        for index, image in enumerate(images):
            _validate_remote_image(image, f"reference_images[{index}]")
            slot = slots[index]
            if not isinstance(slot, dict) or not isinstance(slot.get("load"), dict):
                raise BindingError(f"reference slot {index} needs a load mapping")
            _set_input(graph, slot["load"], image, field=f"reference_images[{index}]")
            targets = slot.get("conditioning", [])
            if not isinstance(targets, list):
                raise BindingError(f"reference slot {index} conditioning must be a list")
            for target in targets:
                # Confirm each denoising pass already contains its corresponding
                # socket. Profiles do not invent ComfyUI inputs at runtime.
                _check_input(graph, target, field=f"reference slot {index}")
        _remove_unused_slots(graph, slots, len(images), field="conditioning")

    def _bind_audios(self, graph: dict[str, Any], audios: list[str]) -> None:
        slots = self._profile.get("audio_slots", [])
        if len(audios) > len(slots):
            raise BindingError(
                f"profile {self.name!r} supports {len(slots)} reference audio(s); received {len(audios)}"
            )
        for index, audio in enumerate(audios):
            _validate_remote_image(audio, f"reference_audios[{index}]")
            slot = slots[index]
            if not isinstance(slot, dict) or not isinstance(slot.get("load"), dict):
                raise BindingError(f"audio slot {index} needs a load mapping")
            _set_input(graph, slot["load"], audio, field=f"reference_audios[{index}]")
            targets = slot.get("conditioning", [])
            if not isinstance(targets, list):
                raise BindingError(f"audio slot {index} conditioning must be a list")
            for target in targets:
                _check_input(graph, target, field=f"audio slot {index}")
        _remove_unused_slots(graph, slots, len(audios), field="conditioning")


def _remove_unused_slots(
    graph: dict[str, Any], slots: list[dict[str, Any]], used: int, *, field: str
) -> None:
    for slot in slots[used:]:
        for target in slot.get(field, []):
            node_id = str(target.get("node_id", ""))
            input_name = target.get("input")
            node = graph.get(node_id)
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"].pop(input_name, None)
        load = slot.get("load", {})
        node_id = str(load.get("node_id", ""))
        if node_id:
            graph.pop(node_id, None)


def get_default_template(
    profile: str,
    *,
    bindings_file: str | Path | None = None,
    workflows_dir: str | Path | None = None,
) -> WorkflowTemplate:
    """Load one of the checked-in ``qwen``, ``ref2va`` or ``i2va`` profiles."""
    return WorkflowTemplate.from_profile(
        profile, bindings_file=bindings_file, workflows_dir=workflows_dir
    )


def _set_input(graph: dict[str, Any], mapping: dict[str, Any], value: Any, *, field: str) -> None:
    if not isinstance(mapping, dict):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    if not node_id or not isinstance(input_name, str) or not input_name:
        raise BindingError(f"mapping for {field!r} requires node_id and input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    if input_name not in node["inputs"]:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")
    expected_type = mapping.get("type", "any")
    if not _matches_type(value, expected_type):
        raise BindingError(
            f"mapped value for {field!r} must have type {expected_type}, got {type(value).__name__}"
        )
    node["inputs"][input_name] = value


def _check_input(graph: dict[str, Any], mapping: dict[str, Any], *, field: str) -> None:
    if not isinstance(mapping, dict):
        raise BindingError(f"invalid mapping for {field!r}")
    node_id = str(mapping.get("node_id", ""))
    input_name = mapping.get("input")
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise BindingError(f"mapping for {field!r} targets missing node {node_id!r}")
    if not isinstance(input_name, str) or input_name not in node["inputs"]:
        raise BindingError(f"mapping for {field!r} targets missing input {node_id}.{input_name}")


def _matches_type(value: Any, type_name: str) -> bool:
    checks = {
        "any": lambda v: True,
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
    }
    if type_name not in checks:
        raise BindingError(f"unsupported binding type: {type_name!r}")
    return checks[type_name](value)


def _validate_remote_image(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BindingError(f"{field} must be a non-empty remote image name")
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise BindingError(f"{field} must be a safe ComfyUI-relative image name")
