"""The LIMIT check: a session stalled at its weekly usage limit, told from an idle one (docs/pm-watch.md
"Usage-limit stall"). The readers over real entries (usage_limit_entry.jsonl: line 1 a main session's,
line 2 a subagent's), limit_events over fake records, and the check inside a Watcher."""
import copy
import json
import os
import tempfile
import time
import unittest

from kitlib import pmwatch as W
from hosts.claude.adapter import claude_host as H

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_limit_entry.jsonl")
with open(FIXTURE, encoding="utf-8") as f:
    MAIN, SUB = [json.loads(line) for line in f]
RESET_MAIN, RESET_SUB = 1792468800, 1792771200
NOW = 1792400000.0                     # before both resets
SID = "11111111-2222-3333-4444-555555555555"


def retyped(entry, kind):
    """The entry with its rateLimitType changed: no real five-hour entry has been seen."""
    e = copy.deepcopy(entry)
    e["apiErrorParams"]["rate_limit_info"]["rateLimitType"] = kind
    e["quotaLimits"]["rateLimitType"] = kind
    return e


def work(stamp, kind="assistant"):
    return {"type": kind, "timestamp": stamp, "message": {"content": "work"}}


class Entries(unittest.TestCase):
    def test_both_real_entries_read_as_the_weekly_limit(self):
        self.assertEqual(H.weekly_reset(MAIN), RESET_MAIN)
        self.assertEqual(H.weekly_reset(SUB), RESET_SUB)

    def test_the_subagent_entry_has_no_status_and_the_main_one_does(self):
        self.assertNotIn("apiErrorStatus", SUB)        # what makes the reader not require it
        self.assertEqual(MAIN["apiErrorStatus"], 429)

    def test_the_display_text_is_not_read(self):
        e = copy.deepcopy(MAIN)
        e["message"]["content"] = [{"type": "text", "text": "something else"}]
        self.assertEqual(H.weekly_reset(e), RESET_MAIN)
        ordinary = dict(work("t"), message=MAIN["message"])    # the words alone are no limit entry
        self.assertIsNone(H.weekly_reset(ordinary))

    def test_a_window_other_than_seven_day_is_no_stall(self):
        for kind in ("five_hour", "seven_day_opus", "something_new", "", None):
            with self.subTest(kind=kind):
                self.assertIsNone(H.weekly_reset(retyped(MAIN, kind)))

    def test_each_structured_field_is_required(self):
        for key, value in (("isApiErrorMessage", False), ("isApiErrorMessage", None),
                           ("apiError", "server_error"), ("apiError", None)):
            with self.subTest(key=key, value=value):
                self.assertIsNone(H.weekly_reset(dict(MAIN, **{key: value})))

    def test_quota_limits_stands_in_for_a_missing_rate_limit_info(self):
        e = dict(MAIN, apiErrorParams=None)
        self.assertEqual(H.weekly_reset(e), RESET_MAIN)

    def test_a_reset_that_is_no_number_or_a_malformed_block_gives_none(self):
        for bad in ("soon", None, True):
            e = copy.deepcopy(MAIN)
            e["apiErrorParams"]["rate_limit_info"]["resetsAt"] = bad
            self.assertIsNone(H.weekly_reset(e))
        self.assertIsNone(H.weekly_reset(dict(MAIN, apiErrorParams=None, quotaLimits="x")))
        self.assertIsNone(H.weekly_reset(None))


