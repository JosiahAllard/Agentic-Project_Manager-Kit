"""The Action Hub's To PM logic (docs/action-hub.md "to_pm/P-NNN").

The page is one HTML file, so its pure functions sit between the `pm-pure:begin` and `pm-pure:end`
markers in hosts/claude/shell/hub/action-hub.html. These tests cut that block out and run it under node.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = os.path.join(ROOT, "hosts", "claude", "shell", "hub", "action-hub.html")
NODE = shutil.which("node")

NOW = "2026-10-07T12:00:00Z"


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def block():
    m = re.search(r"// pm-pure:begin\r?\n(.*?)// pm-pure:end", page(), re.S)
    assert m, "pm-pure markers missing from hosts/claude/shell/hub/action-hub.html"
    return m.group(1)


def js(expr, items):
    """Evaluate expr (which sees `items` and the page's pm* functions) under node; return its JSON."""
    src = block() + "\nconst items = %s;\nconsole.log(JSON.stringify(%s));" % (json.dumps(items), expr)
    # The script goes in on stdin: the pure block outgrew the Windows command-line limit.
    out = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def item(i, status, **kw):
    d = {"id": "P-%03d" % i, "status": status, "kind": "question", "repo": "", "urgent": False,
         "text": "t%d" % i, "created_at": "2026-10-01T00:00:00Z"}
    d.update(kw)
    return d


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class LogicTest(unittest.TestCase):
    def test_next_id(self):
        self.assertEqual(js("pmNextId(items)", []), "P-001")
        self.assertEqual(js("pmNextId(items)", [item(1, "done"), item(7, "sent"), {"id": "x"}]), "P-008")
        self.assertEqual(js("pmNextId(items)", [item(999, "done")]), "P-1000")

    def test_send_all_moves_only_queued(self):
        items = [item(1, "queued"), item(2, "sent"), item(3, "queued"), item(4, "done")]
        got = js('pmSendAll(items, "%s")' % NOW, items)
        self.assertEqual(got, [{"id": "P-001", "patch": {"status": "sent", "sent_at": NOW}},
                               {"id": "P-003", "patch": {"status": "sent", "sent_at": NOW}}])
        self.assertEqual(js('pmSendAll(items, "%s")' % NOW, [item(2, "sent")]), [])

    def test_badge_is_queued_plus_sent(self):
        items = [item(1, "queued"), item(2, "sent"), item(3, "done"), item(4, "sent", urgent=True)]
        self.assertEqual(js("pmBadge(items)", items), {"n": 3, "hot": True})
        self.assertEqual(js("pmBadge(items)", [item(1, "done")]), {"n": 0, "hot": False})

    def test_done_window_is_30_days(self):
        old = item(1, "done", done_at="2026-09-01T00:00:00Z")                 # 36 days
        edge = item(2, "done", done_at="2026-09-07T12:00:00Z")                # exactly 30 days: kept
        new = item(3, "done", done_at="2026-10-06T00:00:00Z")
        items = [old, edge, new]
        now = "Date.parse('%s')" % NOW
        got = js("pmRows(items, 'done', %s, false)" % now, items)
        self.assertEqual([r["id"] for r in got["rows"]], ["P-003", "P-002"])  # newest first
        self.assertEqual(got["hidden"], 1)
        got = js("pmRows(items, 'done', %s, true)" % now, items)
        self.assertEqual([r["id"] for r in got["rows"]], ["P-003", "P-002", "P-001"])
        self.assertEqual(got["hidden"], 0)

    def test_window_does_not_touch_queued_or_sent(self):
        items = [item(1, "queued", created_at="2026-01-01T00:00:00Z"), item(2, "sent", sent_at="2026-01-01T00:00:00Z")]
        now = "Date.parse('%s')" % NOW
        self.assertEqual(len(js("pmRows(items, 'queued', %s, false)" % now, items)["rows"]), 1)
        self.assertEqual(len(js("pmRows(items, 'sent', %s, false)" % now, items)["rows"]), 1)

    def test_order_urgent_first_then_oldest(self):
        items = [item(1, "queued", created_at="2026-10-03T00:00:00Z"), item(2, "queued", created_at="2026-10-02T00:00:00Z"),
                 item(3, "queued", created_at="2026-10-04T00:00:00Z", urgent=True)]
        got = js("pmRows(items, 'queued', 0, false)", items)
        self.assertEqual([r["id"] for r in got["rows"]], ["P-003", "P-002", "P-001"])

    def test_alert_lists_sent_only(self):
        items = [item(1, "queued"), item(2, "sent", kind="fyi", repo="pm", text="line one\nline two"),
                 item(3, "sent", urgent=True), item(4, "done")]
        lines = js("pmAlertLines(items)", items)
        self.assertEqual(len(lines), 4)                                       # blank, heading, two items
        self.assertIn("P-002 [FYI, pm]: line one line two", lines[2])
        self.assertIn("P-003 [Question] (urgent)", lines[3])
        self.assertEqual(js("pmAlertLines(items)", [item(1, "queued")]), [])


class PageTest(unittest.TestCase):
    def test_wiring(self):
        p = page()
        self.assertRegex(p, r'data-s="topm"[^>]*>To PM')
        self.assertLess(p.index('data-s="questions"'), p.index('data-s="topm"'))
        self.assertLess(p.index('data-s="topm"'), p.index('data-s="todo"'))
        self.assertIn('db.collection("to_pm").onSnapshot', p)
        self.assertIn("...pmAlertLines(s.topm)", p)                           # in pmAlertLines2, which pmAlertText builds on
        self.assertIn("sendAlert = fire", p)
        self.assertEqual(p.count("cm.sendToClaude("), 1)                      # one alert path, reused by Send all

    def test_inbox_answers_untouched(self):
        p = page()
        self.assertEqual(p.count('db.collection("inbox")'), 1)
        self.assertNotIn("to_pm", p[p.index("function renderInbox"):p.index("// To PM")])


if __name__ == "__main__":
    unittest.main()
