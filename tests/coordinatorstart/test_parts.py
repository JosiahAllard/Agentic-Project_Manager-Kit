"""The read-only start parts of the coordinator header (kitlib.coordinatorstart.parts)."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import coordinatorstart
from kitlib.coordinatorstart import parts

NAME = "alpha-coordinator-a2-n6"
ENTRY_ALPHA = {"repo": "alpha", "agent": True, "stem": "alpha-coordinator", "name": NAME}
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

    def test_a_note_is_never_read_whole(self):
        with tempfile.TemporaryDirectory() as t:
            p = note(t, "x-coordinator", NAME, "first\n" + ("pad line\n" * 5000) + "last\n")
            reads = []
            real_open = open

            def spy(path, mode="r", *a, **kw):
                f = real_open(path, mode, *a, **kw)
                if os.path.abspath(str(path)) == os.path.abspath(p):
                    orig = f.read
                    f.read = lambda n=-1: (reads.append(n), orig(n))[1]
                return f
            with mock.patch("builtins.open", spy):
                out = parts.state_note({"AGENT_TEMP": t}, NAME, [])
        self.assertEqual(out[-1], "  | last")
        self.assertNotIn("first", "\n".join(out))
        self.assertTrue(reads and all(0 <= n <= parts.TAIL_BYTES for n in reads))
        self.assertNotIn(-1, reads)                        # read() with no bound would be the whole file

    def test_the_none_found_line_shows_the_home_form_not_a_user_folder(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch("os.path.expanduser", lambda p: t if p == "~" else p):
                out = parts.state_note({"AGENT_TEMP": os.path.join(t, "agent")}, NAME, [])
        self.assertIn("under ~/agent/*-coordinator", out[0])
        self.assertNotIn(t.replace("\\", "/"), out[0])

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
        out = parts.backlog_next("R", "k", lambda a, timeout: NEXT, where=lambda r: r)
        self.assertEqual(out[0], "backlog next (local copy, not fetched):")
        self.assertTrue(any("EX-ONE" in l for l in out))
        self.assertFalse(any("passed over" in l or "blockers" in l for l in out))

    def test_next_never_fetches_and_reads_the_store_root(self):
        seen = []

        def run(args, timeout):
            seen.append(args)
            return NEXT
        parts.backlog_next("R", "k", run, where=lambda r: "PM")
        self.assertEqual(len(seen), 1)
        self.assertIn("--no-fetch", seen[0])
        self.assertLess(seen[0].index("--no-fetch"), seen[0].index("next"))   # a global option, before the verb
        self.assertEqual(seen[0][seen[0].index("-C") + 1], "PM")

    def test_store_root_is_the_pm_data_worktree_only_when_it_names_the_store(self):
        def root(repo_named, pm):
            cfg = mock.Mock()
            cfg.written.return_value = repo_named
            with mock.patch("kitlib.config.load", return_value=cfg), \
                    mock.patch("kitlib.backlog.store.pm_data_live_store", return_value=pm):
                return parts.store_root("R")
        self.assertEqual(root(None, ("PM", "kit/backlog", None)), "PM")
        self.assertEqual(root("ref", ("PM", "kit/backlog", None)), "R")      # the repo names its own store
        self.assertEqual(root(None, ("PM", None, None)), "R")                 # the PM tree names none
        self.assertEqual(root(None, (None, None, "not found")), "R")

    def test_worktrees_keeps_the_class_lines_and_the_stale_rows_not_the_live_rows(self):
        text = "\n".join(parts.worktrees("R", lambda a, timeout: CLASSIFY))
        self.assertIn("C half-done", text)
        self.assertIn("kit/one", text)
        self.assertIn("A landed", text)
        self.assertNotIn("kit/two", text)
        self.assertNotIn("classify  3", text)

    def test_the_production_defaults_run_classify_read_only_and_parse_its_exit_one(self):
        # _start_parts with the default runner: hub and next must exit 0, classify may exit 1 and is read
        argvs = []

        def fake_exec(args, timeout):
            argvs.append(args)
            tool = os.path.basename(args[1])
            return (1, CLASSIFY, "") if tool == "worktree_remove.py" else (0, NEXT if tool == "backlog.py" else "", "")
        box = {"s": {"kitc": ENTRY_ALPHA}, "e": (ENTRY_ALPHA, None)}
        with mock.patch.object(parts, "_exec", fake_exec), tempfile.TemporaryDirectory() as t, \
                mock.patch.object(parts, "store_root", lambda r: r), \
                mock.patch.object(parts.backlog_next, "__defaults__", (None, lambda r: r)):
            lines = coordinatorstart._start_parts({"repos_root": "D:/src"}, box, lambda: ENTRY_ALPHA,
                                                  {"AGENT_TEMP": t}, coordinatorstart._run, None, "H", 5)
        text = "\n".join(lines)
        self.assertIn("C half-done", text)                 # exit 1 with output was parsed
        self.assertNotIn("could not read worktrees", text)
        classify = [a for a in argvs if os.path.basename(a[1]) == "worktree_remove.py"][0]
        self.assertIn("--classify", classify)
        self.assertNotIn("--apply", classify)              # read-only
        nxt = [a for a in argvs if os.path.basename(a[1]) == "backlog.py"][0]
        self.assertIn("--no-fetch", nxt)

    def test_classify_exit_one_with_output_is_read_but_exit_one_without_is_an_error(self):
        with mock.patch.object(parts, "_exec", lambda a, t: (1, CLASSIFY, "boom")):
            self.assertIn("C half-done", parts.run_loose(["x"]))
            with self.assertRaises(RuntimeError):
                parts.run_strict(["x"])                    # a command that must exit 0
        with mock.patch.object(parts, "_exec", lambda a, t: (1, "", "boom")):
            with self.assertRaises(RuntimeError):
                parts.run_loose(["x", "y"])

    def test_a_timeout_ends_the_whole_process_tree(self):
        proc = mock.Mock(pid=77)
        proc.communicate.side_effect = [parts.subprocess.TimeoutExpired("x", 1), ("", "")]
        killed = []
        with mock.patch.object(parts.spawn, "popen", return_value=proc), \
                mock.patch("kitlib.proc.process.proc_ctime", return_value=123), \
                mock.patch("kitlib.proc.process.kill_tree", lambda pid, ct: killed.append((pid, ct))):
            with self.assertRaises(parts.subprocess.TimeoutExpired):
                parts._exec(["x"], 1)
        self.assertEqual(killed, [(77, 123)])


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
