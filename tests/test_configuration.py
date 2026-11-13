from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import protocols
from pipeline.assets import bindings, parse_assets
from pipeline.config import Config, LLMConfig, MediaConfig, load_dotenv
from pipeline.storage import RunLock


class ConfigurationTests(unittest.TestCase):
    def test_json_values_override_environment_and_paths_are_relative_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "prompt_project": "../prompts",
                        "asset_library": "db/assets.sqlite3",
                        "llm": {"model": "from-json", "base_url": "https://json.example/v1"},
                        "media": {
                            "reference_audios": [
                                {"path": "voice.wav", "usage": "voice"},
                                {"path": "remote/voice.wav", "usage": "voice", "remote": True},
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"LLM_MODEL": "from-env", "OPENAI_BASE_URL": "https://env.example/v1"}
            ):
                config = Config.load(config_file)
            self.assertEqual(config.llm.model, "from-json")
            self.assertEqual(config.llm.base_url, "https://json.example/v1")
            self.assertEqual(config.prompt_project, str((root / "../prompts").resolve()))
            self.assertEqual(config.asset_library, str((root / "db/assets.sqlite3").resolve()))
            self.assertEqual(
                config.media.reference_audios[0]["path"], str((root / "voice.wav").resolve())
            )
            self.assertEqual(config.media.reference_audios[1]["path"], "remote/voice.wav")
            self.assertNotIn("remote", config.media.reference_audios[0])
            json.dumps(config.public_dict())

    def test_empty_model_placeholders_use_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"llm":{"model":"","base_url":""}}', encoding="utf-8")
            with patch.dict(
                os.environ, {"LLM_MODEL": "from-env", "OPENAI_BASE_URL": "https://env.example/v1"}
            ):
                config = Config.load(path)
            self.assertEqual(
                (config.llm.model, config.llm.base_url), ("from-env", "https://env.example/v1")
            )

    def test_shared_llm_config_uses_the_same_environment_mapping(self):
        from llm.config import LLMConfig as SharedLLMConfig

        with patch.dict(
            os.environ, {"LLM_MODEL": "shared-model", "OPENAI_BASE_URL": "https://llm.example/v1"}
        ):
            shared = SharedLLMConfig.from_env()
            pipeline = Config.load().llm
            self.assertIs(type(pipeline), SharedLLMConfig)
            self.assertEqual(shared.model_dump(), pipeline.model_dump())

    def test_invalid_types_unknown_fields_and_audio_limits_are_rejected(self):
        invalid = (
            {"batch_size": True},
            {"batch_size": "4"},
            {"project_id": " "},
            {"media": {"seed": True}},
            {"media": {"asset_width": 1000}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b", "remote": "false"}]}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b", "unknown": 1}]}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b"}] * 4}},
            {"min_seconds": 16, "max_seconds": 15},
            {"llm": {"api": "unsupported"}},
            {"unknown": "value"},
        )
        for config_data in invalid:
            with self.subTest(config_data=config_data), self.assertRaises(ValueError):
                Config.model_validate(config_data)

    def test_runtime_audio_mutations_are_validated_before_serialization(self):
        config = Config()
        config.media.reference_audios.append({"path": "voice.wav", "usage": ""})
        with self.assertRaises(ValueError):
            config.public_dict()
        config.media.reference_audios[0]["usage"] = "reference voice"
        self.assertEqual(config.public_dict()["media"]["reference_audios"][0]["path"], "voice.wav")

    def test_secrets_are_not_serialized_or_in_validation_errors(self):
        secret = "fake-sensitive-credential"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            self.assertNotIn(secret, json.dumps(Config().public_dict()))
            for data in (
                {"base_url": f"https://user:{secret}@example.org"},
                {"base_url": f"https://example.org?api_key={secret}"},
                {"extra": {"extra_headers": {"Authorization": secret}}},
            ):
                with self.subTest(data_key=next(iter(data))), self.assertRaises(ValueError) as ctx:
                    LLMConfig(**data)
                self.assertNotIn(secret, str(ctx.exception))
            config = Config()
            config.llm.extra["extra_headers"] = {"Authorization": secret}
            with self.assertRaises(ValueError) as ctx:
                config.public_dict()
            self.assertNotIn(secret, str(ctx.exception))

    def test_dotenv_does_not_override_process_environment_or_expand_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "PIPELINE_TEST_VALUE=file\nPIPELINE_TEST_LITERAL=${PIPELINE_TEST_VALUE}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PIPELINE_TEST_VALUE": "process"}):
                os.environ.pop("PIPELINE_TEST_LITERAL", None)
                load_dotenv(env_file)
                self.assertEqual(os.environ["PIPELINE_TEST_VALUE"], "process")
                self.assertEqual(os.environ["PIPELINE_TEST_LITERAL"], "${PIPELINE_TEST_VALUE}")

    def test_instance_models_preserve_direct_use(self):
        config = Config(llm=LLMConfig(model="example"), media=MediaConfig())
        config.llm.model = "updated"
        self.assertEqual(config.public_dict()["llm"]["model"], "updated")
        with self.assertRaises(ValueError):
            config.media.seed = True


