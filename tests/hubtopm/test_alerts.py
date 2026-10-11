"""The Action Hub page's alert text, status buckets, command list and freshness logic.

These are the pure functions between the `pm-pure:begin` and `pm-pure:end` markers in
claude/hub/action-hub.html (docs/action-hub.md "Alert the PM"), run under node like test_hubtopm.
"""
import unittest

from tests.hubtopm.test_hubtopm import NODE, js

NOW = "2026-10-11T12:00:00Z"


def state(**kw):
    s = {"topm": [], "docs": [], "stories": [], "todos": []}
    s.update(kw)
    return s


def text(s, mx=3900):
    return js("pmAlertText(items, %d)" % mx, s)


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class AlertTextTest(unittest.TestCase):
    def test_every_section_carries_ids(self):
        s = state(
            topm=[{"id": "P-001", "status": "sent", "kind": "question", "text": "hello"}],
            docs=[{"id": "Q-9", "status": "answered", "answer": {"label": "Yes", "note": "go"}}],
            stories=[{"id": "S1", "status": "submitted", "repo": "alpha", "title": "Do it"}],
            todos=[{"id": "T-1", "status": "done", "title": "ticked"},
                   {"id": "T-2", "status": "open", "from": "you", "title": "mine", "urgent": True}])
        t = text(s)
        for want in ("P-001", "Q-9: Yes (note: go)", "id S1", "T-1: ticked", "T-2: mine (urgent)"):
            self.assertIn(want, t)

    def test_acknowledged_todos_are_left_out(self):
        s = state(todos=[{"id": "T-1", "status": "done", "title": "a", "pm_ack": True},
                         {"id": "T-2", "status": "open", "from": "you", "title": "b", "pm_ack": True}])
        self.assertIn("Nothing new", text(s))
        self.assertTrue(js("pmAlertEmpty(items)", s))
        self.assertFalse(js("pmAlertEmpty(items)", state(todos=[{"id": "T-3", "status": "done"}])))

    def test_trim_is_by_whole_line_with_a_count(self):
        s = state(todos=[{"id": "T-%03d" % i, "status": "done", "title": "x" * 60} for i in range(200)])
        t = text(s)
        self.assertLessEqual(len(t.encode("utf-8")), 3900)
        self.assertRegex(t, r"\(\+\d+ more not shown")
        for line in t.split("\n"):
            if line.startswith("- T-"):
                self.assertEqual(line, "- T-%s: %s" % (line[4:7], "x" * 60))   # no line cut in half

    def test_trim_counts_bytes_not_characters(self):
        s = state(todos=[{"id": "T-%03d" % i, "status": "done", "title": "é" * 80} for i in range(100)])
        t = text(s, 1000)
        self.assertLessEqual(len(t.encode("utf-8")), 1000)
        self.assertIn("more not shown", t)

    def test_trim_leaves_no_empty_header_or_blank_before_note(self):
        s = state(topm=[{"id": "P-001", "status": "sent", "kind": "question", "text": "q"}],
                  todos=[{"id": "T-%03d" % i, "status": "done", "title": "x" * 60} for i in range(30)])
        full = text(s)
        first_todo = full.index("To-dos ticked done:")
        # Room for the To PM section and the note, but for no ticked to-do.
        mx = len(full[:first_todo].rstrip("\n").encode("utf-8")) + 80
        t = text(s, mx)
        self.assertLessEqual(len(t.encode("utf-8")), mx)
        self.assertNotIn("To-dos ticked done:", t)
        self.assertIn("P-001", t)
        self.assertNotIn("\n\n(+", t)
        self.assertRegex(t, r"\(\+30 more not shown")

    def test_short_text_is_not_marked(self):
        self.assertNotIn("more not shown", text(state(todos=[{"id": "T-1", "status": "done", "title": "a"}])))


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class SmallFunctionsTest(unittest.TestCase):
    def test_commands_accepts_a_string(self):
        for t, want in [({"commands": ["a", "b"]}, ["a", "b"]), ({"commands": "run it"}, ["run it"]),
                        ({"commands": "  "}, []), ({"command": "old"}, ["old"]), ({}, []),
                        ({"commands": ["a", 3, None]}, ["a"])]:
            self.assertEqual(js("pmCommands(items)", t), want, t)

    def test_unknown_status_goes_to_other(self):
        for st, want in [("open", "open"), ("answered", "answered"), ("logged", "logged"),
                         ("weird", "other"), (None, "other")]:
            d = {"id": "Q-1"}
            if st is not None:
                d["status"] = st
            self.assertEqual(js("pmQBucket(items)", d), want, st)

    def test_freshness(self):
        now = "Date.parse('%s')" % NOW
        self.assertEqual(js("pmFresh(items, %s)" % now, []), {"kind": "never"})
        self.assertEqual(js("pmFresh(items, %s)" % now, [{"posted_at": None}]), {"kind": "never"})
        self.assertEqual(js("pmFresh(items, %s)" % now, [{"posted_at": "2026-10-10T12:00:00Z"}]), {"kind": "fresh"})
        old = [{"posted_at": "2026-10-01T00:00:00Z"}, {"posted_at": "2026-10-05T00:00:00Z"}]
        self.assertEqual(js("pmFresh(items, %s)" % now, old), {"kind": "stale", "since": "2026-10-05T00:00:00Z"})
        edge = [{"posted_at": "2026-10-08T12:00:00Z"}]                        # exactly 3 days: still fresh
        self.assertEqual(js("pmFresh(items, %s)" % now, edge), {"kind": "fresh"})

    def test_alert_why(self):
        self.assertEqual(js('pmAlertWhy("available", true)', []), "")
        ns = js('pmAlertWhy("no_session", true)', [])
        self.assertIn("No PM session is watching", ns)
        self.assertNotIn("next turn anyway", ns)
        self.assertIn("saved", ns)
        self.assertIn("next hub check", ns)
        self.assertIn("blocked", ns)
        self.assertIn("comment access", js('pmAlertWhy("off", false)', []))
        self.assertIn("editor", js('pmAlertWhy("writers_only", true)', []))
        self.assertTrue(js('pmAlertWhy("surprise", true)', []))

    def test_every_blocked_reason_says_queued_items_are_saved(self):
        for code, has in [("off", False), ("off", True), ("writers_only", True), ("surprise", True)]:
            self.assertIn("Your queued items are saved.", js('pmAlertWhy("%s", %s)' % (code, "true" if has else "false"), []), code)
        self.assertEqual(js('pmAlertWhy("no_session", true)', []),
                         "No PM session is watching right now, so Send all is blocked. Your queued items are saved; the PM sees sent items at its next hub check.")

    def test_load_banner_lists_only_collections_still_failing(self):
        self.assertEqual(js("pmLoadBanner(items)", {}), "")
        t = js("pmLoadBanner(items)", {"todos": "denied", "usage": "offline"})
        self.assertIn("todos, usage (denied; offline)", t)
        self.assertNotIn("todos", js("pmLoadBanner(items)", {"usage": "offline"}))

    def test_auto_alert_skips_repeat_and_empty(self):
        s = state(todos=[{"id": "T-1", "status": "done", "title": "a"}])
        self.assertEqual(js('pmAutoAlert(items, "", 3900)', s)["send"], True)
        t = js('pmAutoAlert(items, "", 3900)', s)["text"]
        self.assertEqual(js('pmAutoAlert(items, %s, 3900)' % __import__("json").dumps(t), s)["send"], False)
        self.assertEqual(js('pmAutoAlert(items, "", 3900)', state())["send"], False)
