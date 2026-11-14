from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.config import Config, MediaConfig, load_dotenv
from pipeline.storage import RunLock


class ConfigurationTests(unittest.TestCase):
    def test_paths_are_relative_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "asset_library": "db/assets.sqlite3",
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
            config = Config.load(config_file)
            self.assertEqual(config.asset_library, str((root / "db/assets.sqlite3").resolve()))
            self.assertEqual(
                config.media.reference_audios[0]["path"], str((root / "voice.wav").resolve())
            )
            self.assertEqual(config.media.reference_audios[1]["path"], "remote/voice.wav")
            self.assertNotIn("remote", config.media.reference_audios[0])
            json.dumps(config.public_dict())

    def test_invalid_types_unknown_fields_and_audio_limits_are_rejected(self):
        invalid = (
            {"production": {"workers": True}},
            {"production": {"workers": "4"}},
            {"production": {"episodes_per_batch": 4}},
            {"project_id": " "},
            {"media": {"seed": True}},
            {"media": {"asset_width": 1000}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b", "remote": "false"}]}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b", "unknown": 1}]}},
            {"media": {"reference_audios": [{"path": "a", "usage": "b"}] * 4}},
            {"production": {"minutes_per_episode": 0}},
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
        config = Config(media=MediaConfig())
        config.media.seed = 42
        self.assertEqual(config.public_dict()["media"]["seed"], 42)
        with self.assertRaises(ValueError):
            config.media.seed = True


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