class OutputSchemaTests(unittest.TestCase):
    def test_outputs_use_strict_models_and_reject_duplicate_json_keys(self):
        self.assertEqual(
            protocols.summary('{"story":"x","chapter":"y"}'), {"story": "x", "chapter": "y"}
        )
        for value in (
            '{"story":1,"chapter":"y"}',
            '{"story":"x","chapter":"y","extra":0}',
            '{"story":"x","story":"y","chapter":"y"}',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocols.summary(value)
        with self.assertRaises(ValueError):
            protocols.continuity('{"summary":null}')
        with self.assertRaises(ValueError):
            protocols.rewrites(
                '{"results":[{"panel_index":true,"text":"x"}]}', [{"panel_index": 1}]
            )

    def test_asset_structure_and_first_frame_subset_are_validated(self):
        text = '[{"name":"阿青","aliases":"阿青","description":"A coat."}]'
        assets = parse_assets(text, "character", "阿青穿外套。")
        chosen = bindings(json.dumps({"asset_ids": [assets[0]["asset_id"]]}), assets)
        self.assertTrue(chosen[0]["first_frame"])
        for bad in (
            '[{"name":"阿青","aliases":"阿青","description":"coat","extra":1}]',
            '[{"name":"阿青","aliases":"阿青","description":3}]',
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_assets(bad, "character", "阿青")
        with self.assertRaises(ValueError):
            bindings('{"asset_ids":[],"first_frame_asset_ids":null}', assets)


class LockTests(unittest.TestCase):
    def test_existing_live_legacy_pid_lock_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".run.lock").write_text(str(os.getpid()), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "legacy process"):
                with RunLock(root):
                    pass

    def test_dead_legacy_pid_lock_can_be_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".run.lock").write_text("9999999999", encoding="utf-8")
            with patch("pipeline.storage._process_is_alive", return_value=False):
                with RunLock(root):
                    pass

    def test_lock_file_from_previous_process_does_not_block_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".run.lock").write_text("old marker", encoding="utf-8")
            with RunLock(root):
                with self.assertRaises(RuntimeError):
                    with RunLock(root):
                        pass
            with RunLock(root):
                pass

    def test_termination_releases_cross_process_lock(self):
        code = (
            "from pathlib import Path; import sys; from pipeline.storage import RunLock; "
            "lock=RunLock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked', flush=True); sys.stdin.buffer.read(1)"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(RuntimeError):
                    with RunLock(root):
                        pass
            finally:
                child.terminate()
                child.communicate(timeout=10)
            with RunLock(root):
                pass


if __name__ == "__main__":
    unittest.main()
