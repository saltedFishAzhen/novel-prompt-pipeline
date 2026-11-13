#!/usr/bin/env python3
# ruff: noqa: UP031
# -*- coding: utf-8 -*-
"""校验场景/物品设定板提示词输出的 JSON 合规性（两种模式共用）。

用法：python validate_asset_sheet_output.py <mode> <output_file> [source_file]
  mode: scene 或 prop
  output_file: 模型输出的 JSON 数组（含代码块围栏会被报告）
  source_file: 含 {{小说原文}} 注入内容的原始请求（可选；提供时校验 name 是否来自原文）
退出码：0 通过，1 失败。"""

import json
import re
import sys

BOARD_HINTS_COMMON = [
    "16:9",
    "white background",
    "sheet",
    "layout",
    "main view",
    "close-up",
    "detail",
    "inset",
]
SCENE_HINTS = [
    "scene",
    "room",
    "layout",
    "top view",
    "perspective",
    "door",
    "window",
    "wall",
    "light",
    "foreground",
    "background",
]
PROP_HINTS = ["three-quarter", "object", "front", "side", "back", "turnaround", "centered"]
NOISE = ["qwen", "parameter", "4k", "8k", "masterpiece", "best quality", "midjourney"]
# 否定式人物描写（合法）："no people" / "without hands" 等，先从文本中剔除再查人物元素
NEG_HUMAN = re.compile(
    r"\b(?:no|without|zero|free\s+of|absolutely\s+no)\s+"
    r"(?:any\s+)?(?:(?:humanoid|human)\s+)?(?:people|persons?|humans?|hands?|fingers?|figures?|"
    r"characters?|silhouettes?|man|woman|men|women)\b"
    r"(?:\s*(?:,\s*|\s+(?:or|and)\s+)(?:(?:humanoid|human)\s+)?"
    r"(?:people|persons?|humans?|hands?|fingers?|figures?|characters?|silhouettes?|man|woman|men|women)\b)*",
    re.I,
)
HUMAN_WORDS = re.compile(
    r"\b(?:a\s+man|a\s+woman|man|woman|men|women|person|people|"
    r"human|hands?|fingers?|palms?|thumbs?|figures?|character|girl|boy|silhouettes?)\b",
    re.I,
)


def find_humans(desc: str) -> list:
    """返回人物元素命中（已剔除否定式）；仅整词匹配，不误伤 handle/handrail 等。"""
    cleaned = NEG_HUMAN.sub(" ", desc)
    return [m.group(0).lower() for m in HUMAN_WORDS.finditer(cleaned)]


def name_in_source(name: str, aliases: str, source: str) -> bool:
    """name 合法性：name 本身、任一 alias、或 name 的任一 2+ 字连续子串见于原文。
    允许"码头小屋"这类合并命名；"屠龙宝刀"这类无任何子串命中才算编造。"""
    if name in source:
        return True
    for al in [a.strip() for a in (aliases or "").split(",") if a.strip()]:
        if al in source:
            return True
    for i in range(len(name) - 1):
        for j in range(i + 2, len(name) + 1):
            if name[i:j] in source:
                return True
    return False


