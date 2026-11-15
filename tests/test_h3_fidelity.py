"""Regression tests for explicitly timed storyboard dialogue."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from h3_client.fidelity import validate_dialogue_windows  # noqa: E402

LABEL = "台词（仅此栏为实际对白，保持原话与标点）："


class DialogueWindowTests(unittest.TestCase):
    def test_observed_missing_end_is_rejected_even_when_nearby_action_has_end(self) -> None:
        source = LABEL + "3.0–4.6s Girl (Mandarin):「今天不打开。」"
        prompt = (
            "[Shot 2] At 00:03.000, the camera cuts to the girl. "
            "At 3.0 seconds, <Subject 1> (S1) says, <d>[Chinese] 今天不打开。</d> "
            "A small settle occurs at 4.6 seconds."
        )
        with self.assertRaises(ValueError):
            validate_dialogue_windows(source, prompt)

    def test_full_english_or_chinese_ranges_match_by_value(self) -> None:
        source = LABEL + "3.0–4.6s Girl (Mandarin):「今天不打开。」"
        for text in (
            "From 3.0 to 4.6 seconds, (S1) says, <d>[Chinese] 今天不打开。</d>",
            "从3秒到4.600秒，(S1)说<d>[Chinese] 今天不打开。</d>",
            "From 00:03.000 to 00:04.600, (S1) says <d>[Chinese] 今天不打开。</d>",
            "(S1) says <d>[Chinese] 今天不打开。</d> from 3.0 to 4.6 seconds.",
        ):
            with self.subTest(text=text):
                validate_dialogue_windows(source, text)

    def test_time_reference_in_another_sentence_is_not_sufficient(self) -> None:
        source = LABEL + "3秒至4.6秒 女孩：‘今天不打开。’"
        for prompt in (
            "From 3 to 4.6 seconds, the camera pushes in. (S1) says <d>[Chinese] 今天不打开。</d>",
            "(S1) says <d>[Chinese] 今天不打开。</d> The camera settles from 3 to 4.6 seconds.",
            "From 3 to 4.7 seconds, (S1) says <d>[Chinese] 今天不打开。</d>",
        ):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                validate_dialogue_windows(source, prompt)

    def test_split_dialogue_preserves_both_ranges_order_and_control_tokens(self) -> None:
        source = "\n".join(
            (
                LABEL + "D1 1/2 2.8–3秒 女孩（普通话）：「先看这里，」",
                LABEL + "D1 2/2 3–4.1秒 女孩（普通话）：「今天不打开。」",
            )
        )
        first = "From 2.8 to 3 seconds, (S1) says <d>[Chinese] 先看这里，<scenetrans></d>"
        second = "From 3 to 4.1 seconds, (S1) continues <d>[Chinese] <scenetrans>今天不打开。</d>"
        validate_dialogue_windows(source, first + ". [Shot 2] " + second)
        with self.assertRaises(ValueError):
            validate_dialogue_windows(source, second + ". [Shot 2] " + first)
        with self.assertRaises(ValueError):
            validate_dialogue_windows(source, first + ". [Shot 2] " + second.replace("4.1", "4.2"))

    def test_each_of_two_lines_on_one_field_requires_its_own_range(self) -> None:
        source = LABEL + '1–2秒 女孩:「你好！」；3–4秒 女孩: "再见。"'
        prompt = (
            "From 1 to 2 seconds, (S1) says <d>[Chinese] 你好！</d>. "
            "From 3 to 4 seconds, (S1) says <d>[Chinese] 再见。</d>"
        )
        validate_dialogue_windows(source, prompt)
        with self.assertRaises(ValueError):
            validate_dialogue_windows(source, prompt.replace("3 to 4", "3 to 5"))

    def test_no_structured_timed_dialogue_does_not_change_old_behavior(self) -> None:
        for source in (
            "女子在3到4.6秒说：今天不打开。",
            LABEL + "女孩（普通话）：「今天不打开。」",
            LABEL + "无。",
        ):
            with self.subTest(source=source):
                validate_dialogue_windows(source, "no compiled dialogue or time")

    def test_unquoted_timed_dialogue_is_not_silently_skipped(self) -> None:
        with self.assertRaises(ValueError):
            validate_dialogue_windows(LABEL + "3–4秒 女孩说今天不打开。", "")

    def test_a_range_spoken_as_words_is_not_an_extra_event(self) -> None:
        source = LABEL + "3–4秒 女孩:「等3–4秒。」"
        prompt = "From 3 to 4 seconds, (S1) says <d>[Chinese] 等3–4秒。</d>"
        validate_dialogue_windows(source, prompt)


if __name__ == "__main__":
    unittest.main()
