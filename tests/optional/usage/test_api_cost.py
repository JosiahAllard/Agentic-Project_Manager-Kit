"""Tests for api_cost.py on made-up transcripts under temp config folders.

    python -m unittest test_api_cost -v
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
import api_cost as AP  # noqa: E402


class ApiCost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.acct1 = os.path.join(self.tmp.name, "acct1")
        self.acct2 = os.path.join(self.tmp.name, "acct2")
        u = {"input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000, "output_tokens": 0}
        F.write(os.path.join(self.acct1, "projects", "p", "s.jsonl"), [
            F.turn("m1", "2026-09-20T00:00:00Z", model="claude-opus-5", usage=u),
            F.turn("m1", "2026-09-20T00:00:00Z", model="claude-opus-5", usage=u),   # duplicate id
            F.turn("m2", "2026-09-20T00:00:00Z", model="mystery", usage=u),        # no price: skipped
        ])
        F.write(os.path.join(self.acct2, "projects", "q", "sub", "t.jsonl"), [
            F.turn("m3", "2026-09-20T00:00:00Z", model="claude-haiku-5", usage=u),
            F.turn("m4", "2026-09-01T00:00:00Z", model="claude-haiku-5", usage=u),  # before since
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_price_low_and_high_cache_write(self):
        paths = [os.path.join(self.acct1, "projects", "p", "s.jsonl")]
        lo, hi, toks = AP.price(paths, "2026-09-18")
        self.assertAlmostEqual(lo["opus"], 5 + 5 * 1.25)    # 1 MTok input + 1 MTok cache write
        self.assertAlmostEqual(hi["opus"], 5 + 5 * 2.0)
        self.assertEqual(toks["opus"], 2_000_000)
        self.assertNotIn("mystery", toks)

    def test_main_reads_every_config_dir_given(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            AP.main(["2026-09-18", "--config-dir", self.acct1, "--config-dir", self.acct2])
        text = out.getvalue()
        self.assertIn("read: %s, %s" % (self.acct1, self.acct2), text)
        self.assertIn("opus", text)
        self.assertIn("haiku", text)
        self.assertIn("2 transcripts", text)

    def test_a_config_dir_given_twice_is_read_once(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            AP.main(["2026-09-18", "--config-dir", self.acct1, "--config-dir", self.acct1 + os.sep])
        self.assertIn("1 transcripts", out.getvalue())


if __name__ == "__main__":
    unittest.main()