class Tail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.jsonl")

    def write(self, *lines):
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))

    def test_the_last_conversation_entry_is_returned(self):
        self.write(MAIN, work("2026-10-16T10:00:00.000Z"))
        self.assertEqual(H.newest_entry(self.path)["message"]["content"], "work")

    def test_records_of_other_types_after_the_entry_are_passed_over(self):
        self.write(MAIN, {"type": "custom-title", "customTitle": "n"}, {"type": "last-prompt"})
        self.assertEqual(H.newest_entry(self.path)["uuid"], MAIN["uuid"])

    def test_a_half_written_line_says_nothing_yet(self):
        self.write(MAIN, '{"type":"assis')
        self.assertIs(H.newest_entry(self.path), H.UNREADABLE)

    def test_only_the_tail_is_read(self):
        # a broken line far above the tail would give None if it were read
        self.write("{broken", {"type": "assistant", "pad": "x" * (2 * H.TAIL_BYTES)}, MAIN)
        self.assertEqual(H.newest_entry(self.path)["uuid"], MAIN["uuid"])

    def test_a_newest_line_longer_than_the_tail_hides_the_older_entry(self):
        self.write(MAIN, {"type": "user", "pad": "x" * (2 * H.TAIL_BYTES)})
        self.assertIs(H.newest_entry(self.path), H.UNREADABLE)

    def test_a_tail_of_only_other_records_can_hide_the_newest_entry(self):
        self.write(MAIN, *[{"type": "last-prompt", "pad": "x" * 1000}] * 80)
        self.assertIs(H.newest_entry(self.path), H.UNREADABLE)

    def test_a_missing_file_is_unreadable_and_one_with_no_entry_is_none(self):
        self.assertIs(H.newest_entry(self.path), H.UNREADABLE)
        self.write()
        self.assertIsNone(H.newest_entry(self.path))
        self.write({"type": "custom-title", "customTitle": "n"})
        self.assertIsNone(H.newest_entry(self.path))


