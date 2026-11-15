"""Parse and check a director's Markdown shot table without model inference."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .schema import DialogueEvent, ShotRow, ShotTable


class TableValidationError(ValueError):
    """A table cannot be interpreted safely under the stated input contract."""


_ALIASES = {
    "number": ("镜号", "镜头号", "镜头编号", "序号", "编号", "shot"),
    "time": ("时间", "时间段", "时间范围", "时段", "起止时间", "时间(秒)"),
    "references": ("参考图", "参考图片", "引用图", "参考图引用", "图片引用"),
    "shot_size": ("景别", "镜头景别"),
    "camera_angle": ("机位/角度", "机位角度", "机位", "机位/视角", "摄像机位置"),
    "camera_movement": ("运镜", "镜头运动", "相机运动", "镜头运镜"),
    "blocking": ("动作调度", "人物动作", "动作", "动作与调度", "表演调度"),
    "emotion_subtext": ("情绪/潜台词", "情绪潜台词", "情绪", "人物情绪", "情绪与潜台词"),
    "dialogue": ("台词", "对白", "对话", "对白/声音"),
    "dialogue_body_relation": (
        "台词与肢体的关系",
        "台词与肢体关系",
        "台词与动作的关系",
        "对白与动作关系",
        "台词肢体关系",
    ),
    "scene_layers": ("道具/场景层次", "道具场景层次", "道具与场景层次", "道具与场景", "场景层次"),
    "continuity": ("衔接", "转场/衔接", "转场衔接", "镜头衔接", "转场"),
    "director_notes": ("导演备注", "导演说明", "备注", "导演提示"),
}
_NONE = frozenset(
    {
        "无",
        "无对白",
        "无台词",
        "无对话",
        "静音",
        "—",
        "-",
        "/",
        "n/a",
        "none",
        "无声",
        "no dialogue",
        "no speech",
        "silent",
        "not applicable",
        "no reference",
    }
)


def _is_none(value: str) -> bool:
    """Recognize an explicit absence, including optional final punctuation."""
    text = value.strip().casefold().rstrip("。.;； ")
    return text in _NONE


_NUMBER = r"\d+(?::\d{1,2}(?:\.\d+)?|\.\d+)?"
_SECONDS_UNIT = r"(?:seconds|second|secs|sec|秒|s)"
_TIME_RANGE = rf"{_NUMBER}\s*{_SECONDS_UNIT}?\s*[-–—~～至到]\s*{_NUMBER}\s*{_SECONDS_UNIT}?"
_QUOTE = r'(?P<quote>“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*")'
_DIALOGUE = re.compile(
    rf"(?:(?P<group>D[\w-]+)\s+(?P<part>\d+)\s*/\s*(?P<total>\d+)\s+)?"
    rf"(?:(?P<before>{_TIME_RANGE})\s+)?"
    rf"(?P<speaker>[^：:\n\[\]“”‘’「」『』\"；;]+?)\s*"
    rf"(?:\[(?P<after>{_TIME_RANGE})\])?\s*[：:]\s*{_QUOTE}",
    re.IGNORECASE,
)


def _header_key(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).lower().strip(":：")


_HEADER_MAP = {
    _header_key(alias): canonical for canonical, aliases in _ALIASES.items() for alias in aliases
}


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _table_rows(text: str) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise TableValidationError("storyboard_table must be non-empty Markdown text")
    lines = text.splitlines()
    candidates: list[tuple[int, list[str]]] = []
    for i in range(len(lines) - 1):
        if "|" not in lines[i] or "|" not in lines[i + 1]:
            continue
        headings = _cells(lines[i])
        separator = _cells(lines[i + 1])
        if len(separator) != len(headings) or not all(
            re.fullmatch(r":?-{3,}:?", x) for x in separator
        ):
            continue
        mapped = [_HEADER_MAP.get(_header_key(x), "") for x in headings]
        if len(mapped) == len(_ALIASES) and set(mapped) == set(_ALIASES):
            candidates.append((i + 2, mapped))
    if len(candidates) != 1:
        raise TableValidationError(
            "expected exactly one Markdown table with all 13 recognized columns"
        )
    first, headings = candidates[0]
    rows = []
    for line in lines[first:]:
        if not line.strip() or "|" not in line:
            break
        cells = _cells(line)
        if len(cells) != len(headings):
            raise TableValidationError(
                f"shot row {len(rows) + 1} must contain exactly 13 cells; escape literal pipes"
            )
        row = dict(zip(headings, cells, strict=True))
        empty = [name for name, value in row.items() if not value]
        if empty:
            raise TableValidationError(
                f"shot row {len(rows) + 1} has empty cells: {', '.join(empty)}; write 无 when absent"
            )
        rows.append(row)
    if not rows:
        raise TableValidationError("shot table must contain at least one row")
    return rows


def _milliseconds(value: str) -> int:
    value = re.sub(rf"{_SECONDS_UNIT}\s*$", "", value.strip(), flags=re.I).strip()
    if value.startswith(("从", "第")):
        value = value[1:].strip()
    parts = value.split(":")
    try:
        if len(parts) == 1:
            seconds = Decimal(parts[0])
        elif len(parts) == 2 and parts[0].isdigit():
            seconds_part = Decimal(parts[1])
            if seconds_part < 0 or seconds_part >= 60:
                raise ValueError
            seconds = Decimal(parts[0]) * 60 + seconds_part
        else:
            raise ValueError
        millis = seconds * 1000
        if not seconds.is_finite() or seconds < 0 or millis != millis.to_integral_value():
            raise ValueError
        return int(millis)
    except (InvalidOperation, ValueError, OverflowError):
        raise TableValidationError(
            f"invalid time {value!r}; use nonnegative seconds with millisecond precision"
        ) from None


def _range(value: str) -> tuple[int, int]:
    parts = re.split(r"\s*(?:[-–—~～至到])\s*", value.strip())
    if len(parts) != 2:
        raise TableValidationError(f"invalid time range {value!r}; use absolute start–end seconds")
    start, end = map(_milliseconds, parts)
    if start >= end:
        raise TableValidationError(f"time range {value!r} must end after it starts")
    return start, end


def _image_numbers(value: str, image_count: int) -> list[int]:
    if _is_none(value):
        return []
    numbers = [int(x) for x in re.findall(r"(?:图(?:片)?|picture)\s*(\d+)", value, re.IGNORECASE)]
    if not numbers:
        raise TableValidationError(f"reference cell {value!r} must use 图1 or <Picture 1>")
    if any(not 1 <= n <= image_count for n in numbers):
        raise TableValidationError("shot table refers to an unavailable reference image")
    return list(dict.fromkeys(numbers))


def _speaker_and_language(label: str) -> tuple[str, str | None]:
    label = label.strip()
    language = None
    match = re.search(r"[（(]([^（）()]*)[）)]\s*$", label)
    if match:
        details = re.split(r"[,，、/\s]+", match.group(1).strip().lower())
        languages = {
            "普通话",
            "中文",
            "汉语",
            "mandarin",
            "chinese",
            "英语",
            "英文",
            "english",
            "日语",
            "日文",
            "japanese",
            "粤语",
            "cantonese",
        }
        candidates = [item for item in details if item in languages]
        if candidates:
            language = candidates[0]
        if candidates or all(
            item in {"画外", "屏外", "画内", "offscreen", "on-screen", "旁白"} for item in details
        ):
            label = label[: match.start()].strip()
    if not label:
        raise TableValidationError("dialogue requires a stable speaker name")
    return label, language


def _dialogue_events(value: str, start: int, end: int) -> list[DialogueEvent]:
    if _is_none(value):
        return []
    pos = 0
    events = []
    while pos < len(value):
        separator = re.match(r"(?:\s|[；;]|<br\s*/?>)+", value[pos:], flags=re.I)
        if separator:
            pos += separator.end()
            if pos == len(value):
                break
        match = _DIALOGUE.match(value, pos)
        if not match:
            raise TableValidationError(
                f"cannot parse dialogue at {value[pos : pos + 35]!r}; use 人物（语言）：「原话」"
            )
        group = match.group("group")
        part = int(match.group("part")) if group else None
        total = int(match.group("total")) if group else None
        if group and (total is None or total < 2 or part is None or not 1 <= part <= total):
            raise TableValidationError("split dialogue requires D1 1/2 ... D1 2/2 numbering")
        if match.group("before") and match.group("after"):
            raise TableValidationError("dialogue must not specify time twice")
        timing = match.group("before") or match.group("after")
        event_start, event_end = _range(timing) if timing else (None, None)
        if timing and not (start <= event_start < event_end <= end):
            raise TableValidationError("dialogue time must fall entirely within its shot")
        if group and timing is None:
            raise TableValidationError(
                "split dialogue chunks require explicit absolute time ranges"
            )
        speaker, language = _speaker_and_language(match.group("speaker"))
        quote = match.group("quote")
        spoken = quote[1:-1]
        if not spoken.strip():
            raise TableValidationError("dialogue cannot be empty")
        events.append(
            DialogueEvent(
                speaker=speaker,
                language=language,
                text=spoken,
                start_ms=event_start,
                end_ms=event_end,
                group_id=group,
                part_index=part,
                part_total=total,
            )
        )
        pos = match.end()
    if not events:
        raise TableValidationError("dialogue cell has no parseable dialogue")
    return events


def _source_dialogue(source: str) -> list[str]:
    """Extract clearly introduced quoted speech; not a general semantic parser."""
    pattern = re.compile(
        r"(?:(?:台词|对白|旁白|对话)(?=\s*(?:是|为|[：:“‘「『\"]))|"
        r"说|问|回答|喊|叫道|叫喊|说道|答道|唱|念|低声道|轻声道|大声道|告诉|答复|回应|"
        r"\b(?:says|said|say|asks|asked|replies|replied|answers|answered|whispers|shouts|narrates|sings|speaks)\b)"
        r"[^“‘「『\"\n]{0,48}(?:“([^”]+)”|‘([^’]+)’|「([^」]+)」|『([^』]+)』|\"([^\"]+)\")",
        re.IGNORECASE,
    )
    matches = []
    for match in pattern.finditer(source):
        before = source[max(0, match.start() - 24) : match.start()]
        if re.search(
            r"(?:无|没|没有|不含|不新增|没有任何|不|未|别|并未|从未)$", before
        ) or re.search(r"\b(?:not|never|without|doesn't|didn't|don't)\s+$", before, re.IGNORECASE):
            continue
        words = next(part for part in match.groups() if part is not None)
        fragment = bool(re.search(r"前半句|后半句|前半段|后半段|第一部分|第二部分", match.group(0)))
        matches.append((words, fragment))
    full = [words for words, fragment in matches if not fragment]
    return [
        words
        for words, fragment in matches
        if not fragment or not any(words in line for line in full)
    ]


def _check_dialogue(rows: list[ShotRow], source: str) -> None:
    groups: dict[str, list[tuple[ShotRow, DialogueEvent]]] = defaultdict(list)
    events: list[tuple[ShotRow, DialogueEvent]] = []
    timed_by_speaker: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        for event in row.dialogue_events:
            events.append((row, event))
            if event.group_id:
                groups[event.group_id].append((row, event))
            if event.start_ms is not None and event.end_ms is not None:
                timed_by_speaker[event.speaker].append((event.start_ms, event.end_ms))
    for speaker, times in timed_by_speaker.items():
        times.sort()
        if any(b[0] < a[1] for a, b in zip(times, times[1:])):
            raise TableValidationError(f"speaker {speaker!r} has overlapping timed dialogue")
    lines: list[str] = []
    consumed: set[str] = set()
    for row, event in events:
        if not event.group_id:
            lines.append(event.text)
            continue
        if event.group_id in consumed:
            continue
        chunks = groups[event.group_id]
        consumed.add(event.group_id)
        total = event.part_total
        if len(chunks) != total or [item.part_index for _, item in chunks] != list(
            range(1, total + 1)
        ):
            raise TableValidationError(
                f"dialogue group {event.group_id} must contain all parts in order"
            )
        if any(
            item.part_total != total
            or item.speaker != event.speaker
            or item.language != event.language
            for _, item in chunks
        ):
            raise TableValidationError(
                f"dialogue group {event.group_id} must keep speaker, language and part count"
            )
        for (left_row, left), (right_row, right) in zip(chunks, chunks[1:]):
            if (
                right_row.number != left_row.number + 1
                or left.end_ms != right.start_ms
                or left.end_ms != left_row.end_ms
                or right.start_ms != right_row.start_ms
            ):
                raise TableValidationError(
                    f"dialogue group {event.group_id} must continue at adjacent shot boundaries without a gap"
                )
        lines.append("".join(item.text for _, item in chunks))
    expected = _source_dialogue(source)
    if expected:
        if lines != expected:
            raise TableValidationError(
                "dialogue must match the source's explicitly quoted spoken lines exactly once and in order"
            )
    elif lines:
        raise TableValidationError(
            "cannot verify dialogue: add the original spoken line in quotes after 说/台词 in source_text"
        )


def _transition_mentions(source: str, names: Sequence[str]) -> tuple[bool, bool]:
    text = source.casefold()
    positive = negative = False
    pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    for match in re.finditer(pattern, text):
        prefix = re.split(r"[。；;，,\n：:]", text[max(0, match.start() - 32) : match.start()])[-1]
        negated = bool(
            re.search(
                r"(?:而非|而不是|没有|并未|未|无|非|不(?:允许|使用|采用|用|要|能|应|会|得|可|需要|进行|再)?"
                r"|禁止|避免|排除|不得|不可|禁用|无需|切勿|拒绝|弃用|舍弃)"
                r"(?:任何|其他|所有|特殊|特别|额外|这类|这种|此|该|的|形式|一种|一个|交叉|式|效果"
                r"|过渡|转场|叠化|淡入淡出|淡入|淡出|溶解|划像|硬切|直切|擦除|使用|采用|进行"
                r"|允许|添加|出现|引入|以及|或者|或|与|和|、|\s){0,18}$",
                prefix,
            )
            or re.search(
                r"\b(?:no|not|without|never|avoid|prohibit|forbid|disallow)\s+(?:(?:use|using|a|an|any|the|cross|transition|transitions|soft|dissolve|fade|wipe|and|or)\s+){0,12}$",
                prefix,
            )
        )
        negative |= negated
        positive |= not negated
    return positive, negative


def _transition(
    value: str,
    number: int,
    source: str,
    prefs: Mapping[str, Any],
    shot_length_ms: int,
    shot_start_ms: int,
) -> tuple[str, int]:
    lower = value.casefold()
    kinds = {
        "cross_dissolve": (
            "交叉叠化",
            "叠化",
            "溶解",
            "cross-dissolve",
            "cross dissolve",
            "dissolve",
        ),
        "fade": ("淡入", "淡出", "渐入", "渐出", "渐隐", "渐显", "fade"),
        "wipe": ("划像", "擦除", "wipe"),
    }
    selected = [kind for kind, names in kinds.items() if _transition_mentions(lower, names)[0]]
    hard, no_hard = _transition_mentions(
        lower, ("硬切", "直接切", "直切", "hard-cut", "hard cut", "cut")
    )
    if number == 1:
        # "开场（硬切进入）" can describe an editorial entry into this
        # first shot; it does not declare a prior shot within this clip.
        explicit_opening = bool(re.search(r"开场|起始|首镜|opening|first\s+shot", lower))
        if selected or hard and not explicit_opening:
            raise TableValidationError("first shot must start as opening, not a cut or transition")
        amount = (
            r"(\d+(?:\.\d+)?)\s*(?:milliseconds|millisecond|毫秒|ms|seconds|second|secs|sec|秒|s)"
        )
        # A shot's end time or total duration is not a transition duration.
        # Only reject a nonzero amount explicitly attached to the entry cut.
        duration_patterns = (
            rf"{amount}\s*(?:的)?\s*(?:硬切|直接切|直切|hard[ -]?cut)",
            rf"(?:硬切|直接切|直切|hard[ -]?cut)(?:转场|过渡)?\s*(?:持续|历时|时长|为)\s*[:：]?\s*{amount}",
            rf"(?:转场|过渡)(?:持续|历时|时长)\s*[:：]?\s*{amount}",
        )
        for pattern in duration_patterns:
            for match in re.finditer(pattern, lower, re.I):
                if Decimal(match.group(1)) != 0:
                    raise TableValidationError(
                        "first shot opening cannot have a transition duration"
                    )
        return "opening", 0
    if len(selected) > 1 or selected and hard:
        raise TableValidationError(f"shot {number}: transition is ambiguous or contradictory")
    if not selected:
        if no_hard:
            raise TableValidationError(
                f"shot {number}: hard cut is forbidden; specify an authorized transition"
            )
        generic, _ = _transition_mentions(lower, ("转场", "过渡", "渐变", "transition"))
        if generic and not hard:
            raise TableValidationError(
                f"shot {number}: specify the transition type and any soft-transition duration"
            )
        _check_transition_times(lower, number, shot_start_ms, 0)
        return "cut", 0
    kind = selected[0]
    if re.search(
        r"(?:全部|全程|均|都|只|仅)(?:使用|采用|是|为)?硬切|\b(?:only|all)\s+hard[ -]?cuts?\b",
        source,
        re.I,
    ):
        raise TableValidationError(f"shot {number}: source requires hard cuts only")
    allowed, forbidden = _transition_mentions(source, kinds[kind])
    _, forbid_all = _transition_mentions(
        source, ("转场", "过渡效果", "transitions", "transition effects")
    )
    if forbidden or forbid_all:
        raise TableValidationError(f"shot {number}: source explicitly forbids this transition")
    preferences_allow = prefs.get("allow_transitions", [])
    if isinstance(preferences_allow, str):
        preferences_allow = [preferences_allow]
    if not allowed and kind not in preferences_allow and prefs.get("default_transition") != kind:
        raise TableValidationError(
            f"shot {number}: soft transition needs positive source or preference authorization"
        )
    amount_pattern = (
        r"(\d+(?:\.\d+)?)\s*(milliseconds|millisecond|毫秒|ms|seconds|second|secs|sec|秒|s)"
    )
    soft_names = "|".join(re.escape(name) for name in kinds[kind])
    # Prefer a marked duration or the amount directly attached to the
    # transition. A cut-time such as "5秒开始叠化" is not its duration.
    match = (
        re.search(
            rf"(?:{soft_names})[^;\n]{{0,100}}?(?:持续|时长|历时|用时|duration)\s*[:：]?\s*{amount_pattern}",
            lower,
        )
        or re.search(
            rf"(?:{soft_names})[^;\n]{{0,80}}\b(?:lasting|lasts)\s*(?:for\s+)?{amount_pattern}",
            lower,
        )
        or re.search(
            rf"(?:{soft_names})(?:\s+(?:begins?|starts?)(?:\s+at\s+{_NUMBER}\s*{_SECONDS_UNIT})?)?\s+for\s+{amount_pattern}",
            lower,
        )
        or re.search(rf"{amount_pattern}\s*(?:的|进行)?\s*(?:交叉)?(?:{soft_names})", lower)
        or re.search(rf"(?:{soft_names})\s*(?:持续|时长|为)?\s*[:：]?\s*{amount_pattern}", lower)
    )
    if match is None:
        amounts = list(re.finditer(amount_pattern, lower))
        if len(amounts) == 1 and not re.search(
            r"开始|起始|起点|结束|完成|\b(?:at|start|end)\b", lower
        ):
            match = amounts[0]
    if not match:
        raise TableValidationError(
            f"shot {number}: soft transition needs an explicit duration such as 0.5秒"
        )
    amount = Decimal(match.group(1)) * (
        1 if match.group(2) in {"毫秒", "ms", "milliseconds", "millisecond"} else 1000
    )
    if amount != amount.to_integral_value() or not 0 < amount <= shot_length_ms:
        raise TableValidationError(f"shot {number}: invalid soft transition duration")
    _check_transition_times(lower, number, shot_start_ms, int(amount))
    return kind, int(amount)


def _check_transition_times(value: str, number: int, start_ms: int, duration_ms: int) -> None:
    """Reject unambiguous absolute transition anchors that disagree with the row."""
    unit_time = rf"({_NUMBER}\s*{_SECONDS_UNIT})"
    start_patterns = (
        rf"{unit_time}\s*(?:整)?\s*(?:硬切|直接切|直切|切入|切至|切换)",
        rf"{unit_time}\s*(?:整)?\s*(?:开始|起始)\s*(?:\d+(?:\.\d+)?\s*(?:毫秒|ms|秒|s)\s*)?(?:交叉)?(?:叠化|溶解|淡入|淡出|渐入|渐出|划像|擦除|转场|过渡|dissolve|fade|wipe|transition)",
        rf"(?:切点|转场起点|切换时间|起始时间)\s*[:：]?\s*{unit_time}",
        rf"\b(?:cut\s+at|start\s+at)\s*{unit_time}",
        rf"\b(?:cross[ -]?dissolve|fade|wipe|transition|hard[ -]?cut|cut)\s+(?:starts?|begins?)\s+at\s*{unit_time}",
        rf"\bat\s*{unit_time}\s*,?\s*(?:(?:the|a)\s+)?(?:(?:camera\s+)?(?:hard[ -]?cuts?|cuts?)|(?:begin|begins|start|starts)\s+(?:a\s+|the\s+)?(?:cross[ -]?dissolve|fade|wipe|transition)|(?:cross[ -]?dissolve|fade|wipe|transition)\s+(?:begins?|starts?))\b",
        rf"{unit_time}\s*(?:,\s*)?(?:a\s+)?(?:hard[ -]?cuts?|cuts?)\b",
    )
    for pattern in start_patterns:
        for match in re.finditer(pattern, value, re.I):
            if _milliseconds(match.group(1)) != start_ms:
                raise TableValidationError(
                    f"shot {number}: transition start time disagrees with row start"
                )
    if duration_ms == 0:
        # A hard cut or the opening has no duration, so no absolute end time can
        # describe it: "本镜6秒结束" is the shot's end, not a transition's.
        return
    end_patterns = (
        rf"{unit_time}\s*(?:整)?\s*(?:结束|完成)",
        rf"(?:结束于|完成于|end\s+at)\s*{unit_time}",
        rf"\b(?:end(?:s|ing)?|finish(?:es|ing)?|complet(?:e|es|ing))\s+at\s*{unit_time}",
    )
    for pattern in end_patterns:
        for match in re.finditer(pattern, value, re.I):
            before = value[: match.start()]
            markers = list(
                re.finditer(
                    r"叠化|溶解|淡入|淡出|渐入|渐出|划像|擦除|转场|硬切|直切|切入|dissolve|fade|wipe|transition|\bcut\b",
                    before,
                    re.I,
                )
            )
            # A previous shot's or an actor's end time is not the end of the
            # transition. Check only an end time following a transition name.
            if not markers:
                continue
            since_marker = before[markers[-1].end() :]
            current_clause = re.split(r"[。；;，,]", since_marker)[-1]
            after = value[match.end() :]
            if re.search(
                r"前镜|上一镜|上镜|本镜|该镜|后镜|人物|台词|对白|说话|讲话|动作|转身|表情|视线"
                r"|(?:previous|first|current|this|the|new|next)\s+shot|\b(?:actor|character|girl|boy|person|speaker|line|dialogue|action|movement|turn|voice)\b",
                current_clause,
                re.I,
            ) or re.match(
                r"\s*(?:动作|讲话|说话|对白|表演|台词|转身|表情|视线|speaking|talking|dialogue|action|turning)",
                after,
                re.I,
            ):
                continue
            # An end time is only the transition's own end when the cell binds it
            # to the transition, either with a duration word or as "结束于X".
            bound = bool(
                re.match(
                    r"(?:结束于|完成于|end(?:s|ing)?\s+at|finish(?:es|ing)?\s+at|complet(?:e|es|ing)\s+at)",
                    match.group(0),
                    re.I,
                )
                or re.search(
                    r"持续|时长|历时|用时|duration|\blasting\b|\blast(?:ing|s)\b|\bfor\b",
                    since_marker,
                    re.I,
                )
            )
            if not bound:
                continue
            if _milliseconds(match.group(1)) != start_ms + duration_ms:
                raise TableValidationError(
                    f"shot {number}: transition end time disagrees with its duration"
                )


def _check_source_cut_claims(source: str, rows: list[ShotRow]) -> None:
    """Check only narrow, explicit absolute transition statements in source."""
    pattern = re.compile(
        rf"(?<![每过后距])({_NUMBER}\s*{_SECONDS_UNIT})\s*(?:整)?\s*"
        # "切入画面" describes a subject entering the frame, not a cut.
        r"(硬切|直接切到|直接切至|切至|切入(?!画面)|开始交叉叠化|开始叠化)",
        re.I,
    )
    indexed = {row.start_ms: row for row in rows[1:]}
    for match in pattern.finditer(source):
        prefix = source[max(0, match.start() - 4) : match.start()]
        if re.search(r"(?:不|不要|避免|禁止|不得|不可|不能)(?:在|于)?$", prefix):
            continue
        moment = _milliseconds(match.group(1))
        if moment >= rows[-1].end_ms:
            # An out-point at the very end of the clip starts no shot.
            continue
        row = indexed.get(moment)
        if row is None:
            raise TableValidationError("an explicit source cut time is missing from the shot table")
        expected = "cross_dissolve" if "叠化" in match.group(2) else "cut"
        if row.transition != expected:
            raise TableValidationError("shot transition type conflicts with an explicit source cut")

    english = re.compile(
        rf"\b(?P<kind>hard[ -]?cut|cross[ -]?dissolve|fade|wipe)\s+"
        rf"(?:(?:starts?|begins?)\s+)?at\s*(?P<time>{_NUMBER}\s*{_SECONDS_UNIT})\b",
        re.I,
    )
    for match in english.finditer(source):
        prefix = source[max(0, match.start() - 32) : match.start()]
        if re.search(
            r"\b(?:no|not|never|without|avoid|prohibit|forbid|disallow)\s+"
            r"(?:(?:use|using|a|an|any|the)\s+){0,4}$",
            prefix,
            re.I,
        ):
            continue
        moment = _milliseconds(match.group("time"))
        if moment >= rows[-1].end_ms:
            continue
        row = indexed.get(moment)
        if row is None:
            raise TableValidationError("an explicit source cut time is missing from the shot table")
        kind = match.group("kind").lower()
        expected = "cut" if "cut" in kind else "cross_dissolve" if "dissolve" in kind else kind
        if row.transition != expected:
            raise TableValidationError("shot transition type conflicts with an explicit source cut")


def _camera_conflict(value: str, number: int) -> None:
    spec = value.casefold()
    spec = re.sub(
        r"(?:无|不)(?:推拉摇移升降|推拉摇移|运镜|移动|推镜|拉镜|摇镜|平移|跟拍|环绕|旋转|升降)",
        "",
        spec,
    )
    motion = r"(?:push|pull|pan|tilt|truck|zoom|track|orbit|roll|dolly|pedestal|handheld|movement|motion)\w*\b"
    # A negation extends across a simple comma/and/or list, but not across
    # contrast, sequencing, or an explicit positive permission.
    negated_item = rf"{motion}(?!\s+(?:is\s+)?(?:allowed|permitted|enabled)\b)"
    spec = re.sub(
        rf"\b(?:no|without|not)\s+(?:camera\s+)?{negated_item}"
        rf"(?:(?:\s*,\s*(?!(?:but|then)\b)|\s+(?:and|or)\s+(?!(?:but|then)\b)){negated_item})*",
        "",
        spec,
    )
    static = re.search(r"\b(?:static|locked(?:-off)?|stationary|fixed)\b|固定|静止|静态", spec)
    moving = re.search(
        r"\b(?:push|pull|pan|tilt|truck|zoom|track|orbit|roll|dolly|pedestal)\w*\b|推镜|拉镜|摇镜|平移|跟拍|环绕|旋转|俯仰|升降",
        spec,
    )
    if static and moving:
        raise TableValidationError(f"shot {number}: static camera conflicts with camera motion")


def parse_table(
    text: str,
    *,
    type: str,
    source_text: str,
    duration_seconds: int,
    image_count: int,
    preferences: Mapping[str, Any] | None = None,
) -> ShotTable:
    """Parse a single 13-column Markdown table, validating deterministic facts."""
    if type not in {"i2va", "ref2va"}:
        raise TableValidationError("type must be i2va or ref2va")
    if not isinstance(source_text, str) or not source_text.strip():
        raise TableValidationError("source_text must not be empty")
    if (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or duration_seconds <= 0
    ):
        raise TableValidationError("duration_seconds must be a positive integer")
    if (
        not isinstance(image_count, int)
        or isinstance(image_count, bool)
        or not 1 <= image_count <= 9
        or type == "i2va"
        and image_count != 1
    ):
        raise TableValidationError("i2va requires one image; ref2va requires 1-9 images")
    prefs = dict(preferences or {})
    rows: list[ShotRow] = []
    expected_start = 0
    for index, raw in enumerate(_table_rows(text), 1):
        number_match = re.fullmatch(r"(?:镜头?|shot|#)?\s*(\d+)", raw["number"], re.I)
        if not number_match or int(number_match.group(1)) != index:
            raise TableValidationError("shot numbers must be consecutive starting at 1")
        start, end = _range(raw["time"])
        if start != expected_start or end > duration_seconds * 1000:
            raise TableValidationError(
                f"shot {index}: times must cover the timeline without gaps, overlaps or exceeding duration"
            )
        expected_start = end
        refs = _image_numbers(raw["references"], image_count)
        if type == "i2va" and refs != [1]:
            raise TableValidationError(
                f"shot {index}: i2va rows must reference the single first frame as 图1"
            )
        events = _dialogue_events(raw["dialogue"], start, end)
        transition, transition_ms = _transition(
            raw["continuity"], index, source_text, prefs, end - start, start
        )
        _camera_conflict(raw["camera_angle"] + "；" + raw["camera_movement"], index)
        rows.append(
            ShotRow(
                number=index,
                start_ms=start,
                end_ms=end,
                reference_images=refs,
                reference_text=raw["references"],
                time_text=raw["time"],
                shot_size=raw["shot_size"],
                camera_angle=raw["camera_angle"],
                camera_movement=raw["camera_movement"],
                blocking=raw["blocking"],
                emotion_subtext=raw["emotion_subtext"],
                dialogue_text=raw["dialogue"],
                dialogue_events=events,
                dialogue_body_relation=raw["dialogue_body_relation"],
                scene_layers=raw["scene_layers"],
                continuity=raw["continuity"],
                director_notes=raw["director_notes"],
                transition=transition,
                transition_duration_ms=transition_ms,
            )
        )
    if expected_start != duration_seconds * 1000:
        raise TableValidationError("shot table must end exactly at duration_seconds")
    _check_source_cut_claims(source_text, rows)
    requested = prefs.get("shot_count")
    if requested is not None and (
        not isinstance(requested, int)
        or isinstance(requested, bool)
        or requested <= 0
        or requested != len(rows)
    ):
        raise TableValidationError("table does not match a valid preferences.shot_count")
    _check_dialogue(rows, source_text)
    return ShotTable(type=type, duration_ms=duration_seconds * 1000, rows=rows)
