"""Director -> pure shot adapter -> H3 compiler integration without network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from openai import OpenAI
from test_shot_client import PNG, SOURCE, TABLE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from director_client import DirectorClient  # noqa: E402
from h3_client import H3Client, ReferenceImage  # noqa: E402
from shot_client import ShotClient  # noqa: E402

H3_PROMPT = """subject_definitions:
<Subject 1> is the girl in <Picture 1>.
<Subject 2> is the door in <Picture 2>.
summary:
[reference generation] <Subject 1> opens <Subject 2>.
retention_analysis:
<Subject 1>: fully_preserved - preserve the girl.
<Subject 2>: fully_preserved - preserve the door.
detailed_description:
[Shot 1] <Subject 1> opens <Subject 2> with her left hand and, from 1 to 2 seconds, (S1) says, <d>[Chinese] 今天不打开。</d>
[Shot 2] At 00:03.000, she holds her position.
overall_soundscape:
Not specified in the source storyboard.
non_diegetic_music:
Not specified in the source storyboard."""


class ShotH3BridgeTests(unittest.TestCase):
    def test_director_table_through_shot_to_h3_with_two_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for index, name in enumerate(("girl", "door")):
                path = root / f"{name}.png"
                path.write_bytes(PNG + bytes([index]))
                images.append(ReferenceImage(path, name=name, remote_name=f"codex/{name}.png"))
            requests: list[dict] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(json.loads(request.content))
                content = TABLE if len(requests) == 1 else json.dumps({"prompt": H3_PROMPT})
                return httpx.Response(
                    200,
                    json={
                        "id": "test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": content},
                            }
                        ],
                    },
                )

            sdk = OpenAI(
                api_key="test-key",
                base_url="https://example.test/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            self.addCleanup(sdk.close)
            directed = DirectorClient(model="director", sdk_client=sdk).direct(
                "ref2va", SOURCE, images, 6
            )
            shot = ShotClient().process(
                directed.table_text,
                type="ref2va",
                source_text=SOURCE,
                reference_images=images,
                duration_seconds=6,
            )
            self.assertEqual(shot.status, "ready", shot.issues)
            request = shot.to_h3_request(values={"seed": 42})
            self.assertEqual(len(requests), 1, "ShotClient must not make an LLM request")
            self.assertEqual(request["storyboard"].count("今天不打开。"), 1)
            compiled = H3Client(model="compiler", sdk_client=sdk).compile(
                request["type"],
                request["storyboard"],
                [ReferenceImage(**image) for image in request["reference_images"]],
            )
            self.assertEqual(compiled.prompt, H3_PROMPT)
            self.assertIn("<d>[Chinese] 今天不打开。</d>", compiled.prompt)
            self.assertEqual(len(requests), 2)
