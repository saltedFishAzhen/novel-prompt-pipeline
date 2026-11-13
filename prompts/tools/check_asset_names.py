#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材名称保真检查：确认提示词里的角色/场景/道具名称没有被翻译、缩写或漏写。

结构校验器只查格式，看不到「林川被写成 Linchuan」这类改动，所以名称保真必须单独查。

用法：
    python check_asset_names.py <输出文件> <素材清单JSON>

素材清单 JSON：
    {"角色信息": ["林川", "阿宁"], "场景信息": ["木门半开的出租屋"], "物品信息": ["黄铜三齿的铜钥匙"]}
名称按模板规则写成「说明+名称」的形容词式；也兼容旧的「名称（说明）」写法。

判定：每个名称至少有一个候选主干（完整名、最后一个「的」之后的名词、括号前的原名）在输出中出现，
否则报未找到——翻译、缩写、漏写都会落到这一条。
"""
import json
import re
import sys


def stems(name: str) -> list[str]:
    """从一个「说明+名称」取出候选主干，长的在前。"""
    n = name.strip()
    if not n:
        return []
    out = {n}
    if "的" in n:
        out.add(n.rsplit("的", 1)[-1])
    m = re.match(r"^(.*?)[（(](.*)[）)]$", n)
    if m:
        out.add(m.group(1).strip())
    return sorted((s for s in out if len(s) >= 2), key=len, reverse=True)


def check(output_text: str, assets: dict) -> list[str]:
    problems = []
    for cat, names in assets.items():
        if not isinstance(names, list):
            continue
        for nm in names:
            if not nm or not nm.strip():
                continue
            if not any(c in output_text for c in stems(nm)):
                problems.append(
                    f"[{cat}] 未在输出中找到素材名「{nm}」——可能被翻译、缩写或漏写")
    return problems


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python check_asset_names.py <输出文件> <素材清单JSON>")
        return 2
    text = open(sys.argv[1], encoding="utf-8").read()
    assets = json.loads(open(sys.argv[2], encoding="utf-8").read())
    problems = check(text, assets)
    if problems:
        print("❌ 素材名称保真检查失败:")
        for p in problems:
            print("  -", p)
        return 1
    total = sum(len(v) for v in assets.values() if isinstance(v, list))
    print(f"✅ 素材名称保真检查通过（{total} 个名称均按原名出现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
