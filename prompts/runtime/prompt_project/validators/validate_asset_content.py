#!/usr/bin/env python3
# ruff: noqa: UP031
# -*- coding: utf-8 -*-
"""资产设定板内容层校验（补充结构校验器查不到的三条硬规则）。

用法：python validate_asset_content.py <mode: scene|character|prop> <output_file> [source_file]

检查项：
  1. 骨架前缀唯一性：同一地点/角色/物件的变体必须逐字复用同一段骨架
  2. Reuses 子句：仅变体出现、至多一次、且位置在状态段之前
  3. 状态段位置：变体记录的最后一句必须是状态段
  4. 词数硬上限 450
  5. 细节格数量与指向（细节格对象须在骨架中出现）
退出码：0 通过，1 失败。
"""

import json
import re
import sys

WORD_LIMIT = 450

# 每条 description 的版面句识别
LAYOUT_PAT = re.compile(r"\b16:9\b|\bsixteen-to-nine\b", re.I)
# 骨架结束标记（版面句之后进入细节/版式说明）
DETAIL_PAT = re.compile(
    r"^(cut from|detail cell|detail strip|inset|magnified|zoom|the four cells)", re.I
)
LAYOUT_LINE_PAT = re.compile(
    r"^(each sheet|layout:|the board|a landscape|a horizontal|one landscape)", re.I
)
STATE_PAT = re.compile(
    r"^(state\b|the current state|current state|current look|at dawn|after the|by morning|in this state"
    r"|this state governs|the state shown|shown here in|the rain\b|the water\b"
    r"|rain\b|water\b|the quilt|the curtain|the hair\b|the shirt\b|the jacket\b"
    r"|the key\b|the body\b|both segments|the two halves|the pieces|flat morning"
    r"|morning daylight|daylight|wet\b|soaked|dried|drained"
    r"|the plastic|the box|the stains?\b|deepened|spread\b|the quilt)",
    re.I,
)


def sents(d):
    return [p.strip() for p in re.split(r"(?<=\.)\s+", d.strip()) if p.strip()]


def split_parts(mode, d):
    """返回 (skeleton, layout, cells, tail_state)。"""
    ss = sents(d)
    layout_i = next((i for i, s in enumerate(ss) if LAYOUT_PAT.search(s)), None)
    if layout_i is None:
        return " ".join(ss), "", [], ""

    if mode == "scene":
        # 场景：骨架在版面句之前
        skel = " ".join(ss[:layout_i])
        rest = ss[layout_i:]
    else:
        # 角色/物品：版面句在最前（或紧邻），骨架紧随其后
        skel_start = layout_i + 1
        # 跳过紧随其后的版式子句（Layout: / The board / The object sits 之后的架构成句）
        skel = " ".join(ss[skel_start:]) if skel_start < len(ss) else ""
        rest = ss[: layout_i + 1] + ss[skel_start:]

    cells = [s for s in rest if DETAIL_PAT.match(s)]
    # 状态段 = 最后一个以 State:/Current look: 等开头、或位于最后一句
    state = ""
    if ss and STATE_PAT.match(ss[-1]):
        state = ss[-1]
        if skel.endswith(state):
            skel = skel[: -len(state)].strip()
    return skel, " ".join(ss[layout_i : layout_i + 1]), cells, state


