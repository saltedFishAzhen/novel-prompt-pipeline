#!/usr/bin/env python3
"""把 src/common.json + src/modes/*.json 编译成 compiled/*.txt。

设计原则：
  1. 共用约定只写在 common.json —— 改一处，三份模板同时生效
  2. 每份模式只写自己的差异部分
  3. 词数上限、禁词表、输出格式、自查清单全部由编译器统一生成，
     不会出现「一个文件里两处写不同数字」的手工同步错误

用法：
    python tools/build.py            # 编译全部
    python tools/build.py scene      # 只编译场景模板
    python tools/build.py --check    # 只比对，不写文件（CI 用）
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from novel_prompt_pipeline.resources import prompt_root

from .release import PromptReleaseError

MODES = ["scene", "character", "prop"]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render(mode_cfg, common):
    """把一份模式配置渲染成完整提示词文本。"""
    out = []
    push = out.append

    entity = mode_cfg["entity"]
    limit = common["word_limit"]

    # ---------- 标题 ----------
    push(f"任务：{mode_cfg['title']}")
    push("")

    # ---------- 提取 ----------
    ex = mode_cfg["extraction"]
    push(ex["rule"])
    if ex.get("cap"):
        push(ex["cap"])
    push(ex["threshold"])
    push("")

    # ---------- description 通用要求 ----------
    push("【description 通用要求】")
    push(mode_cfg["description_base"])
    push("")

    # ---------- 角色专属前置区块 ----------
    for sec in mode_cfg.get("pre_sections", []):
        push(f"【{sec['title']}】")
        body = sec["body"]
        for para in body if isinstance(body, list) else [body]:
            push(para)
        push("")

    # ---------- 命名（由 common 生成，注入实体名）----------
    nm = common["naming"]
    push("【命名】")
    push(nm["rule"].format(entity, entity))
    push(nm["state_form"].format(entity))
    push(nm["unique"])
    if ex.get("naming_scope"):
        push(ex["naming_scope"])
    push("")

    # ---------- aliases（角色版已在前置区块里写过叙述者规则，此处仍统一）----------
    al = common["aliases"]
    push("【aliases 口径】")
    push(al["rule"].format(entity, entity, entity, entity, entity))
    push(al["format"])
    push(al["reuse_exemption"])
    push("")

    # ---------- 主体区块 ----------
    for sec in mode_cfg["sections"]:
        push(f"【{sec['title']}】")
        body = sec["body"]
        for para in body if isinstance(body, list) else [body]:
            push(para)
        push("")

    # ---------- 状态段规则（common 统一）----------
    state_rules = dict(common["state_rules"])
    overrides = mode_cfg.get("state_rules_override", {})
    if not isinstance(overrides, dict) or any(
        key not in state_rules or not isinstance(value, str) for key, value in overrides.items()
    ):
        raise ValueError("state_rules_override must contain known rule names with string values")
    state_rules.update(overrides)
    push("【状态段适用范围】")
    push(state_rules["variant_only"])
    push(state_rules["light_position"])
    push("")

    # ---------- 词数（由 common 的 limit 注入）----------
    wb = mode_cfg["word_budget"]
    push("【词数】")
    word_lines = [f"每条 description 控制在 {wb['target']} 之间，任何情况下不超过 {limit} 词。"]
    if wb.get("variant_target"):
        word_lines[0] = (
            f"基准记录控制在 {wb['target']} 之间，变体记录控制在 "
            f"{wb['variant_target']} 之间，任何情况下不超过 {limit} 词。"
        )
    if wb.get("floor_note"):
        word_lines.append(wb["floor_note"])
    if wb.get("variant_note"):
        word_lines.append(wb["variant_note"])
    word_lines.append(wb["compress"])
    if mode_cfg.get("state_min_words"):
        word_lines.append(f"状态段不少于 {mode_cfg['state_min_words']} 词。")
    push("".join(word_lines))
    push("")

    # ---------- 输入材料 ----------
    im = common["input_material"]
    push(im["header"])
    push(im["novel"])
    push("{{小说原文}}")
    push(im["copy"])
    push("{{推文文案}}")
    push(im["plot"])
    push("{{故事情节}}")
    push(im["rule"])
    push("")

    # ---------- 输出格式 ----------
    of = common["output_format"]
    push("输出格式")
    push(
        "".join(
            [
                of["top_level"],
                of["no_wrapping"],
                of["fields"],
            ]
        )
    )
    push("".join([of["english"], of["escaping"], of["empty"]]))
    push("")

    # ---------- 示例 ----------
    exs = mode_cfg["example"]
    example = json.dumps(
        [
            {
                "name": exs["name"],
                "aliases": exs["aliases"],
                "description": "English visual description",
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    push(f"示例结构（禁止照抄内容）：{example}")
    push("")

    # ---------- 自查（common + 模式专属）----------
    items = []
    for c in common["self_check_common"]:
        items.append(c.format(word_limit=limit))
    items.extend(mode_cfg.get("self_check_extra", []))
    push("输出前逐项自查：" + "；".join(items) + "。")

    return "\n".join(out) + "\n"


def safe_path(root: Path, *parts: str) -> Path:
    """Keep workspace data and generated outputs within the selected project."""
    root = root.resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise PromptReleaseError(f"Prompt path escapes workspace: {path}")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise PromptReleaseError(f"Prompt path contains a link: {current}")
    return path


def atomic_write_text(path: Path, content: str) -> None:
    """Replace, rather than follow, an existing output file or hard link."""
    fd, temporary = tempfile.mkstemp(prefix=".prompt-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def compile_assets(
    root: str | Path | None = None, *, check: bool = False, targets: list[str] | None = None
) -> list[str]:
    """Compile trusted asset templates from the selected workspace's JSON data."""
    root = Path(root).resolve() if root is not None else prompt_root().resolve()
    selected = targets if targets is not None else MODES
    common = load(safe_path(root, "src", "common.json"))
    errors = []
    output_dir = safe_path(root, "compiled")
    if not check:
        output_dir.mkdir(parents=True, exist_ok=True)
    for mode in selected:
        if mode not in MODES:
            errors.append(f"未知模式：{mode}")
            continue
        cfg = load(safe_path(root, "src", "modes", f"{mode}.json"))
        filename = f"asset.{mode}.txt"
        if cfg.get("output_filename") != filename:
            errors.append(f"{mode}: output_filename must be {filename}")
            continue
        text = render(cfg, common)
        if text.startswith("\ufeff") or "\r" in text:
            errors.append(f"{mode}: compiled text must use UTF-8 without BOM and LF newlines")
            continue
        output = safe_path(root, "compiled", filename)
        if check:
            if not output.is_file() or output.read_bytes() != text.encode("utf-8"):
                errors.append(f"{mode}: compiled prompt is not synchronized with src/")
        else:
            atomic_write_text(output, text)
            legacy = safe_path(root, "compiled", f"{mode}.txt")
            if legacy.exists():
                if not legacy.is_file():
                    raise PromptReleaseError(f"Legacy compiled output is not a file: {legacy}")
                legacy.unlink()
    return errors


def main(argv: list[str] | None = None, *, root: str | Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the three visual asset prompts")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("modes", nargs="*")
    args = parser.parse_args(argv)
    errors = compile_assets(root, check=args.check, targets=args.modes or None)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Asset prompts checked." if args.check else "Asset prompts built.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
