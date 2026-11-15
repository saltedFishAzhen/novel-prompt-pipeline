"""Render a validated director table to a natural-language H3 request."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .schema import ShotTable

_BREAK = re.compile(r"<br\s*/?>", re.I)
_TERMINAL = "。！？…”’」』.!?)"


def _seconds(ms: int) -> str:
    whole, fraction = divmod(ms, 1000)
    return str(whole) if not fraction else f"{whole}.{fraction:03d}".rstrip("0")


def _field(label: str, value: str) -> str:
    """Render one cell as a single labelled line.

    ``<br>`` is the table's in-cell line break, so it becomes a separator rather
    than staying in the prompt as a literal tag. A cell that already ends with
    sentence punctuation keeps its own full stop.
    """
    text = _BREAK.sub("；", value).strip()
    return f"{label}：{text}" if text.endswith(tuple(_TERMINAL)) else f"{label}：{text}。"


def render_storyboard(
    table: ShotTable,
    *,
    reference_roles: Sequence[Mapping[str, Any]] | None = None,
    constraints: Sequence[str] | None = None,
) -> str:
    """Render every original cell without creating or rewriting direction."""
    lines = [f"总时长{_seconds(table.duration_ms)}秒，共{len(table.rows)}个镜头。"]
    if table.type == "i2va":
        lines.append("<Picture 1>是视频的实际首帧。")
    for role in reference_roles or []:
        use = "、".join(role["use"])
        text = f"<Picture {role['picture']}>用于{role['subject']}：{use}"
        if role.get("exclude"):
            text += "；不沿用：" + "、".join(role["exclude"])
        lines.append(text + "。")
    groups = Counter(
        event.group_id
        for row in table.rows
        for event in row.dialogue_events
        if event.group_id is not None
    )
    announced: set[str] = set()
    for row in table.rows:
        lines.append("")
        lines.append(f"镜头{row.number}，{_seconds(row.start_ms)}至{_seconds(row.end_ms)}秒：")
        if row.number == 1:
            lines.append("切换：开场。")
        elif row.transition == "cut":
            lines.append(f"在{_seconds(row.start_ms)}秒硬切入本镜。")
        else:
            name = {"cross_dissolve": "交叉叠化", "fade": "淡化", "wipe": "划像"}[row.transition]
            finish = row.start_ms + row.transition_duration_ms
            lines.append(
                f"在{_seconds(row.start_ms)}秒开始{_seconds(row.transition_duration_ms)}秒{name}，{_seconds(finish)}秒结束。"
            )
        lines.extend(
            [
                _field("参考图", row.reference_text),
                _field("景别", row.shot_size),
                _field("机位/角度", row.camera_angle),
                _field("运镜", row.camera_movement),
                _field("动作调度", row.blocking),
                _field("情绪/潜台词（仅作表演意图，不新增对白或旁白）", row.emotion_subtext),
                _field("台词（仅此栏为实际对白，保持原话与标点）", row.dialogue_text),
                _field("台词与肢体的关系", row.dialogue_body_relation),
                _field("道具/场景层次", row.scene_layers),
                _field("镜头衔接", row.continuity),
                _field("导演备注（仅供执行，不新增事件、对白或旁白）", row.director_notes),
            ]
        )
        # Announce a split sentence once, at its first shot.
        for group_id in dict.fromkeys(
            event.group_id for event in row.dialogue_events if event.group_id
        ):
            if groups[group_id] > 1 and group_id not in announced:
                announced.add(group_id)
                lines.append(f"{group_id}的各段是同一句话，按原表跨镜连续说完，只说一次。")
    if constraints:
        lines.append("")
        lines.append("全局约束：" + "；".join(constraints) + "。")
    return "\n".join(lines)
