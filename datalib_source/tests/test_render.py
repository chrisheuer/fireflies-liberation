#!/usr/bin/env python3
"""Render one REAL transcript from the dump and assert its shape.

Stdlib unittest so it runs under the same bare python3 the DAG uses.
Run:  python3 sources/fireflies/tests/test_render.py
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from render_markdown import (BLOCK_CHARS, COLS, grid_rows_for,  # noqa: E402
                             hhmmss, iso_utc, merge_blocks, merge_turns,
                             render_markdown)

DUMP = Path.home() / "fireflies-dump" / "transcripts"


def pick(require_speech: bool = True) -> dict:
    """A real meeting off disk: the largest with speech, or the first without."""
    files = sorted(DUMP.glob("*.json"), key=lambda p: -p.stat().st_size)
    for f in files:
        t = json.loads(f.read_text())
        # Keyed on sentences, not the speakers roster -- 182 meetings have a
        # transcript with an empty roster, and they are NOT no-shows.
        has = bool(t.get("sentences"))
        if has == require_speech:
            return t
    raise unittest.SkipTest(f"no transcript with speech={require_speech} in {DUMP}")


@unittest.skipUnless(DUMP.is_dir() and any(DUMP.glob("*.json")), f"no dump at {DUMP}")
class TestRenderRealTranscript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = pick(require_speech=True)
        cls.md = render_markdown(cls.t, media=[])
        cls.lines = cls.md.splitlines()

    # --- front matter ---

    def test_front_matter_delimited(self):
        self.assertEqual(self.lines[0], "---", "must open with YAML front matter")
        self.assertIn("---", self.lines[1:40], "front matter must close")

    def test_front_matter_required_keys(self):
        head = "\n".join(self.lines[: self.lines[1:].index("---") + 1])
        for key in ("id:", "title:", "date:", "duration_min:", "organizer:",
                    "participants:", "speakers:", "source: fireflies", "no_speech:"):
            self.assertIn(key, head, f"front matter missing {key!r}")

    def test_front_matter_id_matches_source(self):
        self.assertIn(f'id: "{self.t["id"]}"', self.md)

    def test_date_is_iso_utc(self):
        m = re.search(r'^date: "([^"]+)"', self.md, re.M)
        self.assertIsNotNone(m, "no date in front matter")
        self.assertRegex(m.group(1), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

    # --- speaker turns ---

    def test_has_transcript_section(self):
        self.assertIn("## Transcript", self.md)

    def test_at_least_one_speaker_turn(self):
        turns = re.findall(r"^\*\*\[\d{2}:\d{2}:\d{2}\] .+\*\*$", self.md, re.M)
        self.assertGreater(len(turns), 0, "no speaker turns rendered")

    def test_timestamp_format_is_hhmmss(self):
        stamps = re.findall(r"^\*\*\[(\d{2}:\d{2}:\d{2})\]", self.md, re.M)
        self.assertTrue(stamps)
        for s in stamps:
            h, m, sec = (int(x) for x in s.split(":"))
            self.assertLess(m, 60, f"bad minutes in {s}")
            self.assertLess(sec, 60, f"bad seconds in {s}")

    def test_turns_merge_consecutive_same_speaker(self):
        turns = merge_turns(self.t.get("sentences") or [])
        self.assertLess(len(turns), len(self.t["sentences"]),
                        "merging should reduce sentences to fewer turns")
        for a, b in zip(turns, turns[1:]):
            self.assertNotEqual(a["speaker"], b["speaker"],
                                "adjacent turns must not share a speaker")

    # --- grid rows ---

    def test_grid_rows_meeting_plus_segments(self):
        qmd = f"fireflies/render_markdown/{self.t['id']}/all.md"
        rows = grid_rows_for(self.t, qmd, self.t["id"], len(self.md.encode()))
        meeting = [r for r in rows if r["kind"] == "Meeting"]
        segs = [r for r in rows if r["kind"] == "Meeting Segment"]
        self.assertEqual(len(meeting), 1, "exactly one meeting row")
        self.assertGreater(len(segs), 0, "expected segment rows")

    def test_segments_are_fewer_than_turns(self):
        """The whole point of blocking: far fewer, larger rows."""
        turns = merge_turns(self.t.get("sentences") or [])
        blocks = merge_blocks(turns)
        self.assertLess(len(blocks), len(turns),
                        "blocking must reduce the row count")

    def test_segment_text_carries_speaker_labels(self):
        turns = merge_turns(self.t.get("sentences") or [])
        blocks = merge_blocks(turns)
        for b in blocks[:5]:
            self.assertIn(":", b["text"], "each part is prefixed 'Speaker: text'")
            for sp in b["speakers"]:
                self.assertTrue(sp, "a block must name its speakers")

    def test_segment_size_bounded(self):
        """A block closes once it passes the threshold, so one turn can
        overshoot but a block never accretes indefinitely."""
        turns = merge_turns(self.t.get("sentences") or [])
        blocks = merge_blocks(turns)
        for b in blocks[:-1]:
            self.assertGreaterEqual(b["chars"], 0)
            longest_turn = max(len(x[1]) for x in b["parts"])
            self.assertLessEqual(b["chars"] - longest_turn, BLOCK_CHARS,
                                 "block should close at the threshold")

    def test_grid_row_identity_and_linkage(self):
        qmd = f"fireflies/render_markdown/{self.t['id']}/all.md"
        rows = grid_rows_for(self.t, qmd, self.t["id"], 1)
        tid = self.t["id"]
        self.assertEqual(rows[0]["uuid"], tid, "meeting uuid is the transcript id")
        self.assertEqual(rows[0]["conversation_uuid"], tid)
        for i, r in enumerate(rows[1:]):
            self.assertEqual(r["uuid"], f"{tid}:b{i}", "segment uuid is {id}:b{index}")
            self.assertEqual(r["conversation_uuid"], tid, "segments point at the meeting")
            self.assertEqual(r["message_index"], i)

    def test_every_row_carries_required_non_null_columns(self):
        rows = grid_rows_for(self.t, "p", self.t["id"], 1)
        for r in rows:
            for col in ("uuid", "provider", "kind", "source_label",
                        "conversation_uuid", "entire_chat", "text"):
                self.assertIsNotNone(r.get(col), f"{col} is NOT NULL in the DDL")
            self.assertTrue(set(r).issubset(set(COLS) | {"when_ts"}),
                            f"row has columns not in the DDL: {set(r) - set(COLS)}")

    def test_qmd_path_matches_md_path_exactly(self):
        """A mismatch drops the row from free-text search, silently."""
        qmd = f"fireflies/render_markdown/{self.t['id']}/all.md"
        rows = grid_rows_for(self.t, qmd, self.t["id"], 1)
        for r in rows:
            self.assertEqual(r["qmd_path"], qmd)


@unittest.skipUnless(DUMP.is_dir() and any(DUMP.glob("*.json")), f"no dump at {DUMP}")
class TestNoShowMeeting(unittest.TestCase):
    """14% of the archive is a meeting nobody spoke in. Not a failure."""

    def test_no_speech_renders_and_emits_meeting_row_only(self):
        try:
            t = pick(require_speech=False)
        except unittest.SkipTest:
            self.skipTest("no no-show meeting on disk yet")
        md = render_markdown(t, media=[])
        self.assertIn("no_speech: true", md)
        self.assertIn("No speech was recorded", md)
        rows = grid_rows_for(t, "p", t["id"], len(md.encode()))
        self.assertEqual(len(rows), 1, "a no-show emits the meeting row and nothing else")
        self.assertEqual(rows[0]["kind"], "Meeting")


class TestHelpers(unittest.TestCase):
    def test_hhmmss_rolls_over(self):
        self.assertEqual(hhmmss(0), "00:00:00")
        self.assertEqual(hhmmss(61), "00:01:01")
        self.assertEqual(hhmmss(3661), "01:01:01")
        self.assertEqual(hhmmss(None), "00:00:00")

    def test_iso_utc_shape(self):
        self.assertRegex(iso_utc(1788631200000),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
