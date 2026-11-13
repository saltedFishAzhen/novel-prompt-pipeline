#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词项目 · 三类资产训练校验入口

用法：
    python tools/cli.py check                          # 三份提示词跨模板一致性自检
    python tools/cli.py validate <mode> <file> [src]   # 校验一份模型输出（结构层+内容层）
    python tools/cli.py inject <mode> <source> [out]   # 把原文注入提示词模板，生成可直接投喂的请求
    python tools/cli.py selftest                       # 用内置夹具跑全部校验器（含正例/反例）

mode: scene | character | prop

示例：
    python tools/cli.py inject scene tests/fixtures/scene_src.txt
    python tools/cli.py validate scene tests/fixtures/scene_ok.json tests/fixtures/scene_src.txt
    python tools/cli.py check
"""
import argparse
import os
import sys
from pathlib import Path

from prompt_project.asset_compiler import compile_assets
from prompt_project.validation import check_templates, selftest
from prompt_project.validators import (
    validate_asset_content,
    validate_asset_sheet_output,
    validate_character_sheet_output,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
PROMPTS = os.path.join(ROOT, "compiled")
TESTS = os.path.join(ROOT, "tests")

MODES = {
    "scene": "asset.scene.txt",
    "character": "asset.character.txt",
    "prop": "asset.prop.txt",
}

# mode -> 结构校验器
STRUCT_VALIDATOR = {
    "scene": "validate_asset_sheet_output.py",
    "character": "validate_character_sheet_output.py",
    "prop": "validate_asset_sheet_output.py",
}


def _py(script, *args):
    """调用安装包内可信代码，返回 (returncode, output)。"""
    if script == "build.py":
        targets = [x for x in args if x != "--check"]
        errors = compile_assets(ROOT, check="--check" in args, targets=targets or None)
        return (1 if errors else 0), ("\n".join(errors) if errors else "资产提示词已同步")
    if script == "validate_character_sheet_output.py":
        output, *rest = args
        source = Path(rest[0]).read_text(encoding="utf-8") if rest else None
        errors = validate_character_sheet_output.validate(Path(output).read_text(encoding="utf-8"), source)
        return (1 if errors else 0), ("\n".join(errors) if errors else "角色结构校验通过")
    if script == "validate_asset_sheet_output.py":
        mode, output, *rest = args
        source = Path(rest[0]).read_text(encoding="utf-8") if rest else None
        errors = validate_asset_sheet_output.validate(Path(output).read_text(encoding="utf-8"), source, mode)
        return (1 if errors else 0), ("\n".join(errors) if errors else "资产结构校验通过")
    if script == "validate_asset_content.py":
        mode, output, *rest = args
        source = Path(rest[0]).read_text(encoding="utf-8") if rest else None
        errors, warnings = validate_asset_content.validate(Path(output).read_text(encoding="utf-8"), source, mode)
        return (1 if errors else 0), "\n".join(warnings + (errors or ["资产内容校验通过"]))
    return 2, "（不支持的工具）%s" % script


def cmd_validate(args):
    mode = args.mode
    if mode not in MODES:
        print(f"mode 必须是 {list(MODES)} 之一")
        return 2
    out_file = os.path.abspath(args.file)
    if not os.path.exists(out_file):
        print(f"找不到输出文件：{out_file}")
        return 2
    src = os.path.abspath(args.source) if args.source else None

    struct = STRUCT_VALIDATOR[mode]
    s_args = ([mode, out_file] if struct == "validate_asset_sheet_output.py" else [out_file])
    if src:
        s_args.append(src)

    rc1, o1 = _py(struct, *s_args)
    print("【结构层】")
    print("  " + o1.replace("\n", "\n  "))

    c_args = [mode, out_file] + ([src] if src else [])
    rc2, o2 = _py("validate_asset_content.py", *c_args)
    print("【内容层】")
    print("  " + o2.replace("\n", "\n  "))

    ok = rc1 == 0 and rc2 == 0
    print()
    print("✅ 全部通过" if ok else "❌ 校验未通过")
    return 0 if ok else 1


def cmd_inject(args):
    mode = args.mode
    if mode not in MODES:
        print(f"mode 必须是 {list(MODES)} 之一")
        return 2
    tmpl_path = os.path.join(PROMPTS, MODES[mode])
    src_path = os.path.abspath(args.source)
    if not os.path.exists(src_path):
        print(f"找不到原文文件：{src_path}")
        return 2

    tmpl = open(tmpl_path, encoding="utf-8").read()
    src = open(src_path, encoding="utf-8").read()
    text = (tmpl
            .replace("{{小说原文}}", src)
            .replace("{{推文文案}}", "（本次无推文文案）")
            .replace("{{故事情节}}", "（本次无额外情节摘要）"))
    if "{{" in text:
        print("⚠ 模板中仍有未替换的占位符")

    if args.out:
        out_path = os.path.abspath(args.out)
    else:
        stem = os.path.splitext(os.path.basename(src_path))[0]
        out_path = os.path.join(TESTS, "build", f"injected_{mode}_{stem}.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已生成：{out_path}")
    print(f"  ({len(text)} 字符，原文 {len(src)} 字符)")
    return 0


def cmd_build(args):
    """从 src/ 编译提示词到 prompts/。"""
    rc, out = _py("build.py", *args.rest)
    print(out.replace("\n", "\n  ").strip())
    return rc


def cmd_check(args):
    checks = check_templates(ROOT)
    for label, ok in checks:
        print(("  OK  " if ok else "  FAIL") + "  " + label)
    failed = [label for label, ok in checks if not ok]
    print(f"Checks: {len(checks)}, failures: {len(failed)}")
    return 1 if failed else 0


def cmd_selftest(args):
    failures = selftest(ROOT)
    if failures:
        print("\n".join(failures))
        return 1
    print("All three asset fixtures passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description="提示词项目三类资产校验 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="三份提示词口径自检 + 编译一致性")
    p_check.set_defaults(func=cmd_check)

    p_build = sub.add_parser("build", help="从 src/ 编译资产提示词到 compiled/")
    p_build.add_argument("rest", nargs="*", help="可选：scene / character / prop；留空编译全部")
    p_build.set_defaults(func=cmd_build)

    p_val = sub.add_parser("validate", help="校验一份模型输出")
    p_val.add_argument("mode", choices=list(MODES))
    p_val.add_argument("file")
    p_val.add_argument("source", nargs="?")
    p_val.set_defaults(func=cmd_validate)

    p_inj = sub.add_parser("inject", help="把原文注入模板")
    p_inj.add_argument("mode", choices=list(MODES))
    p_inj.add_argument("source")
    p_inj.add_argument("out", nargs="?")
    p_inj.set_defaults(func=cmd_inject)

    p_st = sub.add_parser("selftest", help="跑夹具自检")
    p_st.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