class Layout(unittest.TestCase):
    """weekly_stop over Claude's folders: <config>/projects/<p>/<id>.jsonl, <id>/subagents/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.tmp.name
        self.registry = os.path.join(self.config, "sessions")
        self.proj = os.path.join(self.config, "projects", "P--repo")
        self.subs = os.path.join(self.proj, SID, "subagents")
        os.makedirs(self.registry)
        os.makedirs(self.subs)
        self.tick = 1_700_000_000

    def main(self, *entries):
        self._write(os.path.join(self.proj, SID + ".jsonl"), entries)

    def sub(self, name, *entries):
        self._write(os.path.join(self.subs, "agent-%s.jsonl" % name), entries)

    def _write(self, path, entries):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries))
        self.tick += 10                 # written in call order, as the mtime the reader goes by
        os.utime(path, (self.tick, self.tick))

    def stop(self):
        found = H.weekly_stop(SID, [self.config])
        return found and found[1]

    def _half_line(self, name):
        path = os.path.join(self.proj, SID + ".jsonl") if name == "main" \
            else os.path.join(self.subs, "agent-%s.jsonl" % name)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write('{"type":"assis')

    def test_a_main_limit_entry_stalls_the_session(self):
        self.main(work("2026-10-16T09:00:00.000Z"), MAIN)
        self.assertEqual(self.stop(), RESET_MAIN)

    def test_a_main_session_waiting_on_a_stalled_subagent_is_stalled(self):
        self.main(work("2026-10-23T15:00:00.000Z"))
        self.sub("a", work("2026-10-23T15:10:00.000Z"), SUB)
        self.assertEqual(self.stop(), RESET_SUB)

    def test_older_limit_entry_followed_by_newer_work_is_no_stall(self):
        self.main(MAIN, work("2026-10-20T01:00:00.000Z", "user"))
        self.assertIsNone(self.stop())

    def test_a_subagent_entry_the_main_session_has_moved_past_is_no_stall(self):
        self.sub("a", SUB)
        self.main(work("2026-10-23T15:30:00.000Z"))
        self.assertIsNone(self.stop())

    def test_main_bookkeeping_written_after_a_stalled_subagent_does_not_hide_it(self):
        self.sub("a", SUB)
        self.main(work("2026-10-23T15:00:00.000Z"), {"type": "last-prompt"})    # mtime newer
        self.assertEqual(self.stop(), RESET_SUB)

    def test_a_subagent_entry_another_subagent_has_moved_past_is_no_stall(self):
        self.main(work("2026-10-23T15:00:00.000Z"))
        self.sub("a", SUB)
        self.sub("b", work("2026-10-23T15:40:00.000Z"))
        self.assertIsNone(self.stop())

    def test_an_old_main_limit_entry_with_a_working_subagent_is_no_stall(self):
        self.main(MAIN)
        self.sub("a", work("2026-10-20T01:00:00.000Z"))
        self.assertIsNone(self.stop())

    def test_five_hour_entry_in_the_transcript_is_no_stall(self):
        self.main(retyped(MAIN, "five_hour"))
        self.assertIsNone(self.stop())

    def test_no_transcript_or_no_session_id_is_no_stall(self):
        self.assertIsNone(self.stop())
        self.main(MAIN)
        for sid in (None, "", 7):
            self.assertIsNone(H.weekly_stop(sid, [self.config]))

    def test_a_half_written_main_line_does_not_let_a_subagent_entry_decide(self):
        self.main(work("2026-10-23T15:00:00.000Z"))
        self.sub("a", SUB)
        self._half_line("main")
        self.assertIsNone(self.stop())

    def test_an_over_long_main_line_does_not_let_a_subagent_entry_decide(self):
        self.sub("a", SUB)
        self.main({"type": "user", "timestamp": "2026-10-23T16:00:00.000Z",
                   "pad": "x" * (2 * H.TAIL_BYTES)})
        self.assertIsNone(self.stop())

    def test_a_half_written_subagent_line_does_not_let_a_main_entry_decide(self):
        self.main(MAIN)
        self.sub("a", work("2026-10-16T09:00:00.000Z"))
        self._half_line("a")
        self.assertIsNone(self.stop())

    def test_rc_records_gives_a_lazy_reader(self):
        self.main(MAIN)
        with open(os.path.join(self.registry, "5.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 5, "name": "n", "startedAt": 1, "procStart": "9", "sessionId": SID}, f)
        rec = H.rc_records([self.registry])[0]
        self.assertEqual(rec["weekly_stop"]([self.config]), (self.config, RESET_MAIN))


class SharedRegistry(unittest.TestCase):
    """As on the machine: every account's sessions folder is one real folder, so a record says
    nothing of its account; the transcripts sit in each account's own folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = {n: os.path.join(self.tmp.name, "account" + n) for n in "12"}
        self.folders = {d: n for n, d in self.dirs.items()}
        self.shared = os.path.join(self.tmp.name, "default", "sessions")
        os.makedirs(self.shared)
        with open(os.path.join(self.shared, "5.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 5, "name": "kit-a1", "startedAt": 1, "procStart": "9",
                       "sessionId": SID}, f)

    def transcript(self, n, entry, mtime=1_700_000_000):
        proj = os.path.join(self.dirs[n], "projects", "P--repo")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, SID + ".jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry) + "\n")
        os.utime(path, (mtime, mtime))

    def events(self, readings):
        return W.limit_events({"kit-a1"}, H.rc_records([self.shared]), lambda r: True, self.folders,
                              readings, [], NOW)

    def test_the_account_is_the_one_holding_the_transcript(self):
        self.transcript("1", MAIN)
        ev, _ = self.events({"1": {"week": 100}, "2": {"week": 10}})
        self.assertEqual(len(ev), 1)
        self.assertIn(" account 1 ", ev[0])

    def test_the_reading_is_the_transcript_accounts_and_no_other(self):
        self.transcript("1", MAIN)
        self.assertEqual(self.events({"1": {"week": 10}, "2": {"week": 100}}), ([], []))

    def test_a_session_moved_to_another_account_goes_by_the_copy_written_last(self):
        self.transcript("1", MAIN, mtime=1_700_000_000)
        self.transcript("2", work("2026-10-17T00:00:00.000Z"), mtime=1_700_000_100)
        self.assertEqual(self.events({"1": {"week": 100}, "2": {"week": 100}}), ([], []))

    def test_a_transcript_in_no_accounts_folder_gets_no_limit(self):
        self.folders.pop(self.dirs["1"])
        self.transcript("1", MAIN)
        self.assertEqual(self.events({"1": {"week": 100}, "2": {"week": 100}}), ([], []))