def validate(output_text: str, source: str, mode: str) -> list:
    errs = []
    text = output_text.strip()
    if text.startswith("```"):
        errs.append("输出被代码块围栏包裹（协议禁止）")
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    if text.startswith("{"):
        errs.append("顶层是 JSON 对象（应为数组，疑似其他模板的 {results} 串味）")
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "results" in obj:
                errs.append("检测到 results 包装（本任务输出应为裸数组）")
        except Exception:
            pass
        return errs
    try:
        arr = json.loads(text)
    except json.JSONDecodeError as e:
        errs.append(
            "JSON 解析失败（行%d 列%d）: %s；常见成因：裸换行/尾随逗号" % (e.lineno, e.colno, e.msg)
        )
        return errs
    if not isinstance(arr, list):
        errs.append("顶层不是 JSON 数组，实际为 %s" % type(arr).__name__)
        return errs
    if not arr:
        return errs
    if mode not in ("scene", "prop"):
        return ["mode 必须为 scene 或 prop，实际: %r" % mode]
    for i, item in enumerate(arr, 1):
        if not isinstance(item, dict):
            errs.append("第%d项不是 JSON 对象" % i)
            continue
        if set(item.keys()) != {"name", "aliases", "description"}:
            errs.append(
                "第%d项字段应为且仅为 name+aliases+description，实际: %s" % (i, sorted(item.keys()))
            )
            continue
        nm, al, d = item["name"], item["aliases"], item["description"]
        for fname, val in (("name", nm), ("aliases", al), ("description", d)):
            if not isinstance(val, str):
                errs.append("第%d项 %s 不是字符串" % (i, fname))
        if isinstance(nm, str):
            if not nm.strip():
                errs.append("第%d项 name 为空" % i)
            elif "我" in nm:
                errs.append("第%d项 name 含“我”（场景/物品名不应出现第一人称）" % i)
        if not isinstance(d, str) or not d.strip():
            errs.append("第%d项 description 为空" % i)
            continue
        # 模板允许一次 "Reuses base location: <原文地点名>" 子句，判中文量前先剥掉
        d_for_cjk = re.sub(r"Reuses base location:\s*[^.]*\.?", "", d)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", d_for_cjk))
        if cjk > 5:
            errs.append("第%d项 description 含大段中文（应为英文）" % i)
        low = " " + d.lower() + " "
        hints = BOARD_HINTS_COMMON + (SCENE_HINTS if mode == "scene" else PROP_HINTS)
        hits = [h for h in hints if h in low]
        if len(hits) < 2:
            errs.append(
                "第%d项 description 缺少设定板布局描述（16:9/主图/细节栏线索不足，仅命中 %s）"
                % (i, hits)
            )
        noise = [w for w in NOISE if w in low]
        if noise:
            errs.append("第%d项 description 含禁止词: %s" % (i, noise))
        if mode in ("scene", "prop") and re.search(r"\bcharacters?\b", d, re.I):
            errs.append("第%d项 description 含 character 一词（三份资产提示词统一整词禁用）" % i)
        words = len(d.split())
        if words > 450:
            errs.append("第%d项 description 过长（%d 词，硬上限 450）" % (i, words))
        humans = find_humans(d)
        if humans:
            errs.append(
                "第%d项 description 含人物元素 %s（场景/物品板应绝对无人）"
                % (i, sorted(set(humans)))
            )
        # hand 类词会被误判为人体部位，但只针对真正危险的复合词。
        # handle（门把手、壶把、刀柄）是器物唯一准确的用词，find_humans 的 hands? 整词匹配
        # 本来就不会误伤它，靠子串正则一刀切会把「壶把」也禁掉，得不偿失。
        d_nohand = NEG_HUMAN.sub(" ", d)
        HAND_SAFE = {"handle", "handles", "handled"}
        bad_hand = sorted(
            set(m.lower() for m in re.findall(r"\b[\w-]*hand[\w-]*\b", d_nohand, re.I)) - HAND_SAFE
        )
        if bad_hand:
            errs.append(
                "第%d项 description 含 hand 类词 %s（hand pump 写 lever pump，handrail 写 stair rail，right-hand 写 on the right）"
                % (i, bad_hand)
            )
        desc_for_name = re.sub(r"Reuses base location:\s*[^.]*\.?", "", d)
        if (
            isinstance(nm, str)
            and nm.strip()
            and len(nm.strip()) > 1
            and nm.strip() in desc_for_name
        ):
            errs.append(
                "第%d项 description 中出现了 name（%r）本身，名称不是画面文字" % (i, nm[:20])
            )
        if source is not None and isinstance(nm, str) and nm.strip():
            if not name_in_source(nm, al if isinstance(al, str) else "", source):
                errs.append("name %r 未见原文出现且无子串/别名命中（疑似编造）" % nm[:20])
    seen = {}
    for item in arr:
        if isinstance(item, dict) and "name" in item and "description" in item:
            key = (item["name"], item["description"])
            if key in seen:
                errs.append("存在完全重复的记录（name+description 相同）")
                break
            seen[key] = True
    return errs


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mode, out_path = sys.argv[1], sys.argv[2]
    out = open(out_path, encoding="utf-8").read()
    src = None
    if len(sys.argv) > 3:
        try:
            src = open(sys.argv[3], encoding="utf-8").read()
        except OSError as e:
            print("（警告）无法读取输入源文件，跳过 name 来源检查: %s" % e)
    errs = validate(out, src, mode)
    if errs:
        print("❌ 校验失败:")
        for p in errs:
            print("  -", p)
        return 1
    clean = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out.strip()).strip()
    arr = json.loads(clean)
    print("✅ %s 设定板校验通过（%d 个对象）" % (mode, len(arr)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
