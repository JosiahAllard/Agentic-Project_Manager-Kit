"""The read-only start parts of the coordinator header (kitlib.coordinatorstart.parts)."""
import json
import os
import tempfile
import threading
import time
import unittest

from kitlib import coordinatorstart
from kitlib.coordinatorstart import parts

NAME = "alpha-coordinator-a2-n6"
CLASSIFY = (
    "classify  3 worktree(s) besides the main checkout\n"
    "A landed       0  tip in the base\n"
    "C half-done    1  dirty\n"
    "  C  D:\\src\\alpha\\w\\one  kit/one  tip abc\n"
    "L live/kept    1  locked\n"
    "  L  D:\\src\\alpha\\w\\two  kit/two  tip def  active\n")
NEXT = (
    "BACKLOG empty | 3 records | 2 pullable\n\n"
    "NEXT (1 offered, session k)\n"
    "  1  EX-ONE  p2  open  alpha  do it\n"
    "  passed over EX-TWO (rank 2): collides\n"
    "  blockers, most first:\n"
    "    blocks 1: x\n")


def note(temp, folder, name, text):
    d = os.path.join(temp, folder)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "state-%s.md" % name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


class StateNoteTest(unittest.TestCase):
    def test_exact_name_in_any_coordinator_folder_with_its_last_ten_lines(self):
        with tempfile.TemporaryDirectory() as t:
            note(t, "other-coordinator", "newer", "elsewhere\n")
            note(t, "kit-coordinator", NAME, "".join("line %d\n" % i for i in range(30)))
            out = parts.state_note({"AGENT_TEMP": t}, NAME, [])
        self.assertIn("state note: ", out[0])
        self.assertNotIn("guessed", out[0])
        self.assertTrue(out[0].replace("\\", "/").endswith("kit-coordinator/state-%s.md (last 10 lines)" % NAME))
        self.assertEqual([l[4:] for l in out[1:]], ["line %d" % i for i in range(20, 30)])

    def test_no_name_takes_the_newest_note_of_the_repo_folder_and_says_guessed(self):
        with tempfile.TemporaryDirectory() as t:
            old = note(t, "alpha-coordinator", "a", "old\n")
            os.utime(old, (1, 1))
            note(t, "alpha-coordinator", "b", "new\n")
            note(t, "beta-coordinator", "c", "not ours\n")
            out = parts.state_note({"AGENT_TEMP": t}, None, ["alpha"])
        self.assertIn("state note (newest, guessed): ", out[0])
        self.assertIn("state-b.md", out[0])
        self.assertEqual(out[1], "  | new")

    def test_none_found_is_one_line(self):
        with tempfile.TemporaryDirectory() as t:
            out = parts.state_note({"AGENT_TEMP": t}, NAME, ["alpha"])
        self.assertEqual(len(out), 1)
        self.assertIn("state note: none found for " + NAME, out[0])

    def test_a_long_note_is_read_from_its_end_and_capped(self):
        with tempfile.TemporaryDirectory() as t:
            note(t, "x-coordinator", NAME, ("y" * 190 + "\n") * 60)
            out = parts.state_note({"AGENT_TEMP": t}, NAME, [])
        self.assertLessEqual(sum(len(l) + 1 for l in out), parts.CAP + 250)
        self.assertIn("more lines", out[-1])


