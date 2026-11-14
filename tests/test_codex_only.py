from __future__ import annotations

import json

import pytest

from comfyui_client import BindingError, get_default_template


def test_only_video_graphs_are_bundled_and_ref2va_i2va_remain_usable():
    from comfyui_client.templates import BINDINGS_FILE

    profiles = json.loads(BINDINGS_FILE.read_text(encoding="utf-8"))["profiles"]
    assert set(profiles) == {"ref2va", "i2va"}
    for name in profiles:
        template = get_default_template(name)
        graph = template.prepare("video prompt", first_frame="input/first.png")
        assert graph and template.has_first_frame
    with pytest.raises((BindingError, ValueError, KeyError)):
        get_default_template("qwen")
