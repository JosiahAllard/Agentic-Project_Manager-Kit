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
            F.turn("m3", "2026-09-20T00:00:00Z", model="claude-haiku-5-5", usage=u),
            F.turn("m4", "2026-09-01T00:00:00Z", model="claude-haiku-5-5", usage=u),  # before since
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_price_low_and_high_cache_write(self):
        paths = [os.path.join(self.acct1, "projects", "p", "s.jsonl")]
        lo, hi, toks, unpriced = AP.price(paths, "2026-09-18")
        self.assertAlmostEqual(lo["opus"], 5 + 5 * 1.25)    # 1 MTok input + 1 MTok cache write
        self.assertAlmostEqual(hi["opus"], 5 + 5 * 2.0)
        self.assertEqual(toks["opus"], 2_000_000)
        self.assertNotIn("mystery", toks)
        self.assertEqual(dict(unpriced), {"mystery": 2_000_000})

    def test_main_prints_the_tokens_it_could_not_price(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            AP.main(["2026-09-18", "--config-dir", self.acct1])
        self.assertIn("no list price (left out of the totals): 2.0M tokens, mystery 2000000\n",
                      out.getvalue())

    def test_a_synthetic_turn_is_skipped_not_reported_unpriced(self):
        acct = os.path.join(self.tmp.name, "acct3")
        zero = {"input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
        path = os.path.join(acct, "projects", "r", "u.jsonl")
        F.write(path, [F.turn("m5", "2026-09-20T00:00:00Z", model="<synthetic>", usage=zero)])
        lo, hi, toks, unpriced = AP.price([path], "2026-09-18")
        self.assertEqual(dict(unpriced), {})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            AP.main(["2026-09-18", "--config-dir", acct])
        self.assertNotIn("no list price", out.getvalue())

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


class Rates(unittest.TestCase):
    def test_each_model_has_its_own_list_price(self):
        for model, want in (("claude-opus-5-5", (4.0, 20.0, 0.20)), ("claude-opus-5", (5.0, 25.0, 0.50)),
                            ("claude-opus-5-5[1m]", (4.0, 20.0, 0.20)), ("claude-sonnet-5-5", (2.0, 10.0, 0.20)),
                            ("claude-sonnet-4-6", (3.0, 15.0, 0.30)), ("claude-fable-5-1", (10.0, 50.0, 0.25)),
                            ("claude-haiku-4-5-20251001", (1.0, 5.0, 0.10)),
                            ("claude-haiku-5-5", (0.10, 0.50, 0.01)),
                            ("claude-opus-4-5-20251101", (5.0, 25.0, 0.50)),
                            ("claude-sonnet-4-5-20250929", (3.0, 15.0, 0.30))):
            with self.subTest(model):
                self.assertEqual(AP.rates(model), want)

    def test_a_model_the_table_does_not_name_has_no_price(self):
        for model in ("mystery", "claude-opus-5-6", "claude-opus-4-1", "claude-haiku-5", None):
            with self.subTest(model):
                self.assertIsNone(AP.rates(model))
                self.assertIsNone(AP.turn_usd(model, {"input_tokens": 10}))

    def test_a_long_haiku_prompt_takes_the_long_rates(self):
        self.assertEqual(AP.rates("claude-haiku-5-5", 100_000), (0.10, 0.50, 0.01))
        self.assertEqual(AP.rates("claude-haiku-5-5", 100_001), (0.50, 2.50, 0.05))
        u = {"input_tokens": 1_000, "cache_read_input_tokens": 200_000, "output_tokens": 1_000}
        self.assertAlmostEqual(AP.turn_usd("claude-haiku-5-5", u), (1_000 * 0.5 + 200_000 * 0.05 + 1_000 * 2.5) / 1e6)

    def test_a_split_cache_write_is_priced_at_each_ttl(self):
        u = {"cache_creation_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 400_000, "ephemeral_1h_input_tokens": 600_000}}
        for unsplit in (AP.WRITE_5M, AP.WRITE_1H):
            self.assertAlmostEqual(AP.turn_usd("claude-opus-5", u, unsplit), (0.4 * 1.25 + 0.6 * 2.0) * 5)
        self.assertAlmostEqual(AP.turn_usd("claude-opus-5-5", {"cache_read_input_tokens": 1_000_000}), 0.20)


if __name__ == "__main__":
    unittest.main()
