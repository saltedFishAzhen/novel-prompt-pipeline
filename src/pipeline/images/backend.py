"""Capabilities of the Codex-operated image task backend.

The backend describes a handoff to an agent. It never imitates a ComfyUI graph
or claims that the image tool accepts a seed or exact pixel dimensions.
"""

from __future__ import annotations


class CodexImageBackend:
    name = "codex"
    has_first_frame = False
    reference_capacity = 10

    @property
    def capabilities(self) -> dict:
        return {
            "reference_capacity": self.reference_capacity,
            "audio_reference_capacity": 0,
            "has_first_frame": False,
            "supports_prompt": True,
            "supports_reference_images": True,
            "supports_reference_audios": False,
            "supports_seed": False,
            "supports_width": False,
            "supports_height": False,
            "supports_length": False,
            "dimension_policy": "requested as guidance; actual dimensions are recorded",
        }

    def identity(self) -> dict:
        return {
            "provider": self.name,
            "protocol": "codex-image-task-v1",
            "capabilities": self.capabilities,
        }
