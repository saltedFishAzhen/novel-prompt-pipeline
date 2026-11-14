"""Shared image prompt composition and reviewed asset board cropping."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps


def _prompt(content: str, layout: str, style: dict, references: list[dict]) -> str:
    parts = []
    if references:
        parts.append("按下述内容和版式生成一张新图。")
        for index, reference in enumerate(references, 1):
            parts.append(f"参考图 {index} 的用途：{reference['role']}。")
        parts.append("参考图仅提供上述身份与外观依据，构图按本次要求重新安排。")
        if any(ref["role"].startswith("仅参考用户指定的") for ref in references):
            parts.append(
                "照片只提供该参考图指定范围内的外观特征，不复制照片中的其他五官、肤色、服装、"
                "姿势、背景或摄影风格；原文服装、设计候选和本项目画风继续生效。"
            )
        elif any(ref["role"].startswith("仅参考脸部身份与发型") for ref in references):
            parts.append(
                "保留参考图中的脸部身份与发型，服装和画风遵循当前任务，"
                "不复制照片中的服装、姿势、背景或摄影风格。"
            )
        elif any(
            ref["role"].startswith(("仅参考脸部身份", "角色的脸部身份")) for ref in references
        ):
            parts.append(
                "文字中的五官特征若与脸部参考冲突，保留参考图中的脸部身份；"
                "服装、发型和版式遵循文字要求。"
            )
    parts.extend(
        (content.strip(), layout.strip(), style["prefix"].strip(), style["suffix"].strip())
    )
    return " ".join(part for part in parts if part)


def _main_reference(
    pipeline, asset: dict, sheet: dict, role: str, region: dict | None = None
) -> dict:
    """Crop the expected L-board main panel without resizing or altering its pixels.

    The operator must inspect the board when completing the image task. Pixel
    bounds reflect the requested layout; code cannot prove the model followed it.
    """
    pipeline._verify_media(sheet)
    if asset["kind"] == "character":
        return sheet
    descriptor, filename = tempfile.mkstemp(prefix=".asset-main-", suffix=".png", dir=pipeline.root)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream, Image.open(sheet["local_path"]) as image:
            # Orientation metadata changes the visible top-left corner. Normalize
            # it in the derived crop while keeping the original board unchanged.
            oriented = ImageOps.exif_transpose(image)
            try:
                if region is not None:
                    if (oriented.width, oriented.height) != (region["width"], region["height"]):
                        raise ValueError("Reviewed main region does not match the board dimensions")
                    bounds = tuple(region["bounds"])
                else:
                    bounds = (
                        0,
                        0,
                        max(1, round(oriented.width * 0.72)),
                        max(1, round(oriented.height * 0.70)),
                    )
                oriented.crop(bounds).save(stream, format="PNG")
            finally:
                oriented.close()
        return pipeline.library.link_file(
            asset["asset_id"], temporary, role=role, asset_version=asset.get("asset_version")
        )
    finally:
        temporary.unlink(missing_ok=True)
