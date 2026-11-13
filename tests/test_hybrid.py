"""Hybrid planning and rendering using the real SDK with in-process transports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from test_pipeline import VARIABLES, FakeComfy, FakeModel, FakeTemplate, create_release

from pipeline import protocols
from pipeline.config import Config, LLMConfig
from pipeline.engine import Pipeline
from pipeline.llm import OpenAILLM
from pipeline.shots import RunShots
from pipeline.storage import fingerprint, write_json
from prompt_project import PromptRelease


def i2va_video(name="林川"):
    return (
        protocols.I2VA_ANCHOR
        + f"\n\nintegrated_multimodal_description:\n[Shot 1] {name} moves naturally.\n"
        + "overall_soundscape:\nN/A\nnon_diegetic_music:\nN/A\n时长：6秒"
    )


class HybridModel(FakeModel):
    def respond(self, node, values):
        if node == "shot_plan":
            panels = json.loads(values["章节文案"])["results"]
            return json.dumps(
                {
                    "shots": [
                        {
                            "panel_index": panel["panel_index"],
                            "generation_mode": "i2va"
                            if panel["panel_index"] % 2 == 0
                            else "ref2va",
                            "reason": "根据镜头需求选择",
                        }
                        for panel in panels
                    ]
                },
                ensure_ascii=False,
            )
        if node == "storyboard_i2va_batch":
            panels = json.loads(values["章节文案"])["results"]
            runtime = json.loads(values["运行约束"])
            rows = []
            for panel in panels:
                refs = runtime["panel_assets"][str(panel["panel_index"])]["image_references"]
                image = " ".join(f"Use image {item['image']} for {item['name']}." for item in refs)
                rows.append(
                    f"{panel['panel_index']}{protocols.FIELD}{image} 林川 waits."
                    f"{protocols.FIELD}{i2va_video()}"
                )
            return f"\n{protocols.RECORD}\n".join(rows)
        return super().respond(node, values)


class HybridTemplate(FakeTemplate):
    def __init__(self, name):
        super().__init__(name)
        if name == "i2va":
            self.reference_capacity = 0
            self.audio_reference_capacity = 0
            self.has_first_frame = True
            self.capabilities = {
                "reference_capacity": 0,
                "has_first_frame": True,
                "audio_reference_capacity": 0,
            }


class HybridComfy(FakeComfy):
    def download_outputs(self, history, output_dir):
        prompt_id = history["id"]
        ext = ".mp4" if self.jobs[prompt_id]["type"] in {"i2va", "ref2va"} else ".png"
        path = Path(output_dir) / (prompt_id + ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"test bytes for {prompt_id}".encode())
        return [path]


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "prompts"
        self.project.mkdir()
        extra = {
            "shot_plan": ["章节文案", "运行约束"],
            "storyboard_i2va_batch": VARIABLES["storyboard_batch"],
        }
        with patch.dict(VARIABLES, extra):
            create_release(self.project, "hybrid")
        self.model = HybridModel()
        self.http = httpx.Client(transport=httpx.MockTransport(self.model.handler))
        self.sdk = OpenAI(
            api_key="test", base_url="http://mock.local/v1", http_client=self.http, max_retries=0
        )
        self.config = Config(
            prompt_project=str(self.project),
            asset_library=str(self.root / "db.sqlite3"),
            project_id="hybrid",
            batch_size=2,
            strict_asset_validation=False,
            validation_retries=0,
            llm=LLMConfig(model="mock"),
        )
        self.llm = OpenAILLM(self.config.llm, client=self.sdk)
        self.source = "林川拿起钥匙，走进房间。"
        self.template_patch = patch(
            "pipeline.engine.get_default_template", side_effect=HybridTemplate
        )
        self.template_patch.start()

    def tearDown(self):
        self.template_patch.stop()
        self.http.close()
        self.temp.cleanup()

    def test_mixed_modes_use_distinct_protocols_and_real_bindings(self):
        audio = self.root / "voice.wav"
        audio.write_bytes(b"sound")
        self.config.media.reference_audios = [{"path": str(audio), "usage": "voice"}]
        comfy = HybridComfy()
        output = self.root / "mixed"
        with Pipeline(self.config, llm=self.llm, comfy=comfy) as pipeline:
            result = pipeline.run(self.source, output, render=True)
        self.assertEqual(result["panels"], 2)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual([p["generation_mode"] for p in state["panels"]], ["i2va", "ref2va"])
        self.assertEqual([p["mode_source"] for p in state["panels"]], ["llm", "llm"])
        planner = next(call for call in self.model.calls if call[0] == "shot_plan")
        self.assertEqual(json.loads(planner[2]["运行约束"])["generation_mode"], "auto")
        first = state["media"]["panel_0_video"]["prompt_id"]
        second = state["media"]["panel_1_video"]["prompt_id"]
        self.assertEqual(comfy.jobs[first]["type"], "i2va")
        self.assertTrue(comfy.jobs[first]["first_frame"].endswith(".png"))
        self.assertEqual(comfy.jobs[first]["reference_images"], [])
        self.assertEqual(comfy.jobs[first]["reference_audios"], [])
        self.assertEqual(comfy.jobs[second]["type"], "ref2va")
        self.assertEqual(len(comfy.jobs[second]["reference_images"]), 1)
        self.assertEqual(comfy.jobs[second]["reference_audios"], ["voice.wav"])
        ref_call = next(call for call in self.model.calls if call[0] == "storyboard_batch")
        self.assertEqual(
            json.loads(ref_call[2]["前面分镜:2"])["previous_panels"][0]["generation_mode"], "i2va"
        )

    def test_user_override_invalidates_and_recomputes_without_repeating_upstream(self):
        output = self.root / "override"
        Pipeline(self.config, llm=self.llm).run(self.source, output)
        manager = RunShots(output)
        old = manager.get(0)
        self.assertEqual(manager.set_mode(0, "i2va"), old)  # no unnecessary re-run
        changed = manager.set_mode(0, "ref2va")
        self.assertEqual(
            (changed["generation_mode"], changed["status"]), ("ref2va", "needs_recompute")
        )
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "stale")
        self.assertFalse(
            any(
                key.startswith("storyboard_") or key.startswith("continuity_")
                for key in state["nodes"]
            )
        )
        self.assertTrue((output / state["mode_changes"][-1]).is_file())
        count = len(self.model.calls)
        Pipeline(self.config, llm=self.llm).run(self.source, output, resume=True)
        new_calls = self.model.calls[count:]
        self.assertEqual([call[0] for call in new_calls], ["storyboard_batch", "continuity"])
        updated = manager.list()
        self.assertEqual([p["generation_mode"] for p in updated], ["ref2va", "ref2va"])
        self.assertEqual(updated[0]["mode_source"], "manual")

    def test_mode_change_rejected_after_media_submission(self):
        output = self.root / "locked"
        Pipeline(self.config, llm=self.llm, comfy=HybridComfy()).run(
            self.source, output, render=True
        )
        with self.assertRaisesRegex(RuntimeError, "media was submitted"):
            RunShots(output).set_mode(0, "ref2va")
        self.assertEqual(RunShots(output).get(0)["generation_mode"], "i2va")

    def test_explicit_mode_skips_classifier(self):
        self.config.generation_mode = "i2va"
        output = self.root / "forced"
        Pipeline(self.config, llm=self.llm).run(self.source, output)
        self.assertEqual([p["generation_mode"] for p in RunShots(output).list()], ["i2va", "i2va"])
        self.assertFalse(any(call[0] == "shot_plan" for call in self.model.calls))

    def test_current_published_hybrid_nodes_with_real_sdk_transport(self):
        project = Path(__file__).resolve().parents[1] / "prompts"
        if not (project / "current.json").is_file():
            self.skipTest("No current prompt release")
        release = PromptRelease.load(project)
        if not {"shot_plan", "storyboard_i2va_batch"}.issubset(release.manifest["nodes"]):
            self.skipTest("The current prompt release does not yet contain hybrid nodes")
        self.config.prompt_project = str(project)
        original = PromptRelease.render

        def identify(current, node, values):
            actual = original(current, node, values)
            labels = f"NODE:{node}\nRELEASE:actual\n" + "".join(
                f"VAR={key}\n{value}\n%%END%%\n" for key, value in values.items()
            )
            return labels + "\n" + actual

        with patch.object(PromptRelease, "render", identify):
            result = Pipeline(self.config, llm=self.llm).run(
                self.source, self.root / "real-release"
            )
        self.assertEqual(result["prompt_version"], release.version)
        self.assertEqual(
            [p["generation_mode"] for p in RunShots(result["output"]).list()], ["i2va", "ref2va"]
        )

    def test_legacy_ref2va_checkpoint_resumes_without_new_prompt_nodes(self):
        self.config.generation_mode = "ref2va"
        output = self.root / "legacy"
        pipeline = Pipeline(self.config, llm=self.llm)
        pipeline.run(self.source, output)
        state = json.loads((output / "state.json").read_text(encoding="utf-8"))
        old_config = state["identity"]["config"]
        old_config.pop("generation_mode")
        old_config["media"].pop("i2va_profile")
        old_caps = {
            "image": pipeline.image_template.capabilities,
            "video": pipeline.video_template.capabilities,
            "min_seconds": self.config.min_seconds,
            "max_seconds": self.config.max_seconds,
        }
        state["identity"]["workflows"] = fingerprint(
            {
                "image": pipeline._workflow_identity(pipeline.image_template),
                "video": pipeline._workflow_identity(pipeline.video_template),
                "capabilities": old_caps,
            }
        )
        write_json(output / "state.json", state)
        self.config.generation_mode = "auto"
        resumed = Pipeline(self.config, llm=self.llm)
        result = resumed.run(self.source, output, resume=True)
        self.assertEqual(result["status"], "inferred")
        self.assertTrue(
            all(shot["generation_mode"] == "ref2va" for shot in RunShots(output).list())
        )
        # Reusing the same object must not force a new run into legacy mode.
        fresh = resumed.run(self.source, self.root / "after-legacy")
        self.assertEqual(
            [shot["generation_mode"] for shot in RunShots(fresh["output"]).list()],
            ["i2va", "ref2va"],
        )

    def test_planner_and_i2va_reject_bad_structure(self):
        with self.assertRaises(ValueError):
            protocols.shot_plan(
                '{"shots":[{"panel_index":0,"generation_mode":"invalid","reason":"x"}]}',
                [{"panel_index": 0}],
            )
        with self.assertRaises(ValueError):
            protocols.shot_plan(
                '{"shots":[{"panel_index":1,"generation_mode":"i2va","reason":"x"}]}',
                [{"panel_index": 0}],
            )
        for bad in (
            i2va_video().replace("<Picture 1>", "<Picture 2>", 1),
            i2va_video().replace("N/A", "<Audio 1>", 1),
            i2va_video().replace("integrated_multimodal_description", "detailed_description"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                protocols.storyboards(
                    f"0{protocols.FIELD}林川 stands.{protocols.FIELD}{bad}",
                    [{"panel_index": 0}],
                    image_capacity=0,
                    video_capacity=1,
                    selected={0: [{"asset_id": "x", "name": "林川"}]},
                    mode="i2va",
                )


if __name__ == "__main__":
    unittest.main()
