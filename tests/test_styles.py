from __future__ import annotations

from pathlib import Path

import pytest

from comfyui_client import BindingError, get_default_template
from pipeline.config import Config
from pipeline.styles import BUILTIN_STYLES, StyleConfig, StylePreset


def selected_style():
    return StyleConfig(
        preset="custom",
        presets={
            "custom": StylePreset(prefix="VISUAL PREFIX", suffix="VISUAL SUFFIX", negative="BLUR")
        },
    )


@pytest.mark.parametrize("profile", ["i2va", "ref2va"])
def test_current_h3_workflows_reject_unsupported_negative(profile):
    with pytest.raises(BindingError, match="negative_prompt"):
        get_default_template(profile).prepare(
            "video", first_frame="first.png", negative_prompt="BLUR"
        )


def test_style_overrides_and_defaults():
    config = StyleConfig()
    assert config.preset == "cinematic_anime_cg"
    assert config.compose_image("original") == "\n\n".join(
        (config.resolved()["prefix"], "original", config.resolved()["suffix"])
    )
    assert Config().public_dict()["style"]["preset"] == "cinematic_anime_cg"
    assert set(BUILTIN_STYLES) == {"cinematic_anime_cg"}
    with pytest.raises(ValueError, match="Unknown visual style preset"):
        StyleConfig(preset="anime")
    style = selected_style()
    assert style.resolved()["image_negative"] == ""
    assert style.compose_image("original") == "VISUAL PREFIX\n\noriginal\n\nVISUAL SUFFIX"
    style.image_negative = "NOISE"
    assert style.resolved()["image_negative"] == ""
    with pytest.raises(ValueError, match="video_negative"):
        StyleConfig.model_validate({"video_negative": "must not be ignored"})


def test_project_default_style_is_consistent_across_config_and_sdk():
    example = Path(__file__).resolve().parents[1] / "configs" / "pipeline.example.json"
    config = Config.load(example)
    assert config.style.preset == "cinematic_anime_cg"
    assert StyleConfig().preset == Config.load().style.preset == "cinematic_anime_cg"
    style = config.style.resolved()
    assert "三维" in style["prefix"] and "动漫" in style["prefix"]
    assert "年龄" in style["suffix"] and "光源" in style["suffix"]
    assert style["image_negative"] == ""
    assert all(word not in style["prefix"].lower() for word in ("sunset", "school", "young"))


def test_project_default_style_can_be_customized():
    style = StyleConfig.model_validate(
        {
            "preset": "cinematic_anime_cg",
            "presets": {"cinematic_anime_cg": {"prefix": "My style."}},
        }
    )
    assert style.compose_image("An object.") == "My style.\n\nAn object."


def test_scene_style_prioritizes_camera_without_portrait_constraints():
    style = StyleConfig()
    original = style.resolved()
    request = "Look back toward the entrance, with the window behind the new camera."
    text = style.compose_scene_image(request, "reverse")
    assert text.startswith(request + "\n\n")
    assert "立体 CG 场景" in text
    assert "光晕" in text
    for portrait_term in ("椭圆面部轮廓", "上眼睑褶皱", "唇峰", "圆形散景"):
        assert portrait_term not in text
    assert "不沿用其原机位" in text
    assert style.resolved() == original  # The user's character/global style is unchanged.


def test_scene_style_override_and_custom_preset_fallback():
    custom = StyleConfig(
        preset="custom",
        presets={"custom": StylePreset(prefix="Original prefix", suffix="Original suffix")},
    )
    assert "Original prefix" in custom.compose_scene_image("Camera instruction", "overview")
    assert custom.compose_scene_image("Master scene", "master").startswith("Original prefix")
    custom.presets["custom"].scene_prefix = "Environment appearance"
    custom.presets["custom"].scene_suffix = "Environment finish"
    text = custom.compose_scene_image("Camera instruction", "overview")
    assert text.startswith("Camera instruction\n\n")
    assert "Environment appearance" in text and "Environment finish" in text
    assert "Original prefix" not in text
    with pytest.raises(ValueError, match="Unknown scene view"):
        custom.compose_scene_image("Scene", "unknown")


def test_cinematic_default_preset_targets_reference_face_structure_and_grade():
    style = StyleConfig(preset="cinematic_anime_cg").resolved()
    prefix = style["prefix"].lower()
    suffix = style["suffix"].lower()
    assert "椭圆面部轮廓" in prefix and "上眼睑褶皱" in prefix
    assert "针织纹理" in prefix
    assert "电影静帧" in suffix and "光晕" in suffix
    for word in ("sunset", "school", "young", "golden hour"):
        assert word not in prefix
    assert (
        StyleConfig(preset="cinematic_anime_cg")
        .compose_image("A room.")
        .startswith("高品质、源自动漫审美的三维游戏电影角色渲染。")
    )


def test_video_style_uses_english_while_images_use_chinese():
    style = StyleConfig()
    frame = style.compose_image("一间房间")
    result = style.compose_video(
        "detailed_description:\n[Shot 1] A key turns.\n\noverall_soundscape:\nA click.",
        "ref2va",
    )
    assert "三维游戏电影角色渲染" in frame
    assert "High-end anime-derived 3D game cinematic character render." in result
    assert "三维游戏电影角色渲染" not in result
    assert result.endswith("overall_soundscape:\nA click.")


def test_custom_style_can_set_video_text_without_changing_image_text():
    style = StyleConfig(
        preset="custom",
        presets={
            "custom": StylePreset(
                prefix="图片前缀", suffix="图片后缀", video_prefix="VIDEO", video_suffix="END"
            )
        },
    )
    assert style.compose_image("主体") == "图片前缀\n\n主体\n\n图片后缀"
    result = style.compose_video("detailed_description:\nA shot.", "ref2va")
    assert "VIDEO\n\nA shot.\n\nEND" in result
    assert "图片前缀" not in result and "图片后缀" not in result
    fallback = selected_style()
    assert fallback.resolved_video()["prefix"] == fallback.resolved()["prefix"]


def test_reference_safe_style_defers_to_each_reference_role():
    style = StyleConfig().resolved_identity()
    assert "按指定职责" in style["suffix"]
    assert "服饰和其他细节遵循具体任务要求" in style["suffix"]
    assert "参考图决定身份" not in style["suffix"]
