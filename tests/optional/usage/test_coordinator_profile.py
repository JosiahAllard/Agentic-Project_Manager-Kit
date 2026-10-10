"""Tests for coordinator_profile.py on a made-up session transcript.

    python -m unittest test_coordinator_profile -v
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'usage')
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
import _fixture as F  # noqa: E402
import coordinator_profile as CP  # noqa: E402


class CoordinatorProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        F.write(os.path.join(self.root, "abcdef12-session.jsonl"), [
            F.user("2026-09-20T00:00:00Z", "owner says go"),
            F.turn("m1", "2026-09-20T00:00:01Z", usage={"input_tokens": 1000, "output_tokens": 10,
                                                         "cache_creation_input_tokens": 60000}),
            F.user("2026-09-20T00:00:02Z", [F.result("t1")]),
            F.turn("m2", "2026-09-20T00:00:03Z", usage={"cache_read_input_tokens": 70000}),
            F.turn("m2", "2026-09-20T00:00:03Z", usage={"cache_read_input_tokens": 70000}),
            F.user("2026-09-20T00:00:04Z", "<task-notification>done</task-notification>"),
            F.turn("m3", "2026-09-20T00:00:05Z", model="claude-sonnet-5",
                   usage={"cache_read_input_tokens": 80000}),
        ])
        F.write(os.path.join(self.root, "old.jsonl"), [F.turn("m0", "2026-09-01T00:00:00Z")])
        F.write(os.path.join(self.root, "abcdef12-session", "subagents", "a.jsonl"),
                [F.turn("s1", "2026-09-20T00:00:00Z")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_trigger_labels(self):
        self.assertEqual(CP.trigger("<cross-session-message>x"), "cross-session msg")
        self.assertEqual(CP.trigger("<task-notification>"), "task notice")
        self.assertEqual(CP.trigger("hello"), "owner/other text")
        self.assertEqual(CP.trigger([{"type": "tool_result"}]), "tool result")
        self.assertEqual(CP.trigger([{"type": "text"}]), "other")
        self.assertIsNone(CP.trigger(None))

    def test_profile_figures(self):
        s = CP.profile(os.path.join(self.root, "abcdef12-session.jsonl"), "2026-09-18")
        self.assertEqual(s["turns"], 3)
        self.assertEqual(s["big_writes"], 1)
        self.assertEqual(s["ctx"], [61000, 70000, 80000])
        self.assertEqual(s["triggers"], {"owner/other text": 1, "tool result": 1, "task notice": 1})
        self.assertEqual(s["models"], {"claude-opus-5": 2, "claude-sonnet-5": 1})
        self.assertIsNone(CP.profile(os.path.join(self.root, "old.jsonl"), "2026-09-18"))

    def test_main_reads_top_level_sessions_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            CP.main([self.root, "2026-09-18"])
        text = out.getvalue()
        self.assertIn("abcdef12  2026-09-20T00:00", text)
        self.assertIn("turns=3  median context=70k  max=80k", text)
        self.assertIn("cache writes >50k tokens (cache expired / compaction): 1", text)
        self.assertEqual(text.count("turns="), 1)        # old.jsonl is before since, subagents/ unread


if __name__ == "__main__":
    unittest.main()
