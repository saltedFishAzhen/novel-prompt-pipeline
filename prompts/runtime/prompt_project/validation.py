"""Trusted asset validators; workspace files are only read as input data."""

from __future__ import annotations

import re
from pathlib import Path

from .asset_compiler import compile_assets, safe_path
from .validators import (
    validate_asset_content,
    validate_asset_sheet_output,
    validate_character_sheet_output,
)


def asset_validation_report(
    text: str, kind: str, source: str | None = None
) -> tuple[list[str], list[str]]:
    """Return structural/content errors and non-fatal warnings for one asset array."""
    if kind not in {"character", "scene", "prop"}:
        raise ValueError("kind must be character, scene, or prop")
    if kind == "character":
        errors = validate_character_sheet_output.validate(text, source)
    else:
        errors = validate_asset_sheet_output.validate(text, source, kind)
    if errors:
        return errors, []
    content_errors, warnings = validate_asset_content.validate(text, source, kind)
    return errors + content_errors, warnings


def validate_assets(text: str, kind: str, source: str | None = None) -> list[str]:
    """Return all validation errors; warnings remain available via the report API."""
    return asset_validation_report(text, kind, source)[0]


def selftest(root: str | Path) -> list[str]:
    """Run the three established positive fixtures without executing workspace code."""
    root = Path(root).resolve()
    safe_path(root, "tests", "fixtures")
    failures = []
    for kind in ("scene", "character", "prop"):
        output = safe_path(root, "tests", "fixtures", f"{kind}_ok.json")
        source = safe_path(root, "tests", "fixtures", f"{kind}_src.txt")
        if not output.is_file() or not source.is_file():
            failures.append(f"Missing required {kind} fixture")
            continue
        errors = validate_assets(
            output.read_text(encoding="utf-8"), kind, source.read_text(encoding="utf-8")
        )
        failures.extend(f"{kind}: {error}" for error in errors)
    return failures


def check_templates(root: str | Path) -> list[tuple[str, bool]]:
    """Run the existing three-template consistency checks as trusted Python."""
    root = Path(root).resolve()
    texts = {
        mode: safe_path(root, "compiled", f"asset.{mode}.txt").read_text(encoding="utf-8")
        for mode in ("scene", "character", "prop")
    }
    scene, character, prop = (texts[mode] for mode in ("scene", "character", "prop"))
    all_text = (scene, character, prop)

    def allhave(*needles: str) -> bool:
        return all(any(needle in text for needle in needles) for text in all_text)

    allowed = {"小说原文", "推文文案", "故事情节"}
    checks = [
        (
            "默认基准命名",
            all(
                f"默认的+{entity}" in text
                for text, entity in ((scene, "地点"), (character, "角色"), (prop, "物品"))
            ),
        ),
        (
            "基准和变体词数分档",
            all("基准记录控制在" in text and "变体记录控制在" in text for text in all_text),
        ),
        ("450 词硬上限", all("不超过 450" in text for text in all_text)),
        (
            "character 禁用规则",
            all(
                ("不写 person" in text and "character" in text)
                or any(
                    rule in text for rule in ("不使用 character", "不写 character", "无 character")
                )
                for text in all_text
            ),
        ),
        (
            "aliases 非空",
            all(
                "aliases 一律填" in text
                and any(x in text for x in ("不得留空", "不得为空", "不要写空字符串"))
                for text in all_text
            ),
        ),
        ("Reuses 子句", allhave("Reuses base")),
        ("Reuses 句号", allhave("以句号结束")),
        ("状态段位置", allhave("最后")),
        ("稀疏输入输出数组", allhave("输出 []")),
        (
            "仅允许已声明的输入占位符",
            all(set(re.findall(r"\{\{([\w.:-]+)\}\}", text)) <= allowed for text in all_text),
        ),
        ("无固定视角模板残留", all("[fixed viewpoint]" not in text for text in all_text)),
        ("骨架纯净规则", all("不进骨架" in text or "不随状态变化" in text for text in all_text)),
        ("场景物品把手规则", "handle" in scene and "handle" in prop),
        (
            "基准记录状态段范围",
            all("基准记录不像变体那样需要" in text for text in (scene, character))
            and "基准记录" in prop
            and "无需独立状态段" in prop,
        ),
        (
            "光线、Reuses、状态顺序",
            all(
                "一致性尾句 → 光线说明 → Reuses 子句 → 状态段" in text
                for text in (scene, character)
            )
            and "Reuses 子句之前" in prop
            and "之后只写当前图内有据状态" in prop,
        ),
        ("场景光线不收尾", "最后一句" in scene and "明暗" in scene and "收尾" in scene),
        ("角色尾句长度", "单句超过 55 词就拆开" in character),
        ("角色骨架两层", "有据才写项" in character),
        ("物品骨架句数", "1 至 3 句英文共享骨架" in prop),
        (
            "场景单幅全景",
            "单幅场景全景" in scene and "不做拼版" in scene and "右侧两格" not in scene,
        ),
        ("角色三个细节格", "3 格细节特写条" in character),
        (
            "物品单视角",
            "1:1" in prop and "单一视角" in prop and "不拼版" in prop and "共 4 格" not in prop,
        ),
        (
            "场景名称保真",
            all(x in scene for x in ("均保留原文写法", "不得换成原文未出现的房屋类型")),
        ),
        ("场景 16:9 格式", "16:9" in scene),
        ("物品无人否定句", "no people, no hands, no fingers" in prop),
        ("资产编译与源同步", not compile_assets(root, check=True)),
    ]
    return checks
