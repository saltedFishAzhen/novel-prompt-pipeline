"""Integration checks using the real OpenAI client against an in-process transport.

These tests never call an external model or ComfyUI server. Prompt releases are
created under a temporary directory; the checked-in release is not modified.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from pipeline import protocols
from pipeline.assets import bindings, parse_assets
from pipeline.config import Config, LLMConfig
from pipeline.engine import Pipeline
from pipeline.library import AssetLibrary
from pipeline.llm import LLMError, OpenAILLM
from pipeline.storage import RunLock
from prompt_project import PromptRelease

VARIABLES = {
    "context_summary": ["小说原文", "当前章节原文"],
    "asset.character": ["小说原文", "推文文案", "故事情节"],
    "asset.scene": ["小说原文", "推文文案", "故事情节"],
    "asset.prop": ["小说原文", "推文文案", "故事情节"],
    "novel_to_script": ["输入文案"],
    "rewrite_batch": [
        "故事情节",
        "章节情节",
        "推文文案",
        "角色信息",
        "场景信息",
        "物品信息",
        "章节文案",
    ],
    "asset_binding": ["当前分镜文案", "候选资产"],
    "storyboard_batch": [
        "故事情节",
        "章节情节",
        "推文文案",
        "角色信息",
        "场景信息",
        "物品信息",
        "章节文案",
        "前面分镜:2",
        "运行约束",
    ],
    "continuity": ["前批末镜状态", "本批分镜结果"],
}


def create_release(root: Path, label: str) -> str:
    """Create a minimal legitimate manifest for exercising real release loading."""
    nodes = {}
    payloads = {}
    for node, names in VARIABLES.items():
        template = f"NODE:{node}\nRELEASE:{label}\n" + "".join(
            f"VAR={name}\n{{{{{name}}}}}\n%%END%%\n" for name in names
        )
        data = template.encode("utf-8")
        rel = f"files/{node}.txt"
        payloads[rel] = data
        nodes[node] = {
            "file": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "variables": names,
            "output_contract": {"type": "test"},
        }
    canonical = json.dumps(
        {"assets": {}, "nodes": nodes}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    version = hashlib.sha256(canonical).hexdigest()
    release = root / "releases" / version
    for rel, data in payloads.items():
        path = release / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (release / "manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "version": version, "assets": {}, "nodes": nodes},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "current.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return version


def fields(prompt: str) -> dict[str, str]:
    return dict(re.findall(r"VAR=([^\n]+)\n(.*?)\n%%END%%\n", prompt, re.DOTALL))


def video(name="林川", extra="") -> str:
    return (
        f"subject_definitions:\n<Picture 1> is the first frame with {name}.\n"
        f"summary:\n[keyframe completion] {name} moves.\n"
        "retention_analysis:\n<Picture 1>: fully_preserved.\n"
        f"detailed_description:\n[Shot 1] <Picture 1> {name} moves. {extra}\n"
        "overall_soundscape:\nN/A\nnon_diegetic_music:\nN/A\n时长：6秒"
    )


class FakeModel:
    def __init__(self):
        self.calls = []
        self.failure_node = None
        self.failure_number = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        node = re.search(r"^NODE:(.+)$", prompt, re.M).group(1)
        release = re.search(r"^RELEASE:(.+)$", prompt, re.M).group(1)
        values = fields(prompt)
        self.calls.append((node, release, values))
        if (
            node == self.failure_node
            and sum(x[0] == node for x in self.calls) == self.failure_number
        ):
            output, finish = "truncated", "length"
        else:
            output, finish = self.respond(node, values), "stop"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {"role": "assistant", "content": output},
                    }
                ],
            },
        )

    @staticmethod
    def respond(node: str, values: dict[str, str]) -> str:
        if node == "context_summary":
            return json.dumps(
                {"story": "林川回屋。", "chapter": "林川拿钥匙。"}, ensure_ascii=False
            )
        if node == "asset.character":
            return json.dumps(
                [{"name": "林川", "aliases": "林川", "description": "A lean man."}],
                ensure_ascii=False,
            )
        if node in {"asset.scene", "asset.prop"}:
            return "[]"
        if node == "novel_to_script":
            return (
                f"{protocols.START}\n林川拿起钥匙。\n{protocols.RECORD}\n"
                f"林川走进房间。\n{protocols.END}"
            )
        if node == "rewrite_batch":
            return values["章节文案"]
        if node == "asset_binding":
            candidates = json.loads(values["候选资产"])
            return json.dumps(
                {
                    "asset_ids": [candidates[0]["asset_id"]],
                    "first_frame_asset_ids": [candidates[0]["asset_id"]],
                }
            )
        if node == "storyboard_batch":
            panels = json.loads(values["章节文案"])["results"]
            runtime = json.loads(values["运行约束"])
            rows = []
            for panel in panels:
                mappings = runtime["panel_assets"][str(panel["panel_index"])]
                image_refs = " ".join(
                    f"Use image {r['image']} as reference for {r['name']}."
                    for r in mappings["image_references"]
                )
                video_refs = " ".join(
                    f"<Picture {r['picture']}> is a reference for {r['name']}."
                    for r in mappings["video_references"]
                )
                rows.append(
                    f"{panel['panel_index']}{protocols.FIELD}{image_refs} A scene shows 林川."
                    f"{protocols.FIELD}{video(extra=video_refs)}"
                )
            return f"\n{protocols.RECORD}\n".join(rows)
        if node == "continuity":
            return json.dumps({"summary": "林川位于房间内。"}, ensure_ascii=False)
        raise AssertionError(node)


class FakeTemplate:
    def __init__(self, name, references=1):
        self.name = name
        self.reference_capacity = references
        self.audio_reference_capacity = 3 if name == "ref2va" else 0
        self.has_first_frame = name == "ref2va"
        self.capabilities = {
            "reference_capacity": references,
            "has_first_frame": self.has_first_frame,
            "audio_reference_capacity": self.audio_reference_capacity,
        }

    def prepare(self, prompt, **kwargs):
        return {"type": self.name, "prompt": prompt, **kwargs}


class FakeComfy:
    def __init__(self, fail_first_video=False):
        self.jobs = {}
        self.submissions = []
        self.waits = []
        self.uploads = []
        self.audio_uploads = []
        self.fail_first_video = fail_first_video

    def submit(self, graph):
        prompt_id = f"job-{len(self.jobs)}"
        self.jobs[prompt_id] = graph
        self.submissions.append(prompt_id)
        return prompt_id

    def wait(self, prompt_id, timeout=None):
        self.waits.append(prompt_id)
        if self.fail_first_video and self.jobs[prompt_id]["type"] == "ref2va":
            self.fail_first_video = False
            raise TimeoutError("injected timeout after submit")
        return {"id": prompt_id}

    def download_outputs(self, history, output_dir):
        prompt_id = history["id"]
        ext = ".mp4" if self.jobs[prompt_id]["type"] == "ref2va" else ".png"
        path = Path(output_dir) / (prompt_id + ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"test bytes for {prompt_id}".encode())
        return [path]

    def upload_image(self, path):
        self.uploads.append(str(path))
        return Path(path).name

    def upload_file(self, path):
        self.audio_uploads.append(str(path))
        return Path(path).name


class AmbiguousSubmitComfy(FakeComfy):
    """Accept the first remote job but drop its HTTP submission response."""

    def submit(self, graph):
        prompt_id = super().submit(graph)
        if len(self.submissions) == 1:
            raise TimeoutError("connection lost after remote acceptance")
        return prompt_id


class ProtocolTests(unittest.TestCase):
    def test_sdk_responses_adapter_detects_incomplete_and_returns_text(self):
        responses = iter(
            [
                {
                    "id": "resp-one",
                    "object": "response",
                    "created_at": 1,
                    "model": "mock",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "完成", "annotations": []}],
                        }
                    ],
                },
                {
                    "id": "resp-two",
                    "object": "response",
                    "created_at": 1,
                    "model": "mock",
                    "status": "incomplete",
                    "output": [],
                },
            ]
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))
        with httpx.Client(transport=transport) as client:
            sdk = OpenAI(
                api_key="test", base_url="http://fake.local/v1", http_client=client, max_retries=0
            )
            adapter = OpenAILLM(LLMConfig(model="mock", api="responses"), client=sdk)
            self.assertEqual(adapter.complete("你好"), "完成")
            with self.assertRaises(LLMError):
                adapter.complete("再试一次")

    def test_current_real_release_matches_engine_contracts(self):
        workspace = Path(__file__).resolve().parents[1]
        project = next(
            (p for p in (workspace / "prompts",) if (p / "current.json").is_file()), None
        )
        if project is None:
            self.skipTest("No current prompt release has been published yet")
        release = PromptRelease.load(project)
        for node, supplied in VARIABLES.items():
            with self.subTest(node=node):
                expected = set(release.manifest["nodes"][node]["variables"])
                self.assertLessEqual(expected, set(supplied))
                output = release.render(node, {name: "SENTINEL{{literal}}" for name in expected})
                self.assertIn("SENTINEL{{literal}}", output)

    def test_script_requires_complete_wrapper_and_single_line(self):
        with self.assertRaises(ValueError):
            protocols.script(f"{protocols.START}\nA")
        with self.assertRaises(ValueError):
            protocols.script(f"{protocols.START}\nA\nB\n{protocols.END}")
        self.assertEqual(
            protocols.script(f"{protocols.START}\nA\n{protocols.END}", 0)[0]["panel_index"], 0
        )

    def test_rewrites_reject_duplicates_missing_and_extra_fields(self):
        panels = [{"panel_index": 0, "text": "甲"}, {"panel_index": 1, "text": "乙"}]
        bad = [
            '{"results":[],"results":[]}',
            '{"results":[{"panel_index":0,"text":"甲"},{"panel_index":0,"text":"乙"}]}',
            '{"results":[{"panel_index":0,"text":"甲"}]}',
            '{"results":[{"panel_index":0,"text":"甲","extra":1},{"panel_index":1,"text":"乙"}]}',
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                protocols.rewrites(text, panels)

    def test_video_timestamps_and_picture_limits(self):
        panels = [{"panel_index": 0, "text": "甲"}]

        def row(prompt, body):
            return f"0{protocols.FIELD}{prompt}{protocols.FIELD}{body}"

        self.assertEqual(len(protocols.storyboards(row("a", video()), panels, video_capacity=1)), 1)
        for prompt, body in [
            ("Use image 2", video()),
            ("a", video(extra="<Picture 2>")),
            ("a", video(extra="[Shot 2] At 00:06.000")),
            ("a", video().replace("时长：6秒", "时长：16秒")),
        ]:
            with self.subTest(prompt=prompt, body=body), self.assertRaises(ValueError):
                protocols.storyboards(row(prompt, body), panels, image_capacity=1, video_capacity=1)
        protocols.storyboards(
            row("a", video(extra="<Audio 3>")), panels, video_capacity=1, audio_capacity=3
        )
        for body in (video(extra="<Audio 1>"), video(extra="<Audio 4>")):
            limit = 0 if "<Audio 1>" in body else 3
            with self.subTest(audio_limit=limit), self.assertRaises(ValueError):
                protocols.storyboards(
                    row("a", body), panels, video_capacity=1, audio_capacity=limit
                )

    def test_lock_excludes_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with RunLock(root):
                with self.assertRaises(RuntimeError):
                    with RunLock(root):
                        pass
            with RunLock(root):
                pass

    def test_asset_binding_rejects_fabricated_and_duplicate_ids(self):
        text = json.dumps(
            [
                {"name": "默认的林川", "aliases": "林川", "description": "A figure."},
                {"name": "白衬衫林川", "aliases": "林川", "description": "A figure in white."},
            ],
            ensure_ascii=False,
        )
        assets = parse_assets(text, "character", "林川走进房间。")
        self.assertNotEqual(assets[0]["asset_id"], assets[1]["asset_id"])
        selected = bindings(json.dumps({"asset_ids": [assets[1]["asset_id"]]}), assets)
        self.assertEqual([a["name"] for a in selected], ["白衬衫林川"])
        for ids in (["fabricated"], [assets[0]["asset_id"], assets[0]["asset_id"]]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                bindings(json.dumps({"asset_ids": ids}), assets)

    def test_later_asset_is_video_only_until_first_visible(self):
        assets = [
            {"name": "林川", "asset_id": "a", "kind": "character"},
            {"name": "小李", "asset_id": "b", "kind": "character"},
        ]
        selected = bindings(
            json.dumps({"asset_ids": ["a", "b"], "first_frame_asset_ids": ["a"]}), assets
        )
        self.assertEqual([a["first_frame"] for a in selected], [True, False])
        text = f"0{protocols.FIELD}林川 stands at the door.{protocols.FIELD}" + video(
            extra="Then 小李 enters the room."
        )
        panels = [{"panel_index": 0, "text": "林川等在门口，小李进屋。"}]
        rows = protocols.storyboards(text, panels, video_capacity=1, selected={0: selected})
        self.assertEqual(rows[0]["asset_ids"], ["a", "b"])
        self.assertEqual(rows[0]["first_frame_asset_ids"], ["a"])
        bad = json.dumps({"asset_ids": ["a"], "first_frame_asset_ids": ["b"]})
        with self.assertRaises(ValueError):
            bindings(bad, assets)

    def test_unbound_reference_is_rejected_even_when_workflow_has_spare_slots(self):
        panels = [{"panel_index": 0, "text": "空镜。"}]
        for prompt, body in [
            ("Use image 1 for the room.", video()),
            ("A room.", video(extra="<Picture 2> is the room.")),
        ]:
            row = f"0{protocols.FIELD}{prompt}{protocols.FIELD}{body}"
            with self.subTest(prompt=prompt, body=body), self.assertRaises(ValueError):
                protocols.storyboards(
                    row, panels, image_capacity=9, video_capacity=9, selected={0: []}
                )


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "prompt-project"
        self.project.mkdir()
        self.version = create_release(self.project, "first")
        self.model = FakeModel()
        transport = httpx.MockTransport(self.model.handler)
        self.http = httpx.Client(transport=transport)
        self.sdk = OpenAI(
            api_key="test-key",
            base_url="http://fake.local/v1",
            http_client=self.http,
            max_retries=0,
        )
        self.config = Config(
            prompt_project=str(self.project),
            asset_library=str(self.root / "assets.sqlite3"),
            project_id="integration-test",
            batch_size=1,
            strict_asset_validation=False,
            validation_retries=0,
            generation_mode="ref2va",
            llm=LLMConfig(model="mock-model"),
        )
        self.llm = OpenAILLM(self.config.llm, client=self.sdk)
        self.source = "林川拿起钥匙，走进房间。正文包含 {{literal}}。"
        self.patcher = patch(
            "pipeline.engine.get_default_template", side_effect=lambda name: FakeTemplate(name)
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.http.close()
        self.temporary.cleanup()

    def test_sdk_end_to_end_resume_and_release_pinning(self):
        output = self.root / "run-one"
        self.model.failure_node = "storyboard_batch"
        self.model.failure_number = 2
        with self.assertRaises(LLMError):
            Pipeline(self.config, llm=self.llm).run(self.source, output)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["prompt_version"], self.version)
        self.assertEqual(sum(x[0] == "novel_to_script" for x in self.model.calls), 1)
        self.assertIn("{{literal}}", self.model.calls[0][2]["小说原文"])

        second_version = create_release(self.project, "second")
        self.model.failure_node = None
        result = Pipeline(self.config, llm=self.llm).run(self.source, output, resume=True)
        self.assertEqual((result["status"], result["panels"], result["assets"]), ("inferred", 2, 1))
        self.assertEqual(result["prompt_version"], self.version)
        self.assertEqual(sum(x[0] == "novel_to_script" for x in self.model.calls), 1)
        self.assertEqual(
            [
                x["panel_index"]
                for x in json.loads((output / "panels.json").read_text(encoding="utf-8"))
            ],
            [0, 1],
        )
        continuation_calls = [call for call in self.model.calls if call[0] == "continuity"]
        self.assertEqual(continuation_calls[1][2]["前批末镜状态"], "林川位于房间内。")
        # All resumed requests are still rendered from the snapshot, not current.json.
        self.assertTrue(all(call[1] == "first" for call in self.model.calls))
        result2 = Pipeline(self.config, llm=self.llm).run(self.source, self.root / "run-two")
        self.assertEqual(result2["prompt_version"], second_version)
        self.assertTrue(any(call[1] == "second" for call in self.model.calls))

    def test_media_timeout_resumes_existing_remote_job_without_duplicate_submission(self):
        output = self.root / "render-run"
        for i in range(3):
            audio = self.root / f"audio-{i}.wav"
            audio.write_bytes(f"audio-{i}".encode())
            self.config.media.reference_audios.append(
                {"path": str(audio), "usage": f"reference {i}"}
            )
        comfy = FakeComfy(fail_first_video=True)
        with self.assertRaises(TimeoutError):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(self.source, output, render=True)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        video_entry = state["media"]["panel_0_video"]
        self.assertEqual(video_entry["status"], "submitted")
        saved_id = video_entry["prompt_id"]
        existing = list(comfy.submissions)
        result = Pipeline(self.config, llm=self.llm, comfy=comfy).run(
            self.source, output, resume=True, render=True
        )
        self.assertEqual(result["status"], "rendered")
        self.assertEqual(comfy.submissions[: len(existing)], existing)
        self.assertEqual(comfy.submissions.count(saved_id), 1)
        self.assertEqual(comfy.waits.count(saved_id), 2)
        self.assertEqual(len(comfy.submissions), 5)  # one asset + two images + two videos
        image_graph = comfy.jobs[existing[1]]
        video_graph = comfy.jobs[saved_id]
        self.assertEqual(image_graph["reference_images"], [Path(comfy.uploads[0]).name])
        self.assertEqual(video_graph["first_frame"], f"{existing[1]}.png")
        self.assertEqual(video_graph["reference_images"], [Path(comfy.uploads[0]).name])
        self.assertEqual(video_graph["length"], 6)
        self.assertNotIn("时长：6秒", video_graph["prompt"])
        self.assertEqual(video_graph["reference_audios"], [f"audio-{i}.wav" for i in range(3)])
        self.assertEqual(len(comfy.audio_uploads), 3)

    def test_resume_rejects_changed_source_and_tampered_snapshot(self):
        output = self.root / "integrity-run"
        Pipeline(self.config, llm=self.llm).run(self.source, output)
        with self.assertRaisesRegex(ValueError, "changed"):
            Pipeline(self.config, llm=self.llm).run(self.source + " changed", output, resume=True)
        prompt_file = output / "prompts" / "files" / "novel_to_script.txt"
        prompt_file.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            Pipeline(self.config, llm=self.llm).run(self.source, output, resume=True)
        # Updating the manifest checksum too must not make modified content
        # acceptable under the original immutable version identifier.
        manifest_file = output / "prompts" / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["nodes"]["novel_to_script"]["sha256"] = hashlib.sha256(b"tampered").hexdigest()
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version hash"):
            Pipeline(self.config, llm=self.llm).run(self.source, output, resume=True)

    def test_real_published_prompts_run_through_sdk_and_node_interfaces(self):
        workspace = Path(__file__).resolve().parents[1]
        project = next(
            (p for p in (workspace / "prompts",) if (p / "current.json").is_file()), None
        )
        if project is None:
            self.skipTest("No current prompt release has been published yet")
        self.config.prompt_project = str(project)
        original = PromptRelease.render

        def identify(release, node, values):
            # Preserve the actual release text and substitutions, adding test-only
            # labels so the fake HTTP service knows which node is being called.
            prompt = original(release, node, values)
            labels = f"NODE:{node}\nRELEASE:actual\n" + "".join(
                f"VAR={key}\n{value}\n%%END%%\n" for key, value in values.items()
            )
            return labels + "\n" + prompt

        with patch.object(PromptRelease, "render", identify):
            result = Pipeline(self.config, llm=self.llm).run(
                self.source, self.root / "real-release-run"
            )
        self.assertEqual((result["status"], result["panels"]), ("inferred", 2))
        self.assertEqual(result["prompt_version"], PromptRelease.load(project).version)

    def test_cli_dry_run_does_not_require_model_or_create_output(self):
        from pipeline.cli import main

        source_file = self.root / "source.txt"
        config_file = self.root / "config.json"
        output = self.root / "dry-run-output"
        source_file.write_text(self.source, encoding="utf-8")
        config_file.write_text(
            json.dumps(self.config.public_dict(), ensure_ascii=False), encoding="utf-8"
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main(
                [
                    "--env-file",
                    str(self.root / "missing.env"),
                    "run",
                    "--input",
                    str(source_file),
                    "--output",
                    str(output),
                    "--config",
                    str(config_file),
                    "--dry-run",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout.getvalue())["prompt_version"], self.version)
        self.assertFalse(output.exists())
        self.assertEqual(self.model.calls, [])

    def test_lost_submit_response_requires_explicit_attach_before_resume(self):
        from pipeline.cli import main

        output = self.root / "lost-submit"
        comfy = AmbiguousSubmitComfy()
        with self.assertRaises(TimeoutError):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(self.source, output, render=True)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        ((key, job),) = state["media"].items()
        self.assertEqual(job["status"], "submitting")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(
                self.source, output, resume=True, render=True
            )
        self.assertEqual(comfy.submissions, ["job-0"])
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(
                [
                    "--env-file",
                    str(self.root / "missing.env"),
                    "attach-job",
                    "--run",
                    str(output),
                    "--key",
                    key,
                    "--prompt-id",
                    "job-0",
                ]
            )
        self.assertEqual(rc, 0)
        result = Pipeline(self.config, llm=self.llm, comfy=comfy).run(
            self.source, output, resume=True, render=True
        )
        self.assertEqual(result["status"], "rendered")
        self.assertEqual(comfy.submissions.count("job-0"), 1)

    def test_established_asset_is_available_when_chapter_extraction_is_empty(self):
        library = AssetLibrary(self.config.asset_library, self.config.project_id)
        imported = {
            "asset_id": "established-character",
            "kind": "character",
            "name": "林川",
            "aliases": "林川",
            "description": "Previously established appearance.",
        }
        library.register([imported])
        original_respond = self.model.respond
        self.model.respond = lambda node, values: (
            "[]" if node == "asset.character" else original_respond(node, values)
        )
        output = self.root / "established-asset-run"
        result = Pipeline(self.config, llm=self.llm).run(self.source, output)
        self.assertEqual(result["assets"], 1)
        calls = [c for c in self.model.calls if c[0] == "asset_binding"]
        self.assertEqual(
            json.loads(calls[0][2]["候选资产"])[0]["asset_id"], "established-character"
        )
        saved = json.loads((output / "panels.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["asset_ids"], ["established-character"])

    def test_latest_link_is_selected_once_and_pinned_during_resume(self):
        library = AssetLibrary(self.config.asset_library, self.config.project_id)
        initial = parse_assets(
            '[{"name":"林川","aliases":"林川","description":"A lean man."}]',
            "character",
            self.source,
        )[0]
        library.register([initial])
        inputs = []
        for index in range(3):
            image = self.root / f"user-reference-{index}.png"
            image.write_bytes(f"user reference {index}".encode())
            inputs.append(image)
        library.link_file(initial["asset_id"], inputs[0])
        newest_before_run = library.link_file(initial["asset_id"], inputs[1])
        comfy = FakeComfy(fail_first_video=True)
        output = self.root / "pinned-reference-run"
        with self.assertRaises(TimeoutError):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(self.source, output, render=True)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            state["asset_generation_refs"][initial["asset_id"]]["sha256"],
            newest_before_run["sha256"],
        )
        generated_media = state["asset_media"][initial["asset_id"]]
        self.assertNotEqual(generated_media["sha256"], newest_before_run["sha256"])
        newer_link = library.link_file(initial["asset_id"], inputs[2])
        Pipeline(self.config, llm=self.llm, comfy=comfy).run(
            self.source, output, resume=True, render=True
        )
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            state["asset_generation_refs"][initial["asset_id"]]["sha256"],
            newest_before_run["sha256"],
        )
        self.assertEqual(state["asset_media"][initial["asset_id"]], generated_media)
        newer_output = self.root / "new-reference-run"
        Pipeline(self.config, llm=self.llm, comfy=FakeComfy()).run(
            self.source, newer_output, render=True
        )
        state = json.loads((newer_output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            state["asset_generation_refs"][initial["asset_id"]]["sha256"], newer_link["sha256"]
        )

    def test_resume_rejects_tampered_pinned_asset_image(self):
        output = self.root / "tampered-asset-run"
        comfy = FakeComfy(fail_first_video=True)
        with self.assertRaises(TimeoutError):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(self.source, output, render=True)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        (pinned,) = state["asset_media"].values()
        Path(pinned["local_path"]).write_bytes(b"tampered image")
        prior_jobs = list(comfy.submissions)
        with self.assertRaisesRegex(ValueError, "integrity"):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(
                self.source, output, resume=True, render=True
            )
        self.assertEqual(comfy.submissions, prior_jobs)

    def test_video_only_asset_not_bound_to_first_frame(self):
        original_respond = self.model.respond

        def respond(node, values):
            if node == "asset.character":
                return json.dumps(
                    [
                        {"name": "林川", "aliases": "林川", "description": "A lean man."},
                        {"name": "小李", "aliases": "小李", "description": "A slim woman."},
                    ],
                    ensure_ascii=False,
                )
            if node == "asset_binding":
                candidates = json.loads(values["候选资产"])
                return json.dumps(
                    {
                        "asset_ids": [a["asset_id"] for a in candidates],
                        "first_frame_asset_ids": [candidates[0]["asset_id"]],
                    }
                )
            if node == "storyboard_batch":
                panels = json.loads(values["章节文案"])["results"]
                runtime = json.loads(values["运行约束"])
                rows = []
                for panel in panels:
                    mapping = runtime["panel_assets"][str(panel["panel_index"])]
                    image = " ".join(
                        f"Use image {ref['image']} for {ref['name']}."
                        for ref in mapping["image_references"]
                    )
                    refs = " ".join(
                        f"<Picture {ref['picture']}> is {ref['name']}."
                        for ref in mapping["video_references"]
                    )
                    rows.append(
                        f"{panel['panel_index']}{protocols.FIELD}{image} 林川 waits."
                        f"{protocols.FIELD}{video(extra=refs + ' 小李 then enters.')}"
                    )
                return f"\n{protocols.RECORD}\n".join(rows)
            return original_respond(node, values)

        self.model.respond = respond
        source = self.source + " 后来小李进屋。"
        comfy = FakeComfy()
        output = self.root / "video-only-asset"
        with patch(
            "pipeline.engine.get_default_template",
            side_effect=lambda name: FakeTemplate(name, references=2),
        ):
            Pipeline(self.config, llm=self.llm, comfy=comfy).run(source, output, render=True)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        first, later = state["assets"]
        first_name = Path(state["asset_media"][first["asset_id"]]["local_path"]).name
        later_name = Path(state["asset_media"][later["asset_id"]]["local_path"]).name
        image_job = state["media"]["panel_0_image"]["prompt_id"]
        video_job = state["media"]["panel_0_video"]["prompt_id"]
        self.assertEqual(comfy.jobs[image_job]["reference_images"], [first_name])
        self.assertEqual(comfy.jobs[video_job]["reference_images"], [first_name, later_name])

    def test_opaque_asset_job_key_cannot_escape_run_directory(self):
        pipeline = Pipeline(self.config, llm=self.llm, comfy=FakeComfy())
        pipeline.root = self.root / "safe-run"
        pipeline.root.mkdir()
        pipeline.state = {"media": {}}
        paths = pipeline._job("asset_../../outside", {"type": "qwen", "prompt": "test"})
        self.assertTrue(
            all(Path(path).resolve().is_relative_to(pipeline.root.resolve()) for path in paths)
        )
        self.assertFalse((self.root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
