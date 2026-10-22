"""Tests for testing_cost.py (the testing-share report) on made-up transcripts.

    python -m unittest test_testing_cost -v
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
import testing_cost as TC  # noqa: E402

OUT = {"output_tokens": 100}      # weight 500 on opus


class Classifier(unittest.TestCase):
    def test_build_and_test_drivers_count(self):
        rx = TC.command_regex()
        for cmd in ("dotnet test x.sln", "./gradlew :core:test", "python tools/gate.py",
                    "godot --headless --script t.gd", "python -m unittest discover -s tools",
                    "pytest -q", "cargo test", "npm test", "powershell -File tools\\e2e.ps1"):
            self.assertTrue(TC.is_test_command(cmd, rx), cmd)

    def test_readers_and_other_commands_do_not(self):
        rx = TC.command_regex()
        for cmd in ("grep -r dotnet test .", "git log", "cat tools/gate.py", "python tools/board.py"):
            self.assertFalse(TC.is_test_command(cmd, rx), cmd)

    def test_extra_pattern(self):
        self.assertFalse(TC.is_test_command("make check-all", TC.command_regex()))
        self.assertTrue(TC.is_test_command("make check-all", TC.command_regex(r"make\s+check")))


class Tally(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        F.write(os.path.join(self.root, "s.jsonl"), [
            F.turn("m1", "2026-09-20T00:00:00Z", usage=OUT, content=[F.shell("t1", "dotnet test")]),
            F.user("2026-09-20T00:00:01Z", [F.result("t1")]),
            F.turn("m2", "2026-09-20T00:00:02Z", usage=OUT),      # reads the test result: testing
            F.turn("m3", "2026-09-20T00:00:03Z", usage=OUT),      # ordinary turn
            F.turn("m4", "2026-09-01T00:00:00Z", usage=OUT, content=[F.shell("t0", "dotnet test")]),
        ])
        F.write(os.path.join(self.root, "s", "subagents", "a.jsonl"),
                [F.turn("r1", "2026-09-20T00:00:00Z", usage=OUT)],
                meta={"agentType": "test-runner", "description": "run tier 1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_tally(self):
        tot, agents = TC.tally(TC.U.transcripts(self.root), "2026-09-18", TC.command_regex())
        self.assertEqual(tot["coordinator"]["turns"], 3)
        self.assertEqual(tot["coordinator"]["test_turns"], 2)
        self.assertAlmostEqual(tot["coordinator"]["testing_cost"], 1000)
        self.assertEqual(tot["test-runner"]["test_turns"], 1)
        self.assertEqual(sorted(a[3] for a in agents), ["coordinator", "test-runner"])

    def test_two_turns_with_no_message_id_are_both_counted(self):
        root = os.path.join(self.root, "noid")
        F.write(os.path.join(root, "s.jsonl"), [
            F.turn(None, "2026-09-20T00:00:00Z", usage=OUT),
            F.turn(None, "2026-09-20T00:00:01Z", usage=OUT),
        ])
        tot, _ = TC.tally(TC.U.transcripts(root), "2026-09-18", TC.command_regex())
        self.assertEqual(tot["coordinator"]["turns"], 2)

    def test_main_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            TC.main([self.root, "2026-09-18"])
        text = out.getvalue()
        self.assertIn("files=2 since=2026-09-18", text)
        self.assertIn("TOTAL test share of weighted cost: 75.0%", text)
        self.assertIn("run tier 1", text)


if __name__ == "__main__":
    unittest.main()
