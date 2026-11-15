"""Behavioral tests for the deterministic Markdown-to-H3 adapter."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))

from pydantic import ValidationError  # noqa: E402

from h3_client import ReferenceImage  # noqa: E402
from shot_client import (  # noqa: E402
    DialogueEvent,
    ShotClient,
    ShotError,
    ShotRow,
    ShotTable,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
HEADER = "| 镜号 | 时间 | 参考图 | 景别 | 机位/角度 | 运镜 | 动作调度 | 情绪/潜台词 | 台词 | 台词与肢体的关系 | 道具/场景层次 | 衔接 | 导演备注 |"
SEPARATOR = "| " + " | ".join(["---"] * 13) + " |"
ROW1 = "| 1 | 0–3秒 | 图1、图2 | 中景 | 平视正面 | 固定 | 女孩用左手推门 | 平静 | 1–2秒 女孩（普通话）：「今天不打开。」 | 身体自然 | 门在右侧 | 开场 | 保留空间关系 |"
ROW2 = "| 2 | 3–6秒 | 图1、图2 | 中景 | 平视正面 | 固定 | 女孩保持姿势 | 平静 | 无 | 无 | 门在右侧 | 硬切 | 视线延续 |"
TABLE = "\n".join(("导演概述：女孩推门。", "", HEADER, SEPARATOR, ROW1, ROW2))
SOURCE = "女孩用左手推门说：‘今天不打开。’"


class ShotClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = [Path(self.temp.name) / "girl.png", Path(self.temp.name) / "door.png"]
        for index, path in enumerate(self.paths):
            path.write_bytes(PNG + bytes([index]))
        self.images = [
            ReferenceImage(self.paths[0], name="女孩", remote_name="codex/girl.png"),
            ReferenceImage(self.paths[1], name="门", remote_name="codex/door.png"),
        ]

    def process(self, table: str = TABLE, source: str = SOURCE):
        return ShotClient().process(
            table,
            type="ref2va",
            source_text=source,
            reference_images=self.images,
            duration_seconds=6,
        )

    def test_ready_export_is_deterministic_and_has_only_h3_fields(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("openai.OpenAI", side_effect=AssertionError),
        ):
            result = self.process()
        self.assertEqual(result.status, "ready", result.issues)
        request = result.to_h3_request(values={"seed": 42})
        self.assertEqual(request, result.to_h3_request(values={"seed": 42}))
        self.assertEqual(set(request), {"type", "storyboard", "reference_images", "values"})
        self.assertEqual(request["values"], {"seed": 42, "length": 6})
        self.assertEqual(request["storyboard"].count("今天不打开。"), 1)
        self.assertIn("左手", request["storyboard"])
        for index, item in enumerate(request["reference_images"]):
            self.assertEqual(Path(item["path"]), self.paths[index].resolve())
        for values in ({"length": 6}, {"length": 7}, {"unknown": 2}):
            with self.subTest(values=values), self.assertRaises((ShotError, ValueError)):
                result.to_h3_request(values=values)

    def test_bad_tables_are_invalid_and_cannot_export(self) -> None:
        cases = {
            "missing_column": TABLE.replace("情绪/潜台词", "不支持的列"),
            "duplicate_id": TABLE.replace("| 2 | 3–6秒", "| 1 | 3–6秒"),
            "gap": TABLE.replace("3–6秒", "3.2–6秒"),
            "overlap": TABLE.replace("3–6秒", "2.8–6秒"),
            "beyond_duration": TABLE.replace("3–6秒", "3–6.1秒"),
            "bad_reference": TABLE.replace("图1、图2", "图1、图3"),
            "missing_dialogue": TABLE.replace("1–2秒 女孩（普通话）：「今天不打开。」", "无"),
            "changed_dialogue": TABLE.replace("今天不打开。", "今天要打开。"),
            "repeated_dialogue": TABLE.replace(
                "| 无 | 无 | 门在右侧", "| 4–5秒 女孩（普通话）：「今天不打开。」 | 无 | 门在右侧"
            ),
        }
        for name, table in cases.items():
            with self.subTest(case=name):
                result = self.process(table)
                self.assertEqual(result.status, "invalid", name)
                self.assertTrue(result.issues, name)
                with self.assertRaises((ShotError, ValueError)):
                    result.to_h3_request()

    def test_cross_shot_dialogue_is_contiguous_and_verbatim(self) -> None:
        first = ROW1.replace(
            "1–2秒 女孩（普通话）：「今天不打开。」",
            "D1 1/2 2.8–3秒 女孩（普通话）：「先看这里，」",
        )
        second = ROW2.replace(
            "| 无 | 无 | 门在右侧",
            "| D1 2/2 3–4.1秒 女孩（普通话）：「今天不打开。」 | 无 | 门在右侧",
        )
        table = "\n".join((HEADER, SEPARATOR, first, second))
        source = "女孩用左手推门说：‘先看这里，今天不打开。’"
        result = self.process(table, source)
        self.assertEqual(result.status, "ready", result.issues)
        storyboard = result.to_h3_request()["storyboard"]
        self.assertEqual(storyboard.count("先看这里，"), 1)
        self.assertEqual(storyboard.count("今天不打开。"), 1)
        self.assertLess(storyboard.index("先看这里，"), storyboard.index("今天不打开。"))
        for broken in (
            table.replace("3–4.1秒", "3.1–4.1秒"),
            table.replace("D1 2/2", "D1 1/2"),
            table.replace("今天不打开。", "今天要打开。"),
            table.replace("3–4.1秒 女孩", "3–4.1秒 男人"),
        ):
            self.assertEqual(self.process(broken, source).status, "invalid")

    def test_escaped_pipe_is_preserved_as_cell_content(self) -> None:
        table = TABLE.replace("保留空间关系", r"保留左\|右关系")
        result = self.process(table)
        self.assertEqual(result.status, "ready", result.issues)
        self.assertIn("左|右关系", result.to_h3_request()["storyboard"])

    def test_dialogue_without_subtime_does_not_invent_one(self) -> None:
        table = TABLE.replace("1–2秒 女孩（普通话）", "女孩（普通话）")
        result = self.process(table)
        self.assertEqual(result.status, "ready", result.issues)
        storyboard = result.to_h3_request()["storyboard"]
        self.assertIn("今天不打开。", storyboard)
        self.assertNotIn("1–2秒", storyboard)

    def test_bracketed_speaker_time_is_accepted(self) -> None:
        table = TABLE.replace("1–2秒 女孩（普通话）", "girl[1-2]")
        result = self.process(table)
        self.assertEqual(result.status, "ready", result.issues)
        self.assertIn("今天不打开。", result.to_h3_request()["storyboard"])

    def test_first_frame_opening_can_mention_total_duration(self) -> None:
        row = (
            ROW1.replace("0–3秒", "0–6秒")
            .replace("图1、图2", "图1")
            .replace("| 开场 |", "| 开场：图1即真实首帧；单镜无剪切，持续至6秒 |")
        )
        table = "\n".join((HEADER, SEPARATOR, row))
        result = ShotClient().process(
            table,
            type="i2va",
            source_text=SOURCE,
            reference_images=self.images[:1],
            duration_seconds=6,
        )
        self.assertEqual(result.status, "ready", result.issues)
        self.assertIn("持续至6秒", result.to_h3_request()["storyboard"])

    def test_transition_anchors_distinguish_previous_end_and_current_start(self) -> None:
        first = ROW1.replace("0–3秒", "0–5秒")
        second = ROW2.replace("3–6秒", "5–6秒").replace(
            "| 硬切 |", "| 前镜5秒结束；5秒开始叠化，持续0.5秒，5.5秒完成 |"
        )
        table = "\n".join((HEADER, SEPARATOR, first, second))
        source = SOURCE + "使用叠化。"
        result = self.process(table, source)
        self.assertEqual(result.status, "ready", result.issues)
        for broken in (
            table.replace("5秒开始叠化", "6秒开始叠化"),
            table.replace("5.5秒完成", "5.6秒完成"),
        ):
            with self.subTest(table=broken):
                invalid = self.process(broken, source)
                self.assertEqual(invalid.status, "invalid")
                with self.assertRaises((ShotError, ValueError)):
                    invalid.to_h3_request()

    def test_own_end_time_and_actor_end_time_are_not_transition_ends(self) -> None:
        """A hard cut has no duration and an actor's beat is not a transition."""
        for continuity in ("硬切；本镜6秒结束", "硬切进入（本镜在6秒结束）"):
            with self.subTest(continuity=continuity):
                table = "\n".join(
                    (HEADER, SEPARATOR, ROW1, ROW2.replace("| 硬切 |", f"| {continuity} |"))
                )
                result = self.process(table)
                self.assertEqual(result.status, "ready", result.issues)
        dissolve = "\n".join(
            (
                HEADER,
                SEPARATOR,
                ROW1,
                ROW2.replace("| 硬切 |", "| 叠化0.5秒；女孩4.1秒完成转身 |"),
            )
        )
        result = self.process(dissolve, SOURCE + "使用叠化。")
        self.assertEqual(result.status, "ready", result.issues)

    def test_negated_hard_cut_is_not_a_contradiction(self) -> None:
        for continuity in ("不用硬切，改用叠化0.5秒", "叠化0.5秒，非硬切"):
            with self.subTest(continuity=continuity):
                table = "\n".join(
                    (HEADER, SEPARATOR, ROW1, ROW2.replace("| 硬切 |", f"| {continuity} |"))
                )
                result = self.process(table, SOURCE + "使用叠化。")
                self.assertEqual(result.status, "ready", result.issues)

    def test_source_entering_frame_and_out_point_are_not_cut_times(self) -> None:
        for extra in ("女孩在2秒切入画面。", "6秒硬切出。"):
            with self.subTest(source=extra):
                result = self.process(TABLE, SOURCE + extra)
                self.assertEqual(result.status, "ready", result.issues)

    def test_split_sentence_note_is_announced_once(self) -> None:
        first = ROW1.replace(
            "1–2秒 女孩（普通话）：「今天不打开。」",
            "D1 1/2 2.8–3秒 女孩（普通话）：「先看这里，」",
        )
        second = ROW2.replace(
            "| 无 | 无 | 门在右侧",
            "| D1 2/2 3–4.1秒 女孩（普通话）：「今天不打开。」 | 无 | 门在右侧",
        )
        table = "\n".join((HEADER, SEPARATOR, first, second))
        result = self.process(table, "女孩用左手推门说：‘先看这里，今天不打开。’")
        self.assertEqual(result.status, "ready", result.issues)
        storyboard = result.to_h3_request()["storyboard"]
        self.assertEqual(storyboard.count("的各段是同一句话"), 1)

    def test_in_cell_line_breaks_render_as_separators(self) -> None:
        table = TABLE.replace("女孩用左手推门", "女孩用左手推门<br>停顿一拍")
        result = self.process(table)
        self.assertEqual(result.status, "ready", result.issues)
        storyboard = result.to_h3_request()["storyboard"]
        self.assertIn("动作调度：女孩用左手推门；停顿一拍", storyboard)
        self.assertNotIn("<br>", storyboard)
        # A cell that already ends a sentence keeps its own punctuation.
        self.assertNotIn("。」。", storyboard)

    def test_i2va_rows_must_reference_the_first_frame(self) -> None:
        row = ROW1.replace("0–3秒", "0–6秒").replace("图1、图2", "无")
        table = "\n".join((HEADER, SEPARATOR, row))
        result = ShotClient().process(
            table,
            type="i2va",
            source_text=SOURCE,
            reference_images=self.images[:1],
            duration_seconds=6,
        )
        self.assertEqual(result.status, "invalid")

    def test_static_camera_negated_motion_list_stops_at_positive_motion(self) -> None:
        still = (
            "None. Static frame for the whole 8 seconds — no pan, tilt, dolly, zoom or handheld."
        )
        for movement, expected in (
            (still, "ready"),
            ("Static frame; no pan, but tilt allowed.", "invalid"),
            ("Static frame; no pan, zoom allowed.", "invalid"),
            ("Static frame; no pan, then dolly.", "invalid"),
            ("Static frame; no pan, tilt, but zoom.", "invalid"),
        ):
            with self.subTest(movement=movement):
                result = self.process(
                    TABLE.replace("| 平视正面 | 固定 |", f"| 平视正面 | {movement} |", 1)
                )
                self.assertEqual(result.status, expected, result.issues)

    def test_schema_rejects_inconsistent_rows_and_events(self) -> None:
        result = self.process()
        self.assertEqual(result.status, "ready", result.issues)
        row = result.table.rows[0]
        with self.assertRaises(ValidationError):
            ShotRow(**{**row.model_dump(), "end_ms": row.start_ms})
        with self.assertRaises(ValidationError):
            ShotTable(**{**result.table.model_dump(), "duration_ms": 9000})
        with self.assertRaises(ValidationError):
            DialogueEvent(speaker="女孩", text="今天不打开。", start_ms=100)
        with self.assertRaises(ValidationError):
            DialogueEvent(speaker="女孩", text="今天不打开。", part_index=1)

    def test_english_three_shot_table_preserves_chinese_dialogue(self) -> None:
        rows = (
            "| 1 | 0–2.6s | <Picture 1>, <Picture 2> | Medium shot | Eye level | Static | Girl keeps her arms down. | Calm | No dialogue | No speech. | Girl left; bottle right. | Opening | No extra action. |",
            "| 2 | 2.6–5s | <Picture 1>, <Picture 2> | Medium close-up | Eye level | Static | Girl raises her left arm. | Quiet resolve | 3s–4s Girl (Mandarin): “今天不打开。” | Holds her arm during speech. | Bottle stays on table. | Hard cut at 2.6s. | No added lines. |",
            "| 3 | 5–8s | <Picture 1>, <Picture 2> | Medium | Eye level | Static | Girl lowers her arm. | Calm | None | No dialogue. | Opening composition. | At 5s, begin a cross-dissolve lasting 0.5s; the transition ends at 5.5s. | No extra action. |",
        )
        source = "The girl says in a calm voice: “今天不打开。” Use a cross-dissolve at 5s."

        def process(table_rows: tuple[str, ...], text: str = source):
            return ShotClient().process(
                "\n".join((HEADER, SEPARATOR, *table_rows)),
                type="ref2va",
                source_text=text,
                reference_images=self.images,
                duration_seconds=8,
                preferences={"shot_count": 3},
            )

        result = process(rows)
        self.assertEqual(result.status, "ready", result.issues)
        shots = result.table.rows
        self.assertEqual(
            [(s.start_ms, s.end_ms) for s in shots], [(0, 2600), (2600, 5000), (5000, 8000)]
        )
        self.assertEqual([s.reference_images for s in shots], [[1, 2], [1, 2], [1, 2]])
        self.assertEqual(
            (shots[2].transition, shots[2].transition_duration_ms), ("cross_dissolve", 500)
        )
        event = shots[1].dialogue_events[0]
        self.assertEqual(
            (event.speaker, event.language, event.text), ("Girl", "mandarin", "今天不打开。")
        )
        storyboard = result.to_h3_request()["storyboard"]
        self.assertIn("Girl raises her left arm", storyboard)
        self.assertEqual(storyboard.count("今天不打开。"), 1)

        for old, new in (
            ("Hard cut at 2.6s", "Hard cut at 2.7s"),
            ("At 5s, begin", "At 6s, begin"),
            ("ends at 5.5s", "ends at 5.6s"),
        ):
            with self.subTest(old=old, new=new):
                changed = tuple(row.replace(old, new) for row in rows)
                self.assertEqual(process(changed).status, "invalid")
        self.assertEqual(process(rows, source.replace("at 5s", "at 6s")).status, "invalid")
        self.assertEqual(process(rows, source.replace("says", "does not say")).status, "invalid")

        # A preceding shot duration must not be mistaken for the dissolve duration.
        prior_duration = tuple(
            row.replace("At 5s, begin", "Previous shot duration 2s; at 5s, begin") for row in rows
        )
        prior_result = process(prior_duration)
        self.assertEqual(prior_result.status, "ready", prior_result.issues)
