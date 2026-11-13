from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from copy import deepcopy
from itertools import groupby
from pathlib import Path

from comfyui_client import get_default_template
from llm import OpenAILLM
from prompt_project import PromptRelease

from . import protocols
from .assets import KINDS, bindings, parse_assets
from .comfy_servers import ComfyRouter
from .config import Config
from .storage import RunLock, fingerprint, json_text, write_json


class Pipeline:
    def __init__(self, config: Config, *, llm=None, comfy=None):
        self.config = config
        self.llm = llm
        self.comfy = comfy
        self._owns_llm = llm is None
        self._owns_comfy = comfy is None
        self._router = ComfyRouter(config)
        self._clients = {}
        self._active_server = None
        self.state = {}
        self.root = None
        self.release = None
        self.library = None
        self._legacy_run = False
        self.image_template = self._template(config.media.image_profile)
        if config.media.video_profile == "i2va":
            raise ValueError(
                "Pipeline storyboard nodes use Ref2VA. Use ref2va or a Ref2VA-compatible custom profile; I2VA is available via the standalone client."
            )
        self.video_template = self._template(config.media.video_profile)
        self.i2va_template = self._template(config.media.i2va_profile)
        if not self.video_template.has_first_frame:
            raise ValueError("Video workflow must expose a first_frame binding")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """Release resources created by this pipeline; injected clients are caller-owned."""
        if self.library is not None:
            close = getattr(self.library, "close", None)
            if close:
                close()
            self.library = None
        if self._owns_llm and self.llm is not None:
            close = getattr(self.llm, "close", None)
            if close:
                close()
            self.llm = None
        if self._owns_comfy and self.comfy is not None:
            close = getattr(self.comfy, "close", None)
            if close:
                close()
            self.comfy = None
        if self._owns_comfy:
            for client in self._clients.values():
                close = getattr(client, "close", None)
                if close:
                    close()
            self._clients.clear()

    def _template(self, profile):
        if (
            profile in {"qwen", "qwen-unlock", "ref2va", "i2va"}
            and not self.config.media.bindings_file
            and not self.config.media.workflows_dir
        ):
            return get_default_template(profile)
        from comfyui_client import WorkflowTemplate

        return WorkflowTemplate.from_profile(
            profile,
            bindings_file=self.config.media.bindings_file,
            workflows_dir=self.config.media.workflows_dir,
        )

    @property
    def capabilities(self):
        return {
            "image": self.image_template.capabilities,
            "video": self.video_template.capabilities,
            "video_modes": {
                "ref2va": self.video_template.capabilities,
                "i2va": self.i2va_template.capabilities,
            },
            "min_seconds": self.config.min_seconds,
            "max_seconds": self.config.max_seconds,
        }

    def _workflow_identity(self, template):
        if hasattr(template, "graph") and hasattr(template, "profile"):
            profile = template.profile
            # Configurations without image negatives retain the identity of
            # equivalent workflows created before negative-prompt support.
            default_profile = getattr(template, "name", None) in {"qwen", "qwen-unlock"}
            if (
                default_profile
                and not self.config.style.resolved()["image_negative"]
                and self.config.style.image_negative is None
            ):
                profile.get("bindings", {}).pop("negative_prompt", None)
            return {"graph": template.graph, "profile": profile}
        # Test adapters may only expose prepare; real templates fingerprint
        # their source graph without fulfilling runtime input requirements.
        options = {"first_frame": "__fingerprint__.png"} if template.has_first_frame else {}
        return template.prepare("__fingerprint__", **options)

    def dry_run(self, source: str) -> dict:
        self._legacy_run = False
        self._check_source(source)
        release = PromptRelease.load(self.config.prompt_project, self.config.prompt_version)
        self._validate_release(release)
        return dict(
            prompt_version=release.version,
            source_characters=len(source),
            stages=[
                "context_summary",
                "asset.character",
                "asset.scene",
                "asset.prop",
                "novel_to_script",
                "rewrite_batch",
                "asset_binding",
                "shot_plan",
                "storyboard_batch",
                "storyboard_i2va_batch",
                "continuity",
            ],
            capabilities=self.capabilities,
            generation_mode=self.config.generation_mode,
            llm_model_configured=bool(self.config.llm.model),
            llm_key_configured=bool(os.getenv(self.config.llm.api_key_env)),
            comfyui_url_configured=(
                any(self._router.url(name, required=False) for name in self._router.names)
                if self._router.named
                else bool(os.getenv("COMFYUI_URL") or os.getenv("COMFYUI_BASE_URL"))
            ),
        )

    @staticmethod
    def _check_source(source):
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Input novel is empty")
        if any(
            token in source
            for token in (protocols.START, protocols.END, protocols.FIELD, protocols.RECORD)
        ):
            raise ValueError(
                "Input contains output protocol markers; escape or preprocess the source first"
            )

    def _save(self):
        write_json(self.root / "state.json", self.state)

    def _node(self, key: str, node: str, variables: dict, parser):
        cached = self.state["nodes"].get(key)
        if cached is not None:
            if cached.get("node_id") != node:
                raise ValueError(f"Cached node {key} has a different prompt contract")
            return cached["value"]
        # Nodes intentionally have different input contracts; never leak unrelated
        # stage fields into the strict renderer.
        template = self.release.text(node)
        required = set(re.findall(r"\{\{\s*([^{}]+?)\s*\}\}", template))
        prompt = self.release.render(node, {k: variables[k] for k in required if k in variables})
        directory = self.root / "nodes" / key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.txt").write_text(prompt, encoding="utf-8")
        if self.llm is None:
            self.llm = OpenAILLM(self.config.llm)
        correction = ""
        for attempt in range(self.config.validation_retries + 1):
            # Do not overwrite prior failed run evidence on resume.
            number = 1 + len(list(directory.glob("attempt-*.txt")))
            raw = self.llm.complete(prompt, correction=correction)
            (directory / f"attempt-{number:03d}.txt").write_text(raw, encoding="utf-8")
            try:
                value = parser(raw)
            except (ValueError, KeyError, TypeError) as exc:
                correction = str(exc)[:4000]
                write_json(
                    directory / f"attempt-{number:03d}.error.json", {"validation_error": correction}
                )
                if attempt == self.config.validation_retries:
                    raise ValueError(f"{key}: output validation failed: {correction}") from exc
            else:
                self.state["nodes"][key] = {"node_id": node, "value": value}
                self._save()
                return value

    def run(
        self, source: str, output: str | Path, *, resume: bool = False, render: bool = False
    ) -> dict:
        self._legacy_run = False
        current_router = ComfyRouter(self.config)
        if (
            self._owns_comfy
            and self.comfy is not None
            and (current_router.named or current_router.legacy_url != self._router.legacy_url)
        ):
            close = getattr(self.comfy, "close", None)
            if close:
                close()
            self.comfy = None
        if self._router.named and current_router.identity() != self._router.identity():
            for client in self._clients.values():
                close = getattr(client, "close", None)
                if close:
                    close()
            self._clients.clear()
        self._router = current_router
        self._active_server = None
        self._check_source(source)
        self.root = Path(output).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with RunLock(self.root):
            state_path = self.root / "state.json"
            identity = dict(
                source_sha256=fingerprint(source),
                config=self.config.public_dict(),
                comfyui_url=("" if self._router.named else self._router.legacy_url),
                workflows=fingerprint(
                    {
                        "image": self._workflow_identity(self.image_template),
                        "video": self._workflow_identity(self.video_template),
                        "i2va": self._workflow_identity(self.i2va_template),
                        "capabilities": self.capabilities,
                    }
                ),
            )
            if self._router.named:
                identity["comfyui_routing"] = self._router.identity()
            if "style" in identity["config"]:
                # Preserve actual resolved content, including built-ins, so a
                # preset implementation change cannot alter an existing run.
                identity["style"] = self.config.style.resolved()
            if state_path.exists():
                if not resume:
                    raise ValueError(
                        "Run already exists. Use --resume or choose another --output directory"
                    )
                self.state = json.loads(state_path.read_text(encoding="utf-8"))
                if self.state["identity"] != identity:
                    if not self._matches_legacy_identity(self.state["identity"], identity):
                        raise ValueError(
                            "Source/config/workflow changed; create a new run instead of resuming"
                        )
                    self._legacy_run = True
                self.release = PromptRelease.from_directory(self.root / "prompts")
                if self.release.version != self.state["prompt_version"]:
                    raise ValueError("Pinned prompt snapshot does not match this run")
            else:
                if resume:
                    raise ValueError("Cannot resume: state.json does not exist")
                current = PromptRelease.load(self.config.prompt_project, self.config.prompt_version)
                self._validate_release(current)
                if (self.root / "prompts").exists():
                    raise ValueError("Orphan prompt snapshot exists; use a new output directory")
                shutil.copytree(current.root, self.root / "prompts")
                self.release = PromptRelease.from_directory(self.root / "prompts")
                (self.root / "source.txt").write_text(source, encoding="utf-8")
                self.state = dict(
                    schema_version=1,
                    identity=identity,
                    prompt_version=current.version,
                    nodes={},
                    media={},
                    status="running",
                )
                self._save()
            try:
                self._infer(source)
                if render:
                    self._render()
                self.state["status"] = "rendered" if render else "inferred"
                self.state.pop("last_error", None)
                self._save()
            except Exception as exc:
                self.state["status"] = "failed"
                # Keep provider credentials out of persisted error text.
                self.state["last_error"] = {"type": type(exc).__name__}
                self._save()
                raise
            return {
                "output": str(self.root),
                "prompt_version": self.release.version,
                "panels": len(self.state["panels"]),
                "assets": len(self.state["assets"]),
                "status": self.state["status"],
            }

    def _validate_release(self, release):
        """Fail before model calls if the published release lacks required nodes."""
        required = [
            "asset.character",
            "asset.scene",
            "asset.prop",
            "novel_to_script",
            "asset_binding",
            "continuity",
        ]
        if self.config.summarize:
            required.append("context_summary")
        if self.config.rewrite:
            required.append("rewrite_batch")
        mode = "ref2va" if self._legacy_run else self.config.generation_mode
        if mode == "auto":
            required.extend(["shot_plan", "storyboard_i2va_batch", "storyboard_batch"])
            self._mode_template("i2va")
            self._mode_template("ref2va")
        elif mode == "i2va":
            required.append("storyboard_i2va_batch")
            self._mode_template("i2va")
        else:
            required.append("storyboard_batch")
            self._mode_template("ref2va")
        for node in required:
            release.text(node)

    def _matches_legacy_identity(self, previous, current):
        """Permit pre-hybrid checkpoints to resume in their original Ref2VA mode."""
        if self._router.named:
            return False
        old_config = previous.get("config", {})
        if "generation_mode" in old_config or self.config.generation_mode == "i2va":
            return False
        config = deepcopy(current["config"])
        config.pop("generation_mode", None)
        config.get("media", {}).pop("i2va_profile", None)
        old_capabilities = {
            "image": self.image_template.capabilities,
            "video": self.video_template.capabilities,
            "min_seconds": self.config.min_seconds,
            "max_seconds": self.config.max_seconds,
        }
        expected = dict(
            current,
            config=config,
            workflows=fingerprint(
                {
                    "image": self._workflow_identity(self.image_template),
                    "video": self._workflow_identity(self.video_template),
                    "capabilities": old_capabilities,
                }
            ),
        )
        return previous == expected

    def _asset_parser(self, text, kind, source):
        result = parse_assets(text, kind, source)
        if self.config.strict_asset_validation:
            # Validators come from the installed package, never workspace code.
            from prompt_project.validation import asset_validation_report

            path = self.root / "nodes" / f"asset_{kind}" / "validation-input.json"
            path.write_text(text, encoding="utf-8")
            errors, warnings = asset_validation_report(text, kind, source)
            report = [
                *(f"ERROR: {item}" for item in errors),
                *(f"WARNING: {item}" for item in warnings),
            ]
            (path.parent / "validation.txt").write_text("\n".join(report), encoding="utf-8")
            if errors:
                raise ValueError("Asset validator failed: " + "\n".join(errors)[-4000:])
        return result

    def _infer(self, source):
        context = (
            self._node(
                "context",
                "context_summary",
                {"小说原文": source, "当前章节原文": source},
                protocols.summary,
            )
            if self.config.summarize
            else {"story": "", "chapter": ""}
        )
        assets = []
        for kind in KINDS:
            assets += self._node(
                f"asset_{kind}",
                f"asset.{kind}",
                {"小说原文": source, "推文文案": "无", "故事情节": context["story"]},
                lambda text, kind=kind: self._asset_parser(text, kind, source),
            )
        from .library import AssetLibrary

        if self.library is not None:
            close = getattr(self.library, "close", None)
            if close:
                close()
        self.library = AssetLibrary(self.config.asset_library, self.config.project_id)
        if "assets" not in self.state:
            assets = self.library.register(
                assets,
                source_sha256=self.state["identity"]["source_sha256"],
                prompt_version=self.release.version,
                run_id=str(self.root),
                reuse_existing=self.config.reuse_existing_assets,
            )
            # A later chapter may only name an established asset without
            # repeating the two visual facts required by extraction.
            candidates = {a["asset_id"]: a for a in assets}
            for asset in self.library.list():
                names = [asset["name"]] + [a.strip() for a in asset["aliases"].split(",")]
                if any(name and name in source for name in names):
                    candidates.setdefault(asset["asset_id"], asset)
            assets = sorted(candidates.values(), key=lambda a: KINDS.index(a["kind"]))
            self.state["assets"] = assets
            self._save()
        else:
            # A run keeps its chosen asset version even if the shared library changes.
            assets = self.state["assets"]
        write_json(self.root / "assets.json", assets)
        panels = self._node(
            "script",
            "novel_to_script",
            {"输入文案": source},
            lambda text: protocols.script(text, self.config.first_panel_index),
        )
        write_json(self.root / "script.json", panels)
        all_boards = []
        previous_state = "无"
        rewritten = []
        for offset in range(0, len(panels), self.config.batch_size):
            batch_index = offset // self.config.batch_size
            batch = panels[offset : offset + self.config.batch_size]
            info = {"故事情节": context["story"], "章节情节": context["chapter"], "推文文案": "无"}
            for kind, label in zip(KINDS, ("角色信息", "场景信息", "物品信息")):
                info[label] = json_text([a for a in assets if a["kind"] == kind])
            if self.config.rewrite:
                batch = self._node(
                    f"rewrite_{batch_index:04d}",
                    "rewrite_batch",
                    dict(info, 章节文案=json_text({"results": batch})),
                    lambda text, batch=batch: protocols.rewrites(text, batch),
                )
            rewritten.extend(batch)
            selected = {}
            for panel in batch:
                index = panel["panel_index"]
                selected[index] = self._node(
                    f"binding_{index}",
                    "asset_binding",
                    {"当前分镜文案": panel["text"], "候选资产": json_text(assets)},
                    lambda text: bindings(text, assets),
                )
            choices = self._shot_choices(batch_index, batch, selected)
            groups = [
                list(group)
                for _, group in groupby(
                    batch, key=lambda item: choices[item["panel_index"]]["generation_mode"]
                )
            ]
            boards = []
            for group_index, group in enumerate(groups):
                mode = choices[group[0]["panel_index"]]["generation_mode"]
                template = self._mode_template(mode)
                group_selected = {
                    item["panel_index"]: selected[item["panel_index"]] for item in group
                }
                group_info = dict(info)
                union_ids = {a["asset_id"] for records in group_selected.values() for a in records}
                for kind, label in zip(KINDS, ("角色信息", "场景信息", "物品信息")):
                    group_info[label] = json_text(
                        [a for a in assets if a["kind"] == kind and a["asset_id"] in union_ids]
                    )
                group_info["运行约束"] = json_text(self._runtime(group_selected, mode))
                group_info["章节文案"] = json_text({"results": group})
                group_info["前面分镜:2"] = json_text(
                    {"previous_panels": (all_boards + boards)[-2:], "end_state": previous_state}
                )
                suffix = f"_{group_index:03d}" if len(groups) > 1 else ""
                node_key = f"storyboard_{batch_index:04d}{suffix}"
                node_id = "storyboard_i2va_batch" if mode == "i2va" else "storyboard_batch"
                generated = self._node(
                    node_key,
                    node_id,
                    group_info,
                    lambda text, group=group, group_selected=group_selected, mode=mode, template=template: (
                        protocols.storyboards(
                            text,
                            group,
                            min_seconds=self.config.min_seconds,
                            max_seconds=self.config.max_seconds,
                            image_capacity=self.image_template.reference_capacity,
                            video_capacity=1
                            + (template.reference_capacity if mode == "ref2va" else 0),
                            audio_capacity=len(self.config.media.reference_audios)
                            if mode == "ref2va"
                            else 0,
                            selected=group_selected,
                            mode=mode,
                        )
                    ),
                )
                for item in generated:
                    decision = choices[item["panel_index"]]
                    boards.append(
                        dict(
                            item,
                            generation_mode=mode,
                            mode_reason=decision["reason"],
                            mode_source=decision["source"],
                            status="planned",
                        )
                    )
            all_boards.extend(boards)
            for board in boards:
                for asset in selected[board["panel_index"]]:
                    self.library.record_usage(
                        asset["asset_id"],
                        str(self.root),
                        board["panel_index"],
                        asset_version=asset.get("asset_version"),
                    )
            end_state = self._node(
                f"continuity_{batch_index:04d}",
                "continuity",
                {"前批末镜状态": previous_state, "本批分镜结果": json_text(boards)},
                protocols.continuity,
            )
            previous_state = end_state["summary"]
        self.state["panels"] = all_boards
        self.state["end_state"] = previous_state
        write_json(self.root / "rewritten.json", rewritten)
        write_json(self.root / "panels.json", all_boards)
        write_json(
            self.root / "shot_modes.json",
            [
                {
                    key: shot[key]
                    for key in ("panel_index", "generation_mode", "mode_reason", "mode_source")
                }
                for shot in all_boards
            ],
        )
        write_json(self.root / "continuity.json", {"summary": previous_state})
        self._save()

    def _mode_template(self, mode):
        if mode == "ref2va":
            audio_slots = getattr(self.video_template, "audio_reference_capacity", 0)
            if len(self.config.media.reference_audios) > audio_slots:
                raise ValueError("Configured audio references exceed the Ref2VA workflow slots")
            return self.video_template
        if mode == "i2va":
            if not self.i2va_template.has_first_frame:
                raise ValueError("I2VA workflow must expose a true first_frame input")
            if self.i2va_template.reference_capacity:
                raise ValueError("I2VA profile cannot advertise unsupported asset reference inputs")
            return self.i2va_template
        raise ValueError(f"Unknown video mode: {mode}")

    def _shot_choices(self, batch_index, batch, selected):
        configured = "ref2va" if self._legacy_run else self.config.generation_mode
        if configured == "auto":
            planning_runtime = self._runtime(selected, "ref2va")
            planning_runtime["generation_mode"] = "auto"
            planning_runtime["rules"] = (
                "Choose a type independently for each shot. video_modes lists the actual "
                "workflows. I2VA has a native first_frame input but no extra image or audio "
                "references. Ref2VA has the listed reference slots; its first image is "
                "a reference, not a guaranteed native first frame. panel_assets shows "
                "the asset mappings available if Ref2VA is selected."
            )
            plan = self._node(
                f"shot_plan_{batch_index:04d}",
                "shot_plan",
                {
                    "章节文案": json_text({"results": batch}),
                    "运行约束": json_text(planning_runtime),
                },
                lambda text: protocols.shot_plan(text, batch),
            )
            decisions = {item["panel_index"]: dict(item, source="llm") for item in plan}
        else:
            decisions = {
                item["panel_index"]: {
                    "panel_index": item["panel_index"],
                    "generation_mode": configured,
                    "reason": f"Configured {configured} mode",
                    "source": "config",
                }
                for item in batch
            }
        for item in batch:
            index = item["panel_index"]
            override = self.state.get("manual_modes", {}).get(str(index))
            if override:
                if self._legacy_run and override == "i2va":
                    raise ValueError("A pre-hybrid run cannot switch to I2VA; start a new run")
                decisions[index] = {
                    "panel_index": index,
                    "generation_mode": override,
                    "reason": f"Manually selected {override} mode",
                    "source": "manual",
                }
        return decisions

    def _runtime(self, selected, mode="ref2va"):
        template = self._mode_template(mode)
        per_panel = {}
        for index, group in selected.items():
            per_panel[index] = {
                "assets": [{k: a[k] for k in ("asset_id", "kind", "name")} for a in group],
                "first_frame_asset_ids": [
                    a["asset_id"] for a in group if a.get("first_frame", True)
                ],
                "image_references": [
                    {"image": i + 1, "name": a["name"]}
                    for i, a in enumerate(
                        [a for a in group if a.get("first_frame", True)][
                            : self.image_template.reference_capacity
                        ]
                    )
                ],
                "video_references": [
                    {"picture": i + 2, "name": a["name"]}
                    for i, a in enumerate(group[: template.reference_capacity])
                ]
                if mode == "ref2va"
                else [],
            }
        runtime = dict(
            self.capabilities,
            panel_assets=per_panel,
            generation_mode=mode,
            audio_references=[
                dict(audio=i + 1, name=Path(a["path"]).name, usage=a["usage"])
                for i, a in enumerate(self.config.media.reference_audios)
            ]
            if mode == "ref2va"
            else [],
        )
        runtime["video"] = template.capabilities
        runtime["rules"] = (
            (
                "Use only the provided mappings. Other assets are text-only. I2VA binds the "
                "generated first frame to a dedicated input. Only Picture 1 is available. "
                "No video asset images or audio references are available."
                if mode == "i2va"
                else "Use only the provided mappings. Other assets are text-only. Picture 1 "
                "is a reference to the generated image, not a guaranteed hard first frame."
            )
            + " Keep exact asset names. results contains panel_index and text. Duration is in seconds."
        )
        return runtime

    def _activate_server(self, stage, key, *, preferred=None):
        """Pin the server before uploads; retries reuse the persisted assignment."""
        if not self._router.named:
            if preferred is not None:
                raise ValueError("A remote audio server requires named ComfyUI servers")
            self._active_server = None
            return None
        pool = self._router.pool(stage)
        if preferred is not None and preferred not in pool:
            raise ValueError(f"ComfyUI server {preferred} is not in the {stage} pool")
        assignments = self.state.setdefault("server_assignments", {})
        record = assignments.get(key)
        media = self.state.get("media", {})
        keys = [key]
        if key.startswith("scene_"):
            asset_key = hashlib.sha256(key[len("scene_") :].encode("utf-8")).hexdigest()[:24]
            keys.extend(name for name in media if name.startswith(f"scene_{asset_key}_"))
        existing_jobs = [media[name] for name in keys if name in media]
        if record and record.get("stage") != stage and not existing_jobs and key.endswith("_video"):
            # Mode may be changed before a video job is submitted. Prior
            # uploads are already scoped by server and can safely remain.
            assignments.pop(key)
            record = None
        if record is None and existing_jobs:
            raise ValueError("A saved media task has no pinned ComfyUI server assignment")
        if record is None:
            server = self._router.choose(stage, server=preferred)
            record = {
                "stage": stage,
                "server": server,
                "url_sha256": self._router.endpoint_hash(server),
            }
            assignments[key] = record
            self._save()
        else:
            server = record.get("server")
            if (
                record.get("stage") != stage
                or server not in pool
                or (preferred is not None and server != preferred)
                or record.get("url_sha256") != self._router.endpoint_hash(server)
            ):
                raise ValueError("ComfyUI server assignment changed; create a new run")
            if any(
                job.get("server") != server or job.get("url_sha256") != record["url_sha256"]
                for job in existing_jobs
            ):
                raise ValueError("Saved media task disagrees with its ComfyUI server assignment")
        self._active_server = server
        return server

    def _client(self):
        if self._router.named:
            if self._active_server is None:
                raise RuntimeError("A ComfyUI server must be selected before accessing media")
            if self.comfy is not None:
                if isinstance(self.comfy, Mapping):
                    if self._active_server not in self.comfy:
                        raise ValueError("No injected client for the selected ComfyUI server")
                    return self.comfy[self._active_server]
                if len(self._router.names) > 1:
                    raise ValueError("Multiple servers require a mapping of injected clients")
                return self.comfy
            if self._active_server not in self._clients:
                self._clients[self._active_server] = self._router.client(self._active_server)
            return self._clients[self._active_server]
        if self.comfy is None:
            self.comfy = self._router.client(None)
        return self.comfy

    def _job(self, key, graph):
        if self._router.named and self._active_server is None:
            self._activate_server("image", key)
        client = self._client()
        entry = self.state["media"].get(key)
        digest = fingerprint(graph)
        # Imported asset IDs are opaque identifiers, never filesystem paths.
        safe_key = (
            key
            if re.fullmatch(r"[A-Za-z0-9_-]+", key)
            else hashlib.sha256(key.encode("utf-8")).hexdigest()
        )
        directory = self.root / "media" / safe_key
        if entry:
            if self._router.named and (
                entry.get("server") != self._active_server
                or entry.get("url_sha256") != self._router.endpoint_hash(self._active_server)
            ):
                raise ValueError(f"ComfyUI server changed for existing media task {key}")
            if entry["graph_sha256"] != digest:
                raise ValueError(f"Workflow changed for existing media task {key}")
            if entry.get("status") == "submitting":
                raise RuntimeError(
                    f"Submission outcome unknown for {key}; check the remote queue before recording its prompt_id. No duplicate job was sent."
                )
            if entry.get("files") and all(
                Path(x).is_file()
                and not Path(x).is_symlink()
                and self._file_hash(x) == entry.get("file_hashes", {}).get(x)
                for x in entry["files"]
            ):
                return entry["files"]
        else:
            entry = {"graph_sha256": digest, "status": "submitting"}
            if self._router.named:
                entry.update(
                    server=self._active_server,
                    url_sha256=self._router.endpoint_hash(self._active_server),
                )
            self.state["media"][key] = entry
            write_json(directory / "workflow.json", graph)
            self._save()
            # Mark before POST. A lost response must not cause an automatic duplicate.
            prompt_id = client.submit(graph)
            entry.update(prompt_id=prompt_id, status="submitted")
            self._save()
        if self._router.named or self._router.legacy_url:
            self._router.record_job(self._active_server, entry["prompt_id"])
        history = client.wait(entry["prompt_id"], timeout=self.config.media.timeout)
        write_json(directory / "history.json", history)
        files = [
            str(Path(x).resolve()) for x in client.download_outputs(history, directory / "outputs")
        ]
        if not files:
            raise RuntimeError(
                f"Remote task {entry['prompt_id']} completed without downloadable files"
            )
        entry.update(
            status="completed", files=files, file_hashes={x: self._file_hash(x) for x in files}
        )
        self._save()
        return files

    def _render(self):
        style = self.config.style.resolved()
        image_negative = self._style_negative_arguments(self.image_template, "image")
        if image_negative:
            self.image_template.prepare("preflight", **image_negative)
        # Validate each styled video before any media upload or submission.
        video_texts = {}
        audio_servers = {}
        if self._router.named:
            stages = {"image"} | {
                panel.get("generation_mode", "ref2va") for panel in self.state["panels"]
            }
            for stage in stages:
                for server in self._router.pool(stage):
                    self._router.url(server)
        for panel in self.state["panels"]:
            mode = panel.get("generation_mode", "ref2va")
            audio_servers[panel["panel_index"]] = self._remote_audio_server(mode)
            text = re.sub(r"\n时长：\d+秒\s*$", "", panel["video_prompt"])
            text = self.config.style.compose_video(text, mode)
            if style["prefix"] or style["suffix"]:
                template = self._mode_template(mode)
                picture_capacity = (
                    1 + min(len(panel["asset_ids"]), template.reference_capacity)
                    if mode == "ref2va"
                    else 1
                )
                audio_capacity = len(self.config.media.reference_audios) if mode == "ref2va" else 0
                protocols._video(
                    text + f"\n时长：{panel['duration_seconds']}秒",
                    self.config.min_seconds,
                    self.config.max_seconds,
                    picture_capacity,
                    audio_capacity,
                    mode,
                )
            video_texts[panel["panel_index"]] = text
        image_style = {
            "prefix": style["prefix"],
            "suffix": style["suffix"],
            "negative": style["image_negative"],
            "override": self.config.style.image_negative,
        }
        styled = bool(
            image_style["prefix"]
            or image_style["suffix"]
            or image_style["negative"]
            or image_style["override"] is not None
        )
        # Asset images are only needed if a selected workflow has real asset slots.
        capacity = max(
            self.image_template.reference_capacity, self.video_template.reference_capacity
        )
        asset_paths = {}
        used = set()
        selected = set()
        for panel in self.state["panels"]:
            selected.update(panel["first_frame_asset_ids"])
            selected.update(panel["asset_ids"])
            used.update(panel["first_frame_asset_ids"][: self.image_template.reference_capacity])
            if panel.get("generation_mode", "ref2va") == "ref2va":
                used.update(panel["asset_ids"][: self.video_template.reference_capacity])
        scene_enabled = False
        if any(a.get("kind") == "scene" and a["asset_id"] in used for a in self.state["assets"]):
            from .scene_views import enabled

            scene_enabled = enabled(self)
        if any(self._square_prop(a) and a["asset_id"] in used for a in self.state["assets"]):
            self.image_template.prepare(
                "preflight",
                width=self.config.media.prop_size,
                height=self.config.media.prop_size,
            )
        # Resolve portraits and any second identity reference before any
        # media upload or submission, even when a scene precedes the person.
        portrait_inputs = {}
        for asset in self.state["assets"]:
            asset_id = asset["asset_id"]
            if (
                asset.get("kind") != "character"
                or asset_id not in selected
                or self.state.get("asset_media", {}).get(asset_id)
            ):
                continue
            face = self._pin_character_face(asset)
            if face is None:
                continue
            if not self.image_template.reference_capacity:
                raise ValueError("Character face generation requires 1 image reference slots")
            if asset_id not in used:
                raise ValueError("Selected character face exceeds available asset reference slots")
            reference = self._pin_asset_reference(asset)
            if reference and reference["sha256"] == face["sha256"]:
                reference = None
            count = 1 + int(reference is not None)
            if self.image_template.reference_capacity < count:
                raise ValueError(
                    f"Character face generation requires {count} image reference slots"
                )
            # Check that the advertised bindings are usable before any POST.
            self.image_template.prepare(
                "preflight", reference_images=[f"face-preflight-{i}.png" for i in range(count)]
            )
            portrait_inputs[asset_id] = (face, reference)
        if capacity:
            for asset in self.state["assets"]:
                if asset["asset_id"] not in used:
                    continue
                pinned = self.state.setdefault("asset_media", {}).get(asset["asset_id"])
                if pinned:
                    self._verify_media(pinned)
                    asset_paths[asset["asset_id"]] = pinned["local_path"]
                    continue
                if scene_enabled and asset.get("kind") == "scene":
                    self._activate_server("image", "scene_" + asset["asset_id"])
                    record = self.generate_scene_asset(asset)
                    asset_paths[asset["asset_id"]] = record["local_path"]
                    continue
                face, reference = portrait_inputs.get(asset["asset_id"], (None, None))
                square = self._square_prop(asset)
                if not face and (styled or square) and self.image_template.reference_capacity:
                    reference = self._pin_asset_reference(asset)
                if face or square:
                    image_role = "reference:render:" + fingerprint(
                        {
                            "style": image_style,
                            "face_sha256": face["sha256"] if face else None,
                            "source_sha256": reference["sha256"] if reference else None,
                            "size": self._asset_size(asset),
                            "seed": self.config.media.seed,
                            "workflow": self._workflow_identity(self.image_template),
                        }
                    )
                elif styled:
                    image_role = "reference:style:" + fingerprint(
                        dict(image_style, source_sha256=reference["sha256"] if reference else None)
                    )
                else:
                    image_role = "reference"
                stored = self.library.files(
                    asset["asset_id"], asset_version=asset.get("asset_version"), role=image_role
                )
                available = [
                    x
                    for x in reversed(stored)
                    if Path(x["local_path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                ]
                if available:
                    record = available[0]
                    self._verify_media(record)
                    self.state["asset_media"][asset["asset_id"]] = record
                    asset_paths[asset["asset_id"]] = record["local_path"]
                    self._save()
                    continue
                self._activate_server("image", "asset_" + asset["asset_id"])
                reference_images = [
                    self._upload(item["local_path"]) for item in (face, reference) if item
                ]
                prompt = asset["description"]
                if face:
                    prompt = (
                        "Use reference image 1 only as the person's facial identity reference. "
                        "Preserve facial identity across the generated views. Use the following "
                        "description for clothing, hair, pose, setting, and artistic treatment; "
                        "do not copy these from the reference portrait.\n\n" + prompt
                    )
                width, height = self._asset_size(asset)
                graph = self.image_template.prepare(
                    self.config.style.compose_image(prompt),
                    seed=self.config.media.seed,
                    width=width,
                    height=height,
                    reference_images=reference_images,
                    **image_negative,
                )
                outputs = self._job("asset_" + asset["asset_id"], graph)
                record = self.library.link_file(
                    asset["asset_id"],
                    self._first_image(outputs),
                    role=image_role,
                    asset_version=asset.get("asset_version"),
                )
                asset_paths[asset["asset_id"]] = record["local_path"]
                self.state["asset_media"][asset["asset_id"]] = record
                self._save()

        def upload(path):
            return self._upload(path)

        for panel in self.state["panels"]:
            index = panel["panel_index"]
            mode = panel.get("generation_mode", "ref2va")
            template = self._mode_template(mode)
            self._activate_server("image", f"panel_{index}_image")
            image_refs = [
                upload(asset_paths[a])
                for a in panel["first_frame_asset_ids"][: self.image_template.reference_capacity]
            ]
            graph = self.image_template.prepare(
                self.config.style.compose_image(panel["prompt"]),
                seed=self.config.media.seed + index,
                width=self.config.media.frame_width,
                height=self.config.media.frame_height,
                reference_images=image_refs,
                **image_negative,
            )
            first_frame = self._first_image(self._job(f"panel_{index}_image", graph))
            self._activate_server(mode, f"panel_{index}_video", preferred=audio_servers[index])
            video_refs = (
                [upload(asset_paths[a]) for a in panel["asset_ids"][: template.reference_capacity]]
                if mode == "ref2va"
                else []
            )
            # Duration is orchestration metadata, not part of either prompt protocol.
            video_text = video_texts[index]
            audio_refs = (
                [
                    a["path"] if a.get("remote") else self._upload(a["path"], audio=True)
                    for a in self.config.media.reference_audios
                ]
                if mode == "ref2va"
                else []
            )
            graph = template.prepare(
                video_text,
                length=panel["duration_seconds"],
                first_frame=upload(first_frame),
                reference_images=video_refs,
                reference_audios=audio_refs,
            )
            self._job(f"panel_{index}_video", graph)

    def _square_prop(self, asset):
        if asset.get("kind") != "prop" or self.release is None:
            return False
        entry = self.release.manifest.get("nodes", {}).get("asset.prop", {})
        return entry.get("output_contract", {}).get("image_aspect_ratio") == "1:1"

    def _asset_size(self, asset):
        if self._square_prop(asset):
            return self.config.media.prop_size, self.config.media.prop_size
        return self.config.media.asset_width, self.config.media.asset_height

    def _pin_asset_reference(self, asset):
        refs = self.state.setdefault("asset_generation_refs", {})
        asset_id = asset["asset_id"]
        if asset_id not in refs:
            references = self.library.files(
                asset_id, asset_version=asset.get("asset_version"), role="reference"
            )
            refs[asset_id] = next(
                (
                    item
                    for item in reversed(references)
                    if Path(item["local_path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                ),
                None,
            )
            self._save()
        reference = refs[asset_id]
        if reference:
            self._verify_media(reference)
        return reference

    def _pin_character_face(self, asset):
        from .config import verify_face_photo

        refs = self.state.setdefault("asset_generation_faces", {})
        asset_id = asset["asset_id"]
        if asset_id not in refs:
            names = {asset.get("name", "")}
            names.update(alias.strip() for alias in asset.get("aliases", "").split(","))
            matches = {
                str(Path(path).expanduser().resolve())
                for name, path in self.config.media.character_face_references.items()
                if name in names
            }
            if len(matches) > 1:
                raise ValueError("Character aliases select conflicting face reference photos")
            if matches:
                source = verify_face_photo(next(iter(matches)))
                reference = self.library.link_file(
                    asset_id, source, role="face", asset_version=asset.get("asset_version")
                )
            else:
                candidates = self.library.files(
                    asset_id, asset_version=asset.get("asset_version"), role="face"
                )
                reference = next(
                    (
                        item
                        for item in reversed(candidates)
                        if Path(item["local_path"]).suffix.lower()
                        in {".png", ".jpg", ".jpeg", ".webp"}
                    ),
                    None,
                )
            refs[asset_id] = reference
            self._save()
        face = refs[asset_id]
        if face:
            self._verify_media(face)
        return face

    def generate_scene_asset(self, asset):
        """Generate/reuse a scene's four-view composite in an initialized run."""
        from .scene_views import generate

        return generate(self, asset)

    def _style_negative_arguments(self, template, medium):
        value = self.config.style.resolved()[f"{medium}_negative"]
        if value:
            # Passing unsupported nonempty negatives makes preflight fail.
            return {"negative_prompt": value}
        override = getattr(self.config.style, f"{medium}_negative")
        profile = getattr(template, "profile", {})
        if override is not None and "negative_prompt" in profile.get("bindings", {}):
            # An explicit empty override clears a workflow's default negative.
            return {"negative_prompt": ""}
        return {}

    def _upload(self, path, *, audio=False):
        digest = self._file_hash(path)
        key = ("audio:" if audio else "image:") + digest
        if self._router.named:
            if self._active_server is None:
                raise RuntimeError("A ComfyUI server must be selected before uploading media")
            server_key = fingerprint(
                {
                    "name": self._active_server,
                    "url": self._router.endpoint_hash(self._active_server),
                }
            )
            key = "server:" + server_key + ":" + key
        uploads = self.state.setdefault("uploads", {})
        if key not in uploads:
            client = self._client()
            uploads[key] = client.upload_file(path) if audio else client.upload_image(path)
            self._save()
        return uploads[key]

    def _remote_audio_server(self, mode):
        if mode != "ref2va":
            return None
        references = [a for a in self.config.media.reference_audios if a.get("remote")]
        if not references:
            return None
        if not self._router.named:
            if any(a.get("server") for a in references):
                raise ValueError("A remote audio server requires named ComfyUI servers")
            return None
        pool = self._router.pool(mode)
        names = set()
        for audio in references:
            server = audio.get("server")
            if server is None:
                if len(pool) != 1:
                    raise ValueError(
                        "Remote audio must specify its server when the video pool has multiple servers"
                    )
                server = pool[0]
            if server not in pool:
                raise ValueError(f"Remote audio server {server} is not in the {mode} pool")
            names.add(server)
        if len(names) != 1:
            raise ValueError("Remote audio references for one video must be on the same server")
        return names.pop()

    @staticmethod
    def _file_hash(path):
        with Path(path).open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def _verify_media(self, record):
        path = Path(record["local_path"])
        if not path.is_file() or path.is_symlink() or self._file_hash(path) != record["sha256"]:
            raise ValueError(f"Managed asset reference failed integrity check: {path}")

    @staticmethod
    def _first_image(paths):
        for path in paths:
            if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                return path
        raise RuntimeError("Image workflow did not return a downloadable image")