class CommandPartsTest(unittest.TestCase):
    def test_hub_pending_names_the_key_and_counts(self):
        seen = []

        def run(args, timeout):
            seen.append((args[2:], timeout))
            return "question EX-1 [r] to=pm from=k t\nto-do EX-2 [r] to=k from=pm t\n"
        out = parts.hub_pending("k", run)
        self.assertEqual(seen, [(["pending", "--for", "k"], parts.PART_TIMEOUT)])
        self.assertEqual(out[0], "hub pending for k: 2")
        self.assertEqual(parts.hub_pending("k", lambda a, timeout: "")[0], "hub pending for k: none")
        with self.assertRaises(KeyError):
            parts.hub_pending(None, run)

    def test_next_keeps_the_offer_and_drops_what_it_passed_over(self):
        out = parts.backlog_next("R", "k", lambda a, timeout: NEXT)
        self.assertEqual(out[0], "backlog next:")
        self.assertTrue(any("EX-ONE" in l for l in out))
        self.assertFalse(any("passed over" in l or "blockers" in l for l in out))

    def test_next_asks_the_pm_data_worktree_when_the_repo_holds_no_record(self):
        asked = []

        def run(args, timeout):
            asked.append(args[args.index("-C") + 1])
            return NEXT if asked[-1] == "PM" else "BACKLOG empty | 0 records | 0 pullable\n"
        out = parts.backlog_next("R", "k", run, store=lambda: "PM")
        self.assertEqual(asked, ["R", "PM"])
        self.assertTrue(any("EX-ONE" in l for l in out))

    def test_worktrees_keeps_the_class_lines_and_the_stale_rows_not_the_live_rows(self):
        text = "\n".join(parts.worktrees("R", lambda a, timeout: CLASSIFY))
        self.assertIn("C half-done", text)
        self.assertIn("kit/one", text)
        self.assertIn("A landed", text)
        self.assertNotIn("kit/two", text)
        self.assertNotIn("classify  3", text)

    def test_classify_exit_one_with_output_is_read_but_exit_one_without_is_an_error(self):
        class R:
            def __init__(self, rc, out):
                self.returncode, self.stdout, self.stderr = rc, out, "boom"
        orig = parts.gitcmd.subprocess_run
        try:
            parts.gitcmd.subprocess_run = lambda a, **kw: R(1, CLASSIFY)
            self.assertIn("C half-done", parts.run_loose(["x"]))
            parts.gitcmd.subprocess_run = lambda a, **kw: R(1, "")
            with self.assertRaises(RuntimeError):
                parts.run_loose(["x"])
        finally:
            parts.gitcmd.subprocess_run = orig


class RunAllTest(unittest.TestCase):
    def test_a_late_part_is_one_line_and_does_not_hold_the_others(self):
        gate = threading.Event()
        t0 = time.monotonic()
        out = parts.run_all([("slow", lambda: gate.wait(5) and ["late"]), ("fast", lambda: ["ok"])], deadline=0.3)
        gate.set()
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(out[1], ("fast", ["ok"]))
        self.assertEqual(out[0][1], ["could not read slow: no answer within 0 s"])

    def test_a_raise_is_one_line_and_the_rest_still_run(self):
        def boom():
            raise ValueError("bad")

        def key():
            raise KeyError("no key")
        out = dict(parts.run_all([("a", boom), ("b", key), ("c", lambda: ["ok"])]))
        self.assertEqual(out["a"], ["could not read a: ValueError: bad"])
        self.assertEqual(out["b"], ["could not read b: no key"])
        self.assertEqual(out["c"], ["ok"])


class ContextPartsTest(unittest.TestCase):
    def test_context_carries_all_four_parts_and_a_hanging_one_does_not_stop_the_start(self):
        entry = {"repo": "alpha", "agent": True, "stem": "alpha-coordinator", "name": NAME}
        files = {"S.json": json.dumps({"kitc": entry})}

        def read(p):
            if p in files:
                return files[p]
            raise FileNotFoundError(p)
        release = threading.Event()

        def run(args, timeout=10):
            tool = os.path.basename(args[1])
            if tool == "machine_lock.py":
                return json.dumps({"schema_version": 1, "holds": [], "slots": {}})
            if tool == "hub.py":
                release.wait(5)           # hangs past the deadline
                return ""
            return NEXT if tool == "backlog.py" else CLASSIFY
        with tempfile.TemporaryDirectory() as t:
            note(t, "alpha-coordinator", NAME, "the tail\n")
            t0 = time.monotonic()
            text = coordinatorstart.context(
                {"repos_root": "D:/src"}, env={"AGENT_TEMP": t, "PM_KIT_SESSION_NAME": NAME}, cwd="D:/src/alpha",
                procs=[], run=run, read=read, kit="H", pid=1, pm_cfg={"sessions_file": "S.json"}, machine_cfg={},
                deadline=0.5)
            release.set()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("  | the tail", text)
        self.assertIn("could not read hub pending: no answer within 0 s", text)
        self.assertIn("EX-ONE", text)
        self.assertIn("kit/one", text)

    def test_no_sessions_entry_gives_a_line_per_command_part_and_the_note_search_still_runs(self):
        with tempfile.TemporaryDirectory() as t:
            text = coordinatorstart.context({"repos_root": "D:/src"}, env={"AGENT_TEMP": t}, cwd="C:/elsewhere",
                                            procs=[], run=lambda a, timeout=10: "", read=lambda p: "{}", kit="H",
                                            pid=1, pm_cfg={}, machine_cfg={})
        self.assertIn("could not read backlog next:", text)
        self.assertIn("could not read worktrees:", text)
        self.assertIn("could not read hub pending:", text)
        self.assertIn("state note: none found", text)


if __name__ == "__main__":
    unittest.main()
