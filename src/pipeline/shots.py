"""Inspect and explicitly change the generation mode of planned shots."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from prompt_project import PromptRelease

from .storage import RunLock, write_json


class RunShots:
    """Manage shot metadata inside a single existing pipeline run.

    A mode change invalidates the containing batch and every following batch,
    because their prompt and continuity inputs may depend on the changed shot.
    Previously submitted media is never discarded or implicitly re-submitted.
    """

    def __init__(self, run_dir: str | Path):
        self.root = Path(run_dir).expanduser().resolve()

    def _read(self) -> dict:
        path = self.root / "state.json"
        if not path.is_file():
            raise FileNotFoundError(f"Run state does not exist: {path}")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("panels"), list):
            raise ValueError("The run has no completed storyboard plan")
        return state

    @staticmethod
    def _find(state: dict, panel_index: int) -> dict:
        if type(panel_index) is not int or panel_index < 0:
            raise ValueError("panel_index must be a nonnegative integer")
        for item in state["panels"]:
            if item.get("panel_index") == panel_index:
                return item
        raise KeyError(f"No shot with panel_index {panel_index}")

    def list(self) -> list[dict]:
        # Atomic state.json replacement gives readers a complete snapshot.
        return deepcopy(self._read()["panels"])

    def get(self, panel_index: int) -> dict:
        return deepcopy(self._find(self._read(), panel_index))

    def set_mode(self, panel_index: int, mode: str) -> dict:
        if mode not in {"i2va", "ref2va"}:
            raise ValueError("mode must be i2va or ref2va")
        if not self.root.is_dir():
            raise FileNotFoundError(f"Run directory does not exist: {self.root}")
        with RunLock(self.root):
            state = self._read()
            shot = self._find(state, panel_index)
            if shot.get("generation_mode", "ref2va") == mode:
                return deepcopy(shot)
            script = state.get("nodes", {}).get("script", {}).get("value")
            if not isinstance(script, list):
                raise ValueError("Run does not contain its original numbered script")
            indices = [item.get("panel_index") for item in script if isinstance(item, dict)]
            if len(indices) != len(script) or panel_index not in indices:
                raise ValueError("Run script and storyboard indices do not match")
            config = state.get("identity", {}).get("config", {})
            batch_size = config.get("batch_size")
            if type(batch_size) is not int or batch_size < 1:
                raise ValueError("Run has no valid batch_size")
            position = indices.index(panel_index)
            first_position = (position // batch_size) * batch_size
            first_batch = position // batch_size
            affected = set(indices[first_position:])
            for key in state.get("media", {}):
                match = re.fullmatch(r"panel_(\d+)_(?:image|video)", key)
                if match and int(match[1]) in affected:
                    raise RuntimeError(
                        "Cannot change mode after media was submitted for this or a subsequent "
                        "affected batch; create a new run"
                    )
            if mode == "i2va" and (
                "generation_mode" not in config or "i2va_profile" not in config.get("media", {})
            ):
                raise ValueError("Pre-hybrid runs cannot switch to I2VA; create a new run")
            release = PromptRelease.from_directory(self.root / "prompts")
            release.text("storyboard_i2va_batch" if mode == "i2va" else "storyboard_batch")

            nodes = state.get("nodes", {})
            removed = {}
            for key in list(nodes):
                match = re.fullmatch(r"(?:storyboard|continuity)_(\d+)(?:_\d+)?", key)
                if match and int(match[1]) >= first_batch:
                    removed[key] = nodes.pop(key)
            history = {
                "panel_index": panel_index,
                "previous_mode": shot.get("generation_mode", "ref2va"),
                "new_mode": mode,
                "previous_end_state": state.get("end_state"),
                "invalidated_nodes": removed,
                "invalidated_panels": [
                    deepcopy(item)
                    for item in state["panels"]
                    if item.get("panel_index") in affected
                ],
            }
            history_path = self.root / "history" / "mode-changes" / f"{uuid4().hex}.json"
            write_json(history_path, history)
            state.setdefault("manual_modes", {})[str(panel_index)] = mode
            for item in state["panels"]:
                if item.get("panel_index") not in affected:
                    continue
                item["status"] = "needs_recompute"
                item["prompt"] = None
                item["video_prompt"] = None
                item["duration_seconds"] = None
                if item["panel_index"] == panel_index:
                    item["generation_mode"] = mode
                    item["mode_reason"] = f"Manually selected {mode} mode"
                    item["mode_source"] = "manual"
            state["status"] = "stale"
            state["end_state"] = None
            state.pop("last_error", None)
            state.setdefault("mode_changes", []).append(str(history_path.relative_to(self.root)))
            # Commit the authoritative state first. Derived JSON exports can be
            # rebuilt by resume if the process is interrupted afterwards.
            write_json(self.root / "state.json", state)
            try:
                write_json(self.root / "panels.json", state["panels"])
                write_json(
                    self.root / "continuity.json", {"summary": None, "status": "needs_recompute"}
                )
                write_json(
                    self.root / "shot_modes.json",
                    [
                        {
                            key: item.get(key)
                            for key in (
                                "panel_index",
                                "generation_mode",
                                "mode_reason",
                                "mode_source",
                                "status",
                            )
                        }
                        for item in state["panels"]
                    ],
                )
            except OSError as exc:
                raise RuntimeError(
                    "Mode change was saved, but derived files could not be updated; "
                    "resume the run to regenerate them"
                ) from exc
            return deepcopy(self._find(state, panel_index))
