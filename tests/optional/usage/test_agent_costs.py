"""Tests for agent_costs.py and the shared _usage.py, on made-up transcripts in a temp folder.

    python -m unittest test_agent_costs -v
"""
import contextlib
import datetime
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
import _usage as U  # noqa: E402
import agent_costs as AC  # noqa: E402


class UsageHelpers(unittest.TestCase):
    def test_weights_and_model_multiplier(self):
        u = {"input_tokens": 10, "cache_creation_input_tokens": 8, "cache_read_input_tokens": 100,
             "output_tokens": 2}
        self.assertAlmostEqual(U.weighted(u, "claude-opus-5"), 10 + 10 + 10 + 10)
        self.assertAlmostEqual(U.weighted(u, "claude-sonnet-5"), 40 * 0.6)
        self.assertAlmostEqual(U.weighted(u, "some-new-model"), 40)
        self.assertEqual(U.weighted(None, None), 0)

    def test_since_defaults_to_days_ago_utc(self):
        now = datetime.datetime(2026, 10, 4, 1, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(U.since_value(None, 7, now), "2026-09-27")
        self.assertEqual(U.since_value("2026-09-18T16:00", 7, now), "2026-09-18T16:00")

    def test_since_compares_dates_and_timestamps_as_text(self):
        self.assertFalse("2026-09-18T05:00:00Z" < "2026-09-18")      # same day counts
        self.assertTrue("2026-09-17T23:59:59Z" < "2026-09-18")
        self.assertTrue("2026-09-18T15:59:00Z" < "2026-09-18T16:00")

    def test_config_dirs_given_or_default(self):
        self.assertEqual(U.config_dirs(["/a", "/b"]), ["/a", "/b"])
        self.assertIn(os.path.expanduser("~/.claude"), U.config_dirs(None))

    def test_agent_type_from_meta_or_path(self):
        with tempfile.TemporaryDirectory() as d:
            main = F.write(os.path.join(d, "s1.jsonl"), [])
            sub = F.write(os.path.join(d, "s1", "subagents", "a1.jsonl"), [],
                          meta={"agentType": "worker", "description": "build x"})
            bare = F.write(os.path.join(d, "s1", "subagents", "a2.jsonl"), [])
            self.assertEqual(U.agent_type(main), ("coordinator", ""))
            self.assertEqual(U.agent_type(sub), ("worker", "build x"))
            self.assertEqual(U.agent_type(bare), ("?", ""))


class AgentCosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        big = {"output_tokens": 100}                  # weight 500 on opus
        F.write(os.path.join(self.root, "s1.jsonl"), [
            F.turn("m1", "2026-09-20T00:00:00Z", usage=big),
            F.turn("m1", "2026-09-20T00:00:01Z", usage=big),      # same message id: counted once
            F.turn("m0", "2026-09-01T00:00:00Z", usage=big),      # before since: skipped
        ])
        F.write(os.path.join(self.root, "s1", "subagents", "a1.jsonl"),
                [F.turn("m2", "2026-09-21T00:00:00Z", model="claude-sonnet-5", usage=big)],
                meta={"agentType": "worker", "description": "port the tool"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_costs_dedupe_filter_and_order(self):
        rows = AC.costs(self.root, "2026-09-18")
        self.assertEqual([(round(c), at) for c, at, _ in rows], [(500, "coordinator"), (300, "worker")])

    def test_main_prints_shares(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(AC.main([self.root, "2026-09-18"]), 0)
        text = out.getvalue()
        self.assertIn("since 2026-09-18", text)
        self.assertIn(" 62.5%  coordinator", text)
        self.assertIn(" 37.5%  worker", text)
        self.assertIn("port the tool", text)


if __name__ == "__main__":
    unittest.main()
