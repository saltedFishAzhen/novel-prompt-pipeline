"""Visual style presets applied only when preparing media workflows."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StylePreset(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)

    prefix: str = ""
    suffix: str = ""
    # Deprecated: accepted when loading older configurations, but never applied.
    negative: str = ""
    # Scene-only appearance avoids applying portrait anatomy and portrait framing.
    # None preserves custom presets by falling back to their general text.
    scene_prefix: str | None = None
    scene_suffix: str | None = None
    # Reference-based generation needs a treatment that does not define anatomy.
    identity_prefix: str | None = None
    identity_suffix: str | None = None
    material_prefix: str | None = None
    material_suffix: str | None = None
    # Custom presets may provide English video-specific wording. Existing
    # presets without these fields retain their general prefix/suffix fallback.
    video_prefix: str | None = None
    video_suffix: str | None = None


# Presets describe rendering style only. Narrative facts stay in the node output.
BUILTIN_STYLES = {
    "cinematic_anime_cg": StylePreset(
        prefix=(
            "高品质、源自动漫审美的三维游戏电影角色渲染。面部采用经过精细塑造的动漫风 CG 结构，"
            "呈现经过设计的游戏角色质感，而非人偶感：椭圆面部轮廓，颧骨柔和收窄，"
            "下颌渐收但不尖削，脸颊保持自然体积而不是圆润的婴儿脸。眼睛大而富有表现力，"
            "但不超出既定脸宽：可见上眼睑褶皱，睫毛线清晰且外眼角睫毛成簇，"
            "下眼睑边缘柔和；虹膜深邃、有放射状纤维细节，瞳孔清晰，"
            "有一个明亮眼神光和较小的次级高光。鼻梁细腻，鼻尖柔和圆润，鼻翼窄。"
            "嘴唇小至中等，唇峰清晰，上唇轮廓分明，下唇柔和饱满，呈低饱和自然玫瑰色，"
            "带轻微湿润光泽。皮肤具有次表面散射，额头、脸颊和鼻部有细微色调变化，"
            "呈哑光缎面质感而非均匀的瓷白。头发呈浓密、有光泽的发束，"
            "可辨认单根细长飞发，边缘有半透明光晕。织物具有可信的重量感，"
            "清晰呈现针织纹理、绒毛纤维边缘、利落的编织格纹和自然挺实的褶皱。"
            "整体呈精致、高对比度的电影渲染质感。"
        ),
        suffix=(
            "保留所描述的身份、年龄、肤色、服装、场景、构图和光源。保持表面清晰、"
            "材质细节锐利。当描述中的光线温暖或具有明确方向时，增强场景本身暗示的对比："
            "深而可辨的阴影衬托明亮轮廓光和发丝亮边，以柔和环境填充光保持面部清晰、"
            "双眼明亮。用大而柔和的圆形散景与轻微泛光分离主体和背景。保持每张脸"
            "内部一致，四分之三侧面透视准确，远侧眼睛部分被鼻梁遮挡。调色呈温暖"
            "电影静帧质感：金色高光、干净深邃且无灰雾的阴影、柔和 S 曲线和丰富的"
            "中间调对比、柔和的高光过渡、明亮轮廓光处轻微光晕，以及极轻的暗角。"
            "皮肤保持温暖奶油色调与柔和红润的脸颊，而非平白肤色。"
        ),
        video_prefix=(
            "High-end anime-derived 3D game cinematic character render. Faces use "
            "carefully sculpted anime-derived CG anatomy that reads as a designed game "
            "character rather than a doll: an oval facial plane with gently tapered "
            "cheekbones, a jaw that narrows without turning pointed, and natural cheek "
            "volume instead of rounded baby cheeks. Eyes are large and clearly "
            "expressive while staying within the stated face width: a visible upper lid "
            "fold, a defined lash line with clustered lashes at the outer corner, a soft "
            "lower-lid edge, a deep-set iris with radial fiber detail, a distinct pupil, "
            "and one bright catchlight plus a smaller secondary highlight. The nose has a "
            "subtle bridge, a soft rounded tip and narrow nostril wings. Lips are small "
            "to medium with a clear cupid's bow, a defined upper lip and a softly full "
            "lower lip, in a muted natural rose tint with a faint moist sheen. Skin shows "
            "subsurface scattering and faint tonal variation across the forehead, cheeks "
            "and nose with a matte-satin finish rather than uniform porcelain whiteness. "
            "Hair renders as thick glossy locks with individually resolved long flyaway "
            "strands and translucent glowing edges. Fabric keeps convincing weight and "
            "shows clearly visible knit stitches, fuzzy fiber edges, crisp woven plaid "
            "and firm natural folds. The finish is a polished high-contrast cinematic "
            "render."
        ),
        video_suffix=(
            "Preserve the stated identity, age, complexion, clothing, setting, framing "
            "and light sources. Keep surfaces sharp and material detail crisp. When the "
            "described light is warm or directional, push the contrast the scene implies: "
            "glowing rim highlights and luminous hair edges against deep readable "
            "shadows, with soft ambient fill that keeps faces clearly readable and eyes "
            "bright. Let large soft circular bokeh and gentle bloom separate the subject "
            "from the background. Keep each face internally consistent and correctly "
            "foreshortened in three-quarter views, with the far eye partly occluded by "
            "the nose bridge. Grade the frame like a warm filmic still: golden highlights, "
            "clean deep shadows without gray haze, a gentle S-curve with rich midtone "
            "contrast, soft highlight rolloff, light halation on bright rim light, and "
            "only a faint vignette. Skin keeps warm cream tones with a soft rosy cheek "
            "flush instead of flat white."
        ),
        scene_prefix=("源自动漫审美的立体 CG 场景，材质符合真实物理质感，表面细节清晰，比例自然。"),
        scene_suffix=(
            "保持既定光源和时间。阴影浓郁且可辨，泛光克制，高光过渡柔和。"
            "场景存在暖光时呈现金色高光和细微光晕。保持所需视角下环境与纵深清晰可辨。"
        ),
        identity_prefix=("源自动漫审美的立体 CG 渲染，材质表现可信，织物纹理清晰，发丝逐根可辨。"),
        identity_suffix=(
            "渲染处理保留各参考图按指定职责提供的属性，服饰和其他细节遵循具体任务要求。"
            "光照遵循所描述场景，阴影可辨，泛光克制，高光过渡柔和。"
        ),
        material_prefix=(
            "源自动漫审美的立体 CG 渲染，材质符合真实物理质感，表面纹理清晰，比例自然。"
        ),
        material_suffix=(
            "光照遵循场景描述及既定光源。阴影清晰可辨，泛光克制，高光过渡柔和。"
            "存在暖光时呈现金色高光与细微光晕。"
        ),
    ),
}


class StyleConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)

    preset: str = "cinematic_anime_cg"
    presets: dict[str, StylePreset] = Field(default_factory=dict)
    # Deprecated: retained for loading older configurations and run snapshots.
    image_negative: str | None = None

    @model_validator(mode="after")
    def validate_preset(self) -> StyleConfig:
        if not self.preset.strip() or self.preset not in (BUILTIN_STYLES | self.presets):
            raise ValueError("Unknown visual style preset")
        if any(not name.strip() for name in self.presets):
            raise ValueError("Visual style preset names must not be empty")
        return self

    def resolved(self) -> dict[str, str]:
        selected = (BUILTIN_STYLES | self.presets)[self.preset]
        return {
            "preset": self.preset,
            "prefix": selected.prefix.strip(),
            "suffix": selected.suffix.strip(),
            "image_negative": "",
        }

    def compose_image(self, prompt: str) -> str:
        resolved = self.resolved()
        if not resolved["prefix"] and not resolved["suffix"]:
            return prompt
        return _join(resolved["prefix"], prompt, resolved["suffix"])

    def resolved_video(self) -> dict[str, str]:
        """Resolve H3 style; custom presets can supply English video text explicitly."""
        selected = (BUILTIN_STYLES | self.presets)[self.preset]
        general = self.resolved()
        return {
            **general,
            "prefix": general["prefix"]
            if selected.video_prefix is None
            else selected.video_prefix.strip(),
            "suffix": general["suffix"]
            if selected.video_suffix is None
            else selected.video_suffix.strip(),
        }

    def resolved_scene(self) -> dict[str, str]:
        selected = (BUILTIN_STYLES | self.presets)[self.preset]
        general = self.resolved()
        return {
            **general,
            "prefix": general["prefix"]
            if selected.scene_prefix is None
            else selected.scene_prefix.strip(),
            "suffix": general["suffix"]
            if selected.scene_suffix is None
            else selected.scene_suffix.strip(),
        }

    def resolved_identity(self) -> dict[str, str]:
        """Use explicit reference-safe style text, never infer it from portrait prose."""
        selected = (BUILTIN_STYLES | self.presets)[self.preset]
        return {
            **self.resolved(),
            "prefix": (selected.identity_prefix or "").strip(),
            "suffix": (selected.identity_suffix or "").strip(),
        }

    def resolved_material(self) -> dict[str, str]:
        """Style non-portrait content without referring to images that may not exist."""
        selected = (BUILTIN_STYLES | self.presets)[self.preset]
        return {
            **self.resolved(),
            "prefix": (selected.material_prefix or "").strip(),
            "suffix": (selected.material_suffix or "").strip(),
        }

    def compose_scene_image(self, prompt: str, view: str) -> str:
        """Keep camera edits ahead of appearance and preserve reference identity only."""
        if view not in {"master", "reverse", "overview", "topdown"}:
            raise ValueError(f"Unknown scene view: {view}")
        style = self.resolved_scene()
        if view == "master":
            return _join(style["prefix"], prompt, style["suffix"])
        appearance = _join(style["prefix"], style["suffix"])
        return _join(
            prompt,
            "仅用于外观处理：\n" + appearance if appearance else "",
            "机位、投影、构图和可见表面遵循以上编辑要求。参考图提供场景身份与世界空间布局，"
            "不沿用其原机位或前景遮挡。外观处理不得覆盖所需视角；渲染同一场景的一个新视角。",
        )

    def compose_video(self, prompt: str, mode: str) -> str:
        """Style the visual section without moving H3's required first-frame line."""
        resolved = self.resolved_video()
        if not resolved["prefix"] and not resolved["suffix"]:
            return prompt
        section = "integrated_multimodal_description" if mode == "i2va" else "detailed_description"
        start = re.search(rf"(?m)^{section}:\s*", prompt)
        if not start:
            raise ValueError(f"Video prompt lacks the {section} section required to apply style")
        following = re.search(r"(?m)^[a-z_]+:\s*", prompt[start.end() :])
        end = start.end() + following.start() if following else len(prompt)
        body = prompt[start.end() : end].strip()
        styled = _join(resolved["prefix"], body, resolved["suffix"])
        return prompt[: start.end()] + styled + "\n\n" + prompt[end:]


def _join(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip())
