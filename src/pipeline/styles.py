"""Visual style presets applied only when preparing media workflows."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StylePreset(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)

    prefix: str = ""
    suffix: str = ""
    negative: str = ""


# Presets describe rendering style only. Narrative facts stay in the node output.
BUILTIN_STYLES = {
    "cinematic_anime_cg": StylePreset(
        prefix=(
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
        suffix=(
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
        negative=(
            "literal live-action human face, documentary actor photograph, flat gray "
            "haze, milky lifted blacks, desaturated pastel wash, overexposed white skin, "
            "muddy washed-out shadows, soft unfocused fabric texture, plastic toy sheen, "
            "waxy featureless skin, porcelain doll skin, uniform airbrushed white skin, "
            "flat pasted-on iris, glossy plastic cheeks, both eyes fully visible in "
            "profile, aggressive HDR halos, teal-and-orange cliche grade, heavy vignette, "
            "heavy contour lines, 2D flat cel shading, chibi proportions, watermark, text"
        ),
    ),
}


class StyleConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_assignment=True)

    preset: str = "cinematic_anime_cg"
    presets: dict[str, StylePreset] = Field(default_factory=dict)
    # None inherits the preset; an explicit empty string disables it for images.
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
        negative = selected.negative.strip()
        return {
            "preset": self.preset,
            "prefix": selected.prefix.strip(),
            "suffix": selected.suffix.strip(),
            "image_negative": negative
            if self.image_negative is None
            else self.image_negative.strip(),
        }

    def compose_image(self, prompt: str) -> str:
        resolved = self.resolved()
        if not resolved["prefix"] and not resolved["suffix"]:
            return prompt
        return _join(resolved["prefix"], prompt, resolved["suffix"])

    def compose_video(self, prompt: str, mode: str) -> str:
        """Style the visual section without moving H3's required first-frame line."""
        resolved = self.resolved()
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
