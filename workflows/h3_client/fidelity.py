"""Check explicitly timed dialogue emitted by the local storyboard renderer.

This deliberately does not try to prove the semantic fidelity of free text.
Only renderer-labelled dialogue with an explicit range and quoted words is
checked, and only against ranges in the corresponding H3 dialogue sentence.
"""

from __future__ import annotations

import re
from decimal import Decimal

_LABEL = re.compile(r"^\s*台词[（(]仅此栏为实际对白，保持原话与标点[）)]\s*[：:]", re.M)
_TIME = r"(?:\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?)"
_RANGE = re.compile(
    rf"(?<![0-9A-Za-z_:])(?P<start>{_TIME})\s*(?:seconds?|secs?|s|秒)?\s*"
    rf"(?:to|through|until|and|[-–—~～至到])\s*"
    rf"(?P<end>{_TIME})\s*(?:seconds?|secs?|s|秒)?(?![0-9A-Za-z_:])",
    re.I,
)
_QUOTED = re.compile(
    r"「([^「」]*)」|『([^『』]*)』|“([^“”]*)”|‘([^‘’]*)’|\"([^\"\n]*)\"|'([^'\n]*)'"
)
_DIALOGUE = re.compile(r"<d>\s*\[[^\]\n]+\]\s*(.*?)</d>", re.S)
_CONTROL = re.compile(r"<(?:scenetrans|cutoff)>")
# Decimal points are not sentence boundaries; shot markers are boundaries.
_BOUNDARY = re.compile(r"\[Shot\s+\d+\]|[。！？!?；;\n]|(?<!\d)\.(?!\d)|(?<=\d)\.(?!\d)")


def _seconds(value: str) -> Decimal:
    if ":" not in value:
        return Decimal(value)
    minutes, seconds = value.split(":", 1)
    if Decimal(seconds) >= 60:
        raise ValueError("invalid dialogue time")
    return Decimal(minutes) * 60 + Decimal(seconds)


def _ranges(text: str):
    for match in _RANGE.finditer(text):
        start, end = _seconds(match.group("start")), _seconds(match.group("end"))
        if end <= start:
            raise ValueError("dialogue time range must end after it begins")
        yield match, (start, end)


def _expected(storyboard: str) -> list[tuple[tuple[Decimal, Decimal], str]]:
    expected = []
    for label in _LABEL.finditer(storyboard):
        line = storyboard[label.end() :].split("\n", 1)[0]
        quotes = list(_QUOTED.finditer(line))
        ranges = [
            item
            for item in _ranges(line)
            if not any(quote.start() < item[0].start() < quote.end() for quote in quotes)
        ]
        for index, (match, time_range) in enumerate(ranges):
            stop = ranges[index + 1][0].start() if index + 1 < len(ranges) else len(line)
            quote = _QUOTED.search(line, match.end(), stop)
            if quote is None:
                raise ValueError("timed storyboard dialogue must include quoted original words")
            words = next(value for value in quote.groups() if value is not None)
            expected.append((time_range, words))
    return expected


def _sentence_bounds(prompt: str, event: re.Match[str], previous_end: int) -> tuple[int, int]:
    before = prompt[previous_end : event.start()]
    boundaries = list(_BOUNDARY.finditer(before))
    start = previous_end + boundaries[-1].end() if boundaries else previous_end
    after = prompt[event.end() :]
    boundary = _BOUNDARY.search(after)
    end = event.end() + boundary.start() if boundary else len(prompt)
    return start, end


def validate_dialogue_windows(storyboard: str, prompt: str) -> None:
    """Reject a missing/altered local dialogue time range or its original words.

    For each timed dialogue in the shot renderer's labelled line, its H3 ``<d>``
    event must appear in order with an identical time range in that event's own
    sentence. Untimed/unlabelled inputs are intentionally left unchanged.
    """
    expected = _expected(storyboard)
    if not expected:
        return
    events = list(_DIALOGUE.finditer(prompt))
    next_event = 0
    for time_range, words in expected:
        matched = False
        while next_event < len(events):
            event = events[next_event]
            previous_end = events[next_event - 1].end() if next_event else 0
            next_event += 1
            actual = _CONTROL.sub("", event.group(1)).strip()
            if actual != words:
                continue
            start, end = _sentence_bounds(prompt, event, previous_end)
            # Exclude the spoken text itself; numeric dialogue is not a time range.
            after = prompt[event.end() : end]
            if not re.match(r"\s*[,，]?\s*(?:from\b|between\b|at\b|从|在|于)", after, re.I):
                after = ""
            surrounding = prompt[start : event.start()] + " " + after
            if any(candidate == time_range for _, candidate in _ranges(surrounding)):
                matched = True
                break
            raise ValueError("H3 dialogue is missing its explicit original time range")
        if not matched:
            raise ValueError("H3 prompt is missing timed dialogue or changed its original words")