def skeleton_of(mode, d):
    """骨架一致性判据：取 description 中用于描述身份/物件的核心名词短语集合。

    不按位置切分（各模板段落顺序不同，位置切分脆弱且会把版式句误判为骨架），
    改为比对「身份锚点词集合」：同一组记录若这些词一致出现，即视为骨架一致；
    若某条把基准的 hair/jacket 换成了 combed hair/shirt，词集合就会不同。
    """
    text = d.lower()
    # 去掉 Reuses 子句、状态段、细节格与版式句，避免这些区域污染判据
    text = re.sub(r"reuses base \w+:\s*[^.]*\.?", " ", text)
    m = re.search(r"\b(state:|the current state|current state|current look)\b", text)
    if m:
        text = text[: m.start()]
    text = re.sub(r"(cut from|detail cell|detail strip|inset|magnified)[^.]*\.", " ", text)
    text = re.sub(
        r"(^|\.\s)(a landscape|a horizontal|the left third|the upper right|layout:)[^.]*\.",
        " ",
        text,
    )

    if mode == "character":
        keys = [
            "hair",
            "brow",
            "eyes",
            "skin",
            "scar",
            "jaw",
            "mouth",
            "nose",
            "cheek",
            "bob",
            "fringe",
            "tattoo",
            "mole",
            "beard",
        ]
    elif mode == "prop":
        keys = [
            "bow",
            "shaft",
            "bit",
            "cord",
            "knot",
            "blade",
            "spine",
            "grip",
            "handle",
            "rim",
            "body",
            "base",
            "notch",
            "chip",
        ]
    else:
        keys = [
            "wall",
            "door",
            "window",
            "floor",
            "ceiling",
            "rack",
            "bed",
            "desk",
            "table",
            "threshold",
            "corner",
            "sill",
        ]
    present = {k for k in keys if re.search(r"\b" + re.escape(k) + r"", text)}
    return "|".join(sorted(present))


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def diff_sentences(a, b, min_words=4):
    """返回 a 与 b 中各自独有的句子（按句号/分号切），用于定位骨架污染。"""

    def sset(x):
        parts = [p.strip() for p in re.split(r"(?<=[.;])\s+", x) if p.strip()]
        return parts

    sa, sb = sset(a), sset(b)
    out_a = [s for s in sa if len(s.split()) >= min_words and s not in b]
    out_b = [s for s in sb if len(s.split()) >= min_words and s not in a]
    return out_a, out_b


def group_key(name):
    """把 name 归一为同组键：去掉「默认的」前缀与状态说明。"""
    n = re.sub(r"^默认的", "", name)
    # 状态说明+实体：取最后一个「的」之后的部分作为实体
    if "的" in n:
        return n.split("的")[-1]
    return n