def rec(name="kit-a1", pid=11, stop=RESET_MAIN, folder="/one", proc_start="900", started=1000):
    r = {"name": name, "pid": pid, "proc_start": proc_start, "started_ms": started}
    if stop is not False:
        r["weekly_stop"] = lambda folders: None if stop is None else (folder, stop)
    return r


def live(r):
    return r["pid"] != 99


FULL = {"1": {"week": 100, "stale": False}}
FOLDERS = {"/one": "1", "/two": "2"}
KEY = "kit-a1 11 900 %d" % RESET_MAIN


class AtLimit(unittest.TestCase):
    def test_the_reading_rule(self):
        for u, want in (({"week": 100, "stale": False}, True), ({"week": 100, "stale": True}, True),
                        ({"week": 99, "stale": True}, True), ({"week": 99, "stale": False}, False),
                        ({"week": 98, "stale": True}, False), ({"week": None, "stale": True}, False),
                        ({"week": 0, "stale": True}, False), ({"five": 100}, False), ({}, False)):
            with self.subTest(u=u):
                self.assertIs(W.at_limit(u), want)


class LimitEvents(unittest.TestCase):
    def events(self, records, readings=FULL, fired=(), now=NOW, names=("kit-a1",)):
        return W.limit_events(set(names), records, live, FOLDERS, readings, list(fired), now)

    def test_a_stalled_session_fires_once(self):
        ev, fired = self.events([rec()])
        self.assertEqual(ev, ["LIMIT kit-a1 account 1 stalled at the weekly limit until %s (pid 11)"
                              % time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(RESET_MAIN))])
        self.assertEqual(fired, [KEY])
        self.assertEqual(self.events([rec()], fired=fired), ([], [KEY]))

    def test_it_fires_again_only_after_it_stopped_reading_stalled(self):
        self.assertEqual(self.events([rec(stop=None)], fired=[KEY]), ([], []))
        self.assertEqual(len(self.events([rec()], fired=[])[0]), 1)

    def test_a_session_with_no_limit_entry_is_only_idle(self):
        self.assertEqual(self.events([rec(stop=None)]), ([], []))

    def test_a_dead_process_is_no_stall(self):
        self.assertEqual(self.events([rec(pid=99)]), ([], []))

    def test_an_account_under_the_limit_is_no_stall(self):
        self.assertEqual(self.events([rec()], readings={"1": {"week": 99, "stale": False}}), ([], []))

    def test_a_reset_in_the_past_is_no_stall(self):
        self.assertEqual(self.events([rec(stop=NOW - 1)]), ([], []))
        self.assertEqual(self.events([rec(stop=NOW)]), ([], []))

    def test_a_session_on_another_account_or_none_is_no_stall(self):
        self.assertEqual(self.events([rec(folder="/two")]), ([], []))
        self.assertEqual(self.events([rec(folder="/org")]), ([], []))

    def test_a_record_with_no_reader_or_an_unwatched_name_is_skipped(self):
        self.assertEqual(self.events([rec(stop=False)]), ([], []))
        self.assertEqual(self.events([rec(name="other")]), ([], []))

    def test_the_newest_live_record_of_a_name_decides(self):
        old = rec(pid=7, started=500)
        new = rec(pid=11, stop=None, started=900)
        self.assertEqual(self.events([old, new]), ([], []))

    def test_a_relaunch_is_a_new_stall(self):
        ev, fired = self.events([rec(pid=12, proc_start="901")], fired=[KEY])
        self.assertEqual(len(ev), 1)
        self.assertEqual(fired, ["kit-a1 12 901 %d" % RESET_MAIN])


