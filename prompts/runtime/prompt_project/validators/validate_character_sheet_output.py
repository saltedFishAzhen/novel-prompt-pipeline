#!/usr/bin/env python3
# ruff: noqa: UP031
# -*- coding: utf-8 -*-
"""校验角色设定板提示词输出的 JSON 合规性。

用法：python validate_character_sheet_output.py <output_file> [source_file]
  output_file: 模型输出的 JSON 数组（含代码块围栏会被报告）
  source_file: 含 {{小说原文}} 注入内容的原始请求（可选；提供时校验 name 是否来自原文）
退出码：0 通过，1 失败。"""

import json
import re
import sys

BOARD_HINTS = [
    "16:9",
    "white background",
    "reference sheet",
    "front",
    "side",
    "back",
    "three",
    "turnaround",
]
NOISE = ["qwen", "prompt", "parameter", "4k", "8k", "masterpiece", "best quality"]


def _name_in_source(name: str, aliases: str, source: str) -> bool:
    """name 合法性：命中 name 本身、任一 alias、或 name 的任一 2+ 字连续子串。
    与 validate_asset_sheet_output.name_in_source 同义，支持变体命名（默认的老周）。"""
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


def validate(output_text: str, source: str) -> list:
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
        # 空数组是合法输出（无可提取实体）
        return errs
    names = []
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
            else:
                names.append(nm)
        if not isinstance(d, str) or not d.strip():
            errs.append("第%d项 description 为空" % i)
            continue
        # description 应为英文主体
        cjk = len(re.findall(r"[\u4e00-\u9fff]", d))
        if cjk > 5:
            errs.append("第%d项 description 含大段中文（应为英文）" % i)
        # 设定板布局线索：至少命中2个（正面/侧面/背面或 16:9/白背景）
        low = d.lower()
        hits = [h for h in BOARD_HINTS if h in low]
        if len(hits) < 2:
            errs.append("第%d项 description 缺少设定板布局描述（三视图/16:9/白背景线索不足）" % i)
        # 噪声词：模型名/参数/质量词
        noise = [w for w in NOISE if w in low]
        if noise:
            errs.append("第%d项 description 含禁止词: %s" % (i, noise))
        # 三份资产提示词统一禁用 character 一词（场景/物品/角色）
        if re.search(r"\bcharacters?\b", d, re.I):
            errs.append(
                "第%d项 description 含 character 一词（三份资产提示词统一整词禁用，版式用 turnaround sheet）"
                % i
            )
        # 长度参考：超过300词提示（警告不计入失败）
        words = len(d.split())
        if words > 450:
            errs.append("第%d项 description 过长（%d 词，建议≤400，硬上限 450）" % (i, words))
        # name 不应作为画面文字出现在 description（允许引用特征而非名字）
        if isinstance(nm, str) and nm.strip() and len(nm.strip()) > 1 and nm.strip() in d:
            errs.append(
                "第%d项 description 中出现了 name（%r）本身，名称不是画面文字" % (i, nm[:20])
            )
    # 重复检查：除同一角色多造型外，name 不应重复且 description 相同
    seen = {}
    for item in arr if isinstance(arr, list) else []:
        if isinstance(item, dict) and "name" in item and "description" in item:
            key = (item["name"], item["description"])
            if key in seen:
                errs.append("完全重复的记录（name+description 相同）")
                break
            seen[key] = True
    # name 来源检查（源文件提供时）：与 scene/prop 校验器保持一致——
    # 命中 name 本身、任一 alias、或 name 中任一 2+ 字连续子串即通过，
    # 以支持「默认的+角色」「状态+角色」这类变体命名。
    if source is not None:
        for i, item in enumerate(arr, 1):
            if not isinstance(item, dict):
                continue
            nm = item.get("name")
            al = item.get("aliases")
            if not isinstance(nm, str) or not nm.strip():
                continue
            if nm == "我":
                continue
            if not _name_in_source(nm, al if isinstance(al, str) else "", source):
                errs.append("name %r 未见原文出现（疑似编造）" % nm[:20])
    return errs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out = open(sys.argv[1], encoding="utf-8").read()
    src = None
    if len(sys.argv) > 2:
        try:
            src = open(sys.argv[2], encoding="utf-8").read()
        except OSError as e:
            print("（警告）无法读取输入源文件，跳过 name 来源检查: %s" % e)
    errs = validate(out, src)
    if errs:
        print("❌ 校验失败:")
        for p in errs:
            print("  -", p)
        return 1
    clean = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out.strip()).strip()
    arr = json.loads(clean)
    print("✅ 角色设定板校验通过（%d 个对象）" % len(arr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