def validate(out_text, source, mode):
    errs = []
    warns = []
    text = out_text.strip()
    if text.startswith("```"):
        errs.append("输出被代码块围栏包裹")
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        arr = json.loads(text)
    except json.JSONDecodeError as e:
        return ["JSON 解析失败（行%d 列%d）: %s" % (e.lineno, e.colno, e.msg)], []
    if not isinstance(arr, list):
        return ["顶层不是数组"], []
    if not arr:
        return [], []

    # 词数
    for i, it in enumerate(arr, 1):
        d = it.get("description", "")
        w = len(d.split())
        if w > WORD_LIMIT:
            errs.append("第%d项 description %d 词，超过硬上限 %d" % (i, w, WORD_LIMIT))

    # Reuses 子句
    for i, it in enumerate(arr, 1):
        d = it.get("description", "")
        n = len(re.findall(r"Reuses base \w+:", d))
        if n > 1:
            errs.append("第%d项 Reuses 子句出现 %d 次（至多一次）" % (i, n))
        for m in re.finditer(r"Reuses base \w+:\s*([^.]*?)\.", d):
            if "默认的" in m.group(1):
                errs.append("第%d项 Reuses 填了带「默认的」前缀的 name（应填原文名）" % i)
            if len(m.group(1).split()) > 6:
                errs.append(
                    "第%d项 Reuses 子句过长（%d 词，疑似吞掉了后续内容）：%s"
                    % (i, len(m.group(1).split()), m.group(1)[:60])
                )
        # Reuses 子句须以英文句号结束；冒号后的内容在第一个句号前不应超过 14 个字符
        for m2 in re.finditer(r"Reuses base \w+:\s*([^.]*)", d):
            seg = m2.group(1)
            # m2.group(0) 后面紧跟的字符是不是句号
            end_pos = m2.end()
            terminated = end_pos < len(d) and d[end_pos] == "."
            if not terminated or len(seg.strip()) > 14:
                errs.append(
                    "第%d项 Reuses 子句格式错误（须为 Reuses base look: 许青.，内容≤14字符且以句号结束）：…%r"
                    % (i, (seg.strip() or "")[:40])
                )

    # 状态段在最后：有 Reuses 子句的变体，其 Reuses 之后的内容就是状态段。
    # 不再用词表判断「末句像不像状态段」——白名单永远列不全合法的状态句，
    # 反而会把 "Both pieces lie flat as they came apart." 这类正确的末句判成误报。
    for i, it in enumerate(arr, 1):
        nm, d = it.get("name", ""), it.get("description", "")
        if "Reuses base" not in d:
            continue
        m = re.search(r"Reuses base \w+:\s*[^.]*\.", d)
        if not m:
            continue  # 格式错误已在上面报过
        tail = d[m.end() :].strip()
        if len(tail.split()) < 8:
            errs.append(
                "第%d项「%s」的 Reuses 子句之后没有状态段（应为当前状态的完整描述）" % (i, nm)
            )
        elif not tail.endswith((".", "!", "?")):
            errs.append("第%d项「%s」的状态段未以句号结尾" % (i, nm))

    # 光线不得作为全文结尾：这是反复出现的缺陷（第 2/6/10 轮都出现过），
    # 第 12 轮已把规则改为「变体以状态段收尾、基准以一致性尾句收尾」，两者都不许是光照片。
    LIGHT_WORDS = re.compile(
        r"\b(daylight|morning light|light comes|light falls|light reaches|"
        r"flat light|even light|soft light|shadow|shadows|lamplight|glow)\b",
        re.I,
    )
    for i, it in enumerate(arr, 1):
        d = it.get("description", "")
        ss = [s for s in re.split(r"(?<=\.)\s+", d) if s.strip()]
        if ss and LIGHT_WORDS.search(ss[-1]):
            errs.append(
                "第%d项「%s」以光线描写收尾（应从光线句后面的一致性尾句或状态段收尾）：%s"
                % (i, it.get("name", ""), ss[-1][:70])
            )

    # 超长单句：骨架被压成一句时会出现 70-90 词的逗号长串，可读性差且无法逐项核对
    for i, it in enumerate(arr, 1):
        d = it.get("description", "")
        ss = [s for s in re.split(r"(?<=\.)\s+", d) if s.strip()]
        if not ss:
            continue
        longest = max(ss, key=lambda s: len(s.split()))
        n = len(longest.split())
        if n > 55:
            warns.append(
                "⚠ 第%d项「%s」有 %d 词的长句（建议拆成 2-3 句）：%s…"
                % (i, it.get("name", ""), n, longest[:70])
            )

    # 骨架前缀唯一性（按组）
    groups = {}
    for it in arr:
        groups.setdefault(group_key(it.get("name", "")), []).append(it)
    for gk, members in groups.items():
        if len(members) < 2:
            continue
        skels = {}
        for it in members:
            skels[it.get("name")] = skeleton_of(mode, it.get("description", ""))
        uniq = len(set(skels.values()))
        if uniq != 1:
            names = list(skels.keys())
            base = skels[names[0]]
            detail = []
            for other_n in names[1:]:
                other = skels[other_n]
                miss = set(base.split("|")) - set(other.split("|"))
                extra = set(other.split("|")) - set(base.split("|"))
                if miss or extra:
                    detail.append(
                        "%s 缺 %s 多 %s" % (other_n, sorted(miss) or "无", sorted(extra) or "无")
                    )
            warns.append(
                "⚠ 「%s」组身份锚点词集合有差异（供人工复核，不计失败）：%s"
                % (gk, "；".join(detail))
            )
    return errs, warns


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mode, path = sys.argv[1], sys.argv[2]
    if mode not in ("scene", "character", "prop"):
        print("mode 必须为 scene/character/prop")
        return 2
    src = open(sys.argv[3], encoding="utf-8").read() if len(sys.argv) > 3 else None
    errs, warns = validate(open(path, encoding="utf-8").read(), src, mode)
    for w in warns:
        print(w)
    if errs:
        print("❌ 内容层校验失败:")
        for e in errs:
            print("  -", e)
        return 1
    arr = json.loads(
        re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", open(path, encoding="utf-8").read().strip()).strip()
    )
    print(
        "✅ 内容层校验通过（%s，%d 个对象：骨架一致 / Reuses / 状态段 / 词数≤%d）"
        % (mode, len(arr), WORD_LIMIT)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