class Check(unittest.TestCase):
    def check(self, readings, read):
        def records():
            read.append("records")
            return [rec()]

        def accounts():
            read.append("accounts")
            return FOLDERS
        return W.limit_check(lambda: {"kit-a1"}, records, live, accounts)(["old"], readings, NOW)

    def test_nothing_is_read_below_the_limit_and_fired_clears(self):
        read = []
        self.assertEqual(self.check({"1": {"week": 50, "stale": False}}, read), ([], []))
        self.assertEqual(read, [])

    def test_at_the_limit_the_folder_maps_to_the_account(self):
        read = []
        ev, fired = self.check(FULL, read)
        self.assertEqual(sorted(read), ["accounts", "records"])
        self.assertTrue(ev[0].startswith("LIMIT kit-a1 account 1 "), ev)
        self.assertEqual(len(fired), 1)

    def test_account_folders_maps_each_folder_of_each_account(self):
        class Settings:
            def account_numbers(self):
                return ["1", "2"]

            def account_dirs(self, n):
                return {"1": ["/a", "/b"], "2": ["/c"]}[n]
        self.assertEqual(W.account_folders(Settings()), {"/a": "1", "/b": "1", "/c": "2"})


class InWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = os.path.join(self.tmp.name, "sessions.json")
        with open(self.sessions, "w", encoding="utf-8") as f:
            json.dump({"k": {"name": "kit-a1"}}, f)
        self.usage = {"1": {"week": 100, "stale": False}}
        self.records = [rec(proc_start="5")]

    def watcher(self, **kw):
        rc = W.rc_check(self.sessions, lambda dirs: self.records, dirs=lambda: [],
                        ctime=lambda pid: 5, rc_pids=lambda: {},
                        accounts=lambda: FOLDERS)
        return W.Watcher({}, [], lambda: self.usage, os.path.join(self.tmp.name, "none"),
                         self.sessions, lambda n: NOW, idle_poll=300, rc=rc, **kw)

    def limits(self, ev):
        return [e for e in ev if e.startswith("LIMIT")]

    def test_rc_check_brings_limit_and_it_fires_once_across_polls_on_the_idle_cadence(self):
        w = self.watcher()
        w.start(NOW)
        self.assertEqual(len(self.limits(w.poll(NOW))), 1)
        self.assertEqual(w.s["limit_stalled"], ["kit-a1 11 5 %d" % RESET_MAIN])
        self.assertEqual(self.limits(w.poll(NOW + 60)), [])         # not due yet
        self.assertEqual(self.limits(w.poll(NOW + 300)), [])        # due: already reported

    def test_a_relaunched_watcher_with_the_saved_state_repeats_nothing(self):
        w = self.watcher()
        w.start(NOW)
        w.poll(NOW)
        again = W.Watcher(w.s, [], lambda: self.usage, os.path.join(self.tmp.name, "none"),
                          self.sessions, lambda n: NOW, rc=w.rc)
        self.assertEqual(self.limits(again.poll(NOW)), [])

    def test_a_yielded_watcher_drops_the_line_and_keeps_the_state(self):
        w = self.watcher(yield_to=lambda: True)
        w.start(NOW)
        self.assertEqual(self.limits(w.poll(NOW)), [])
        self.assertEqual(len(w.s["limit_stalled"]), 1)

    def test_a_rc_check_without_limit_runs_no_limit_check(self):
        w = W.Watcher({}, [], lambda: self.usage, "none", self.sessions, lambda n: NOW,
                      rc=lambda lost: ([], []))
        self.assertIsNone(w.limit)

    def test_third_failure_in_a_row_is_an_error_line(self):
        def boom(fired, readings, now):
            raise OSError("registry unreadable")
        w = W.Watcher({"usage": self.usage}, [], lambda: self.usage, "none", self.sessions,
                      lambda n: NOW, idle_poll=300, limit=boom)
        self.assertEqual(w.poll(NOW) + w.poll(NOW + 300), [])
        self.assertEqual(w.poll(NOW + 600), ["ERROR limit check: registry unreadable"])


if __name__ == "__main__":
    unittest.main()
