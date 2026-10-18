"""The hub server's web page, kitlib/hubsite/web/index.html.

The page is one HTML file. Its pure functions sit between the `pm-pure:begin` and `pm-pure:end`
markers; these tests cut that block out and run it under node: the To PM rows, the freshness,
the snapshot's shape, the Parked groups, the PM line, the retry delay. The repo-tag mapping
(canonRepo) is cut out and run the same way. What node cannot show without a browser is checked
on the file's text: one
transport, no artifact-store call, no HTML sink, no outside resource, and only the decided routes.
"""
import datetime
import json
import os
import re
import shutil
import unittest

from kitlib.hubsite import httpd, serve
from kitlib.hubsite.api import BASE
from kitlib.proc import spawn
from tests.hubsite import httpfix, pagedom
from tests.hubsite.httpfix import get, post

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "kitlib", "hubsite", "web")
PAGE = os.path.join(WEB, "index.html")
NODE = shutil.which("node")

NOW = "2026-10-07T12:00:00Z"
NOW_MS = "Date.parse('%s')" % NOW


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def block():
    m = re.search(r"// pm-pure:begin\r?\n(.*?)// pm-pure:end", page(), re.S)
    assert m, "pm-pure markers missing from kitlib/hubsite/web/index.html"
    return m.group(1)


def node_json(src):
    """Run the script `src` under node; return the JSON it prints."""
    # The script goes in on stdin: the pure block outgrew the Windows command-line limit.
    out = spawn.run([NODE, "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def js(expr, items):
    """Evaluate expr (which sees `items` and the page's pure functions) under node; return its JSON."""
    return node_json(block() + "\nconst items = %s;\nconsole.log(JSON.stringify(%s));" % (json.dumps(items), expr))


def canon(repos, tags):
    """canonRepo, cut out of the page, over a stub `state` holding `repos`, for each tag."""
    m = re.search(r"  function canonRepo\(r\)\{.*?return hit \? named\(hit\) : \"\"; \}", page(), re.S)
    assert m, "canonRepo missing from kitlib/hubsite/web/index.html"
    return node_json("const state = {repos: %s};\n%s\nconsole.log(JSON.stringify(%s.map(canonRepo)));"
                     % (json.dumps(repos), m.group(0), json.dumps(tags)))


def item(i, status, **kw):
    d = {"id": "P-%03d" % i, "status": status, "kind": "question", "repo": "", "urgent": False,
         "text": "t%d" % i, "created_at": "2026-10-01T00:00:00Z"}
    d.update(kw)
    return d


def parked(repo, item_id, at, **kw):
    d = {"id": "%s:%s" % (repo, item_id), "repo": repo, "item": item_id, "status": "parked", "parked_at": at}
    d.update(kw)
    return d


def snapshot(**kw):
    d = {"revision": 3, "pending": [], "collections": {}, "views": {}, "unavailable": {},
         "pm_seen": {"waited": None, "pulled": None, "waiting": 0}}
    d.update(kw)
    return d


class ServedTest(httpfix.ListenerCase):
    def test_the_listener_serves_the_page_at_the_root(self):
        self.assertEqual(os.path.normcase(os.path.realpath(serve.STATIC_DIR)), os.path.normcase(os.path.realpath(WEB)))
        lis = self.listener(static_dir=serve.STATIC_DIR)
        r = get(lis.port, "/")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), ["text/html; charset=utf-8"])
        with open(PAGE, "rb") as f:
            self.assertEqual(r.body, f.read())
        self.assertEqual(get(lis.port, "/index.html").body, r.body)
        for name, value in httpd.SECURITY_HEADERS:
            self.assertEqual(r.header(name), [value])
        self.assertEqual(self.errors(), [])

    @unittest.skipUnless(NODE, "node is needed to run the page's logic")
    def test_the_page_reads_the_snapshot_the_server_gives(self):
        lis = self.listener(static_dir=serve.STATIC_DIR)
        title = "<img src=x onerror=1>"
        made = post(lis.port, BASE + "/todos", {"title": title}, token=self.owner)
        self.assertEqual(made.status, 200, made.body)
        snap = get(lis.port, BASE + "/snapshot", token=self.owner)
        self.assertEqual(snap.status, 200, snap.body)
        got = js("hubState(items)", snap.json())
        self.assertEqual(got["revision"], snap.json()["revision"])
        self.assertEqual([(t["title"], t["from"]) for t in got["todos"]], [(title, "you")])
        self.assertEqual(got["todos"][0]["id"], made.json()["id"])
        for k in ("docs", "inbox", "stories", "topm", "repos", "backlog", "completed", "parked", "usage", "agents", "pages"):
            self.assertEqual(got[k], [], k)
        self.assertEqual(got["pageHtml"], {})
        self.assertEqual(got["gone"], snap.json()["unavailable"])


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class LogicTest(unittest.TestCase):
    """The To PM rows and the freshness, on the page's pure block."""

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
        got = js("pmRows(items, 'done', %s, false)" % NOW_MS, items)
        self.assertEqual([r["id"] for r in got["rows"]], ["P-003", "P-002"])  # newest first
        self.assertEqual(got["hidden"], 1)
        got = js("pmRows(items, 'done', %s, true)" % NOW_MS, items)
        self.assertEqual([r["id"] for r in got["rows"]], ["P-003", "P-002", "P-001"])
        self.assertEqual(got["hidden"], 0)

    def test_window_does_not_touch_queued_or_sent(self):
        items = [item(1, "queued", created_at="2026-01-01T00:00:00Z"), item(2, "sent", sent_at="2026-01-01T00:00:00Z")]
        self.assertEqual(len(js("pmRows(items, 'queued', %s, false)" % NOW_MS, items)["rows"]), 1)
        self.assertEqual(len(js("pmRows(items, 'sent', %s, false)" % NOW_MS, items)["rows"]), 1)

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

    def test_freshness(self):
        now = "Date.parse('2026-10-11T12:00:00Z')"
        self.assertEqual(js("pmFresh(items, %s)" % now, []), {"kind": "never"})
        self.assertEqual(js("pmFresh(items, %s)" % now, [{"posted_at": None}]), {"kind": "never"})
        self.assertEqual(js("pmFresh(items, %s)" % now, [{"posted_at": "2026-10-10T12:00:00Z"}]), {"kind": "fresh"})
        old = [{"posted_at": "2026-10-01T00:00:00Z"}, {"posted_at": "2026-10-05T00:00:00Z"}]
        self.assertEqual(js("pmFresh(items, %s)" % now, old), {"kind": "stale", "since": "2026-10-05T00:00:00Z"})
        edge = [{"posted_at": "2026-10-08T12:00:00Z"}]                        # exactly 3 days: still fresh
        self.assertEqual(js("pmFresh(items, %s)" % now, edge), {"kind": "fresh"})


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class CanonRepoTest(unittest.TestCase):
    """An item's `repo` tag maps to the repo whose repos/ doc has that name, or lists it in
    `aliases`; the page holds no alias of its own."""

    def test_no_aliases_maps_only_real_names(self):
        repos = [{"id": "alpha", "name": "Alpha"}, {"id": "beta"}]
        self.assertEqual(canon(repos, ["alpha", "BETA", "a", "machine", "", None]),
                         ["Alpha", "beta", "", "", "", ""])

    def test_aliases_from_the_repo_doc(self):
        repos = [{"id": "alpha", "name": "Alpha", "aliases": ["a", "Old-Alpha"]},
                 {"id": "beta", "name": "beta", "aliases": "not-a-list"}]
        self.assertEqual(canon(repos, ["A", "old-alpha", "not-a-list", "beta"]),
                         ["Alpha", "Alpha", "", "beta"])

    def test_a_real_name_wins_over_an_alias(self):
        repos = [{"name": "alpha", "aliases": ["beta"]}, {"name": "beta"}]
        self.assertEqual(canon(repos, ["beta"]), ["beta"])

    def test_the_page_names_no_alias(self):
        self.assertNotRegex(page(), r"\{\s*pm\s*:")
        # A repository's own name or alias in the page is the scrub's to catch (tests/scrub/
        # test_generic.py, the install names), so this test names none.


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class ParkedTest(unittest.TestCase):
    def test_amber_only_after_more_than_two_days(self):
        self.assertFalse(js("parkedOld(items, %s)" % NOW_MS, "2026-10-05T12:00:00Z"))      # exactly 2 days
        self.assertTrue(js("parkedOld(items, %s)" % NOW_MS, "2026-10-05T11:59:59Z"))
        self.assertFalse(js("parkedOld(items, %s)" % NOW_MS, "2026-10-07T11:00:00Z"))
        self.assertTrue(js("parkedOld(items, %s)" % NOW_MS, "2026-10-04"))                 # a bare day
        for unknown in (None, "", "soon"):
            self.assertFalse(js("parkedOld(items, %s)" % NOW_MS, unknown), unknown)

    def test_grouped_by_repository_longest_parked_first(self):
        rows = [parked("beta", "B-2", "2026-10-07T00:00:00Z"), parked("alpha", "B-9", None),
                parked("alpha", "B-3", "2026-10-06T00:00:00Z"), parked("alpha", "B-1", "2026-10-01T00:00:00Z")]
        got = js("parkedGroups(items, %s)" % NOW_MS, rows)
        self.assertEqual([g["repo"] for g in got], ["alpha", "beta"])
        self.assertEqual([(p["item"], p["old"]) for p in got[0]["items"]], [("B-1", True), ("B-3", False), ("B-9", False)])
        self.assertEqual([(p["item"], p["old"]) for p in got[1]["items"]], [("B-2", False)])
        self.assertEqual(js("parkedGroups(items, 0)", []), [])

    def test_parked_is_derived_from_the_backlog_view(self):
        entry = {"repo": "alpha", "fetched_at": NOW, "error": None, "items": [
            {"id": "B-1", "t": "one", "p": 1, "s": "open"},
            {"id": "B-2", "t": "two", "p": None, "s": "parked", "q": "Q-7", "parked_at": "2026-10-01T00:00:00Z",
             "parked_at_from": "status", "branch": None, "session": None}]}
        got = js("hubState(items)", snapshot(views={"backlog": [entry]}))
        self.assertEqual([b["id"] for b in got["backlog"]], ["alpha:B-1", "alpha:B-2"])
        self.assertEqual(len(got["parked"]), 1)
        p = got["parked"][0]
        self.assertEqual((p["repo"], p["item"], p["title"], p["q"], p["parked_at"], p["parked_at_from"], p["branch"], p["session"]),
                         ("alpha", "B-2", "two", "Q-7", "2026-10-01T00:00:00Z", "status", None, None))


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class StateTest(unittest.TestCase):
    def test_an_unavailable_view_keeps_its_reason_and_the_rest_still_loads(self):
        snap = snapshot(unavailable={"backlog": "no clone yet"},
                        collections={"todos": {"T-001": {"status": "open", "title": "x", "from": "you"}}},
                        views={"backlog": None, "usage": {"available": False, "reason": "no reading"},
                               "agents": {"available": False, "reason": "no agents folder"}})
        got = js("hubState(items)", snap)
        self.assertEqual(got["gone"], {"backlog": "no clone yet", "usage": "no reading", "agents": "no agents folder"})
        self.assertEqual((got["backlog"], got["parked"], got["usage"], got["agents"]), ([], [], [], []))
        self.assertEqual([t["id"] for t in got["todos"]], ["T-001"])

    def test_a_repository_that_could_not_be_read_is_named(self):
        got = js("hubState(items)", snapshot(views={"backlog": [
            {"repo": "alpha", "fetched_at": NOW, "items": [{"id": "B-1", "t": "one", "s": "open"}]},
            {"repo": "beta", "items": [], "error": "no clone"}]}))
        self.assertEqual(got["partial"]["backlog"], ["beta: no clone"])
        self.assertEqual([b["id"] for b in got["backlog"]], ["alpha:B-1"])
        self.assertEqual(got["gone"], {})

    def test_an_empty_or_odd_snapshot_is_empty_lists(self):
        for snap in ({}, None, snapshot(views={"usage": [], "agents": [], "backlog": "x"}, collections={"todos": []})):
            got = js("hubState(items)", snap)
            for k in ("docs", "inbox", "todos", "stories", "topm", "repos", "backlog", "completed", "parked", "usage", "agents",
                      "pages"):
                self.assertEqual(got[k], [], (k, snap))
            self.assertEqual(got["pageHtml"], {}, snap)
        self.assertEqual(js("hubState(items).revision", {}), 0)
        odd = js("hubState(items)", snapshot(collections={"pages": ["x"], "page_html": "y"}))
        self.assertEqual((odd["pages"], odd["pageHtml"]), ([], {}))

    def test_only_pending_approvals_oldest_first_and_strong_only_when_true(self):
        got = js("hubState(items)", snapshot(strong=True, collections={"approvals": {
            "A-2": {"status": "pending", "title": "later", "created_at": "2026-10-02T00:00:00+00:00"},
            "A-1": {"status": "pending", "title": "first", "created_at": "2026-10-01T00:00:00+00:00"},
            "A-3": {"status": "approved", "title": "decided", "created_at": "2026-09-30T00:00:00+00:00"}}}))
        self.assertEqual([(a["id"], a["title"]) for a in got["approvals"]], [("A-1", "first"), ("A-2", "later")])
        self.assertIs(got["strong"], True)
        for snap in ({}, None, snapshot(strong="yes"), snapshot(strong=1)):
            got = js("hubState(items)", snap)
            self.assertEqual((got["approvals"], got["strong"]), ([], False), snap)

    def test_pages_newest_first_with_their_bodies(self):
        got = js("hubState(items)", snapshot(collections={
            "pages": {"old": {"title": "Old", "at": "2026-10-01T00:00:00Z"},
                      "new": {"title": "New", "at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-05T00:00:00Z"},
                      "mid": {"title": "Mid", "at": "2026-10-03T00:00:00Z"},
                      "bare": {"title": "No body", "at": "2026-10-02T00:00:00Z"}},
            "page_html": {"old": {"html": "<p>old</p>"}, "new": {"html": "<p>new</p>"}, "mid": {"html": "<p>mid</p>"},
                          "stray": {"html": "<p>no page</p>"}}}))
        self.assertEqual([p["id"] for p in got["pages"]], ["new", "mid", "bare", "old"])
        # A page with no body is still listed; a body with no page is not kept.
        self.assertEqual(got["pageHtml"], {"old": "<p>old</p>", "new": "<p>new</p>", "mid": "<p>mid</p>"})

    def test_page_fields_are_read_as_text(self):
        got = js("hubState(items)", snapshot(collections={
            "pages": {"p": {"title": ["x"], "repo": {"a": 1}, "from": 7, "summary": None, "refs": ["Q-1", 2, {"x": 1}, "T-3"],
                            "at": False},
                      "q": {"title": "Q", "refs": "Q-1"}},
            "page_html": {"p": {"html": 5}, "q": {"html": "<p>q</p>"}}}))
        p = {x["id"]: x for x in got["pages"]}
        self.assertEqual((p["p"]["title"], p["p"]["repo"], p["p"]["from"], p["p"]["summary"], p["p"]["at"]), ("", "", "7", "", ""))
        self.assertEqual(p["p"]["refs"], ["Q-1", "T-3"])
        self.assertEqual(p["q"]["refs"], [])
        self.assertEqual(got["pageHtml"], {"q": "<p>q</p>"})              # an html that is not a string is no body

    def test_usage_and_agents_views(self):
        got = js("hubState(items)", snapshot(views={
            "usage": {"available": True, "as_of": NOW, "accounts": {"one": {"five": 12, "week": 95}}},
            "agents": {"available": True, "agents": [{"name": "worker", "description": "builds", "model": "m", "effort": "-"}]}}))
        self.assertEqual(got["usage"], [{"id": "one", "account": "one", "five_hour": 12, "seven_day": 95, "measured_at": NOW}])
        self.assertEqual([(a["id"], a["role"], a["model"], a["effort"]) for a in got["agents"]], [("worker", "builds", "m", "")])

    def test_the_pm_line_is_information(self):
        seen = lambda **kw: dict({"waited": None, "pulled": None, "waiting": 0}, **kw)   # noqa: E731
        say = lambda s: js("pmSeenText(items, %s)" % NOW_MS, s)                           # noqa: E731
        self.assertEqual(say(seen(waiting=1, waited="2026-10-07T11:00:00Z")), "A PM is listening.")
        self.assertIn("No PM is listening", say(seen()))
        self.assertIn("since the hub server started", say(seen()))
        self.assertEqual(say(seen(waited="2026-10-07T11:30:00Z")), "No PM is listening; one last asked 30 min ago.")
        self.assertEqual(say(seen(waited="2026-10-01T00:00:00Z", pulled="2026-10-07T09:00:00Z")),
                         "No PM is listening; one last asked 3 h ago.")
        self.assertEqual(say(None), "")

    def test_retry_honours_retry_after_and_backs_off(self):
        self.assertEqual(js("hubRetryMs(1, items)", 7), 7000)
        self.assertEqual(js("hubRetryMs(9, items)", 1), 1000)
        self.assertEqual(js("hubRetryMs(1, items)", 100000), 300000)
        got = js("[1, 2, 3, 4, 5, 6, 7, 30].map(n => hubRetryMs(n, items))", 0)
        self.assertEqual(got, [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000])
        self.assertEqual(js("hubRetryMs(1, undefined)", None), 2000)

    def test_a_retry_after_under_a_second_is_a_second(self):
        self.assertEqual(js("hubRetryMs(1, items)", 0.001), 1000)
        self.assertEqual(js("hubRetryMs(1, items)", 0.5), 1000)
        self.assertEqual(js("hubRetryMs(1, items)", 1.5), 1500)

    def test_an_unpaired_browser_reads_again_slowly_and_never_gives_up(self):
        # A refusal at a hub server restart must not end the page's reading for good, nor make it a tight loop.
        got = js("[1, 2, 6, 30].map(n => hubRetryMs(n, items, true))", 0)
        self.assertEqual(got, [45000, 45000, 45000, 45000])
        self.assertEqual(js("hubRetryMs(1, items, true)", 1), 45000)            # a short Retry-After does not speed it up
        self.assertEqual(js("hubRetryMs(1, items, true)", 120), 120000)         # a longer one is honoured
        self.assertEqual(js("hubRetryMs(1, items, false)", 0), 2000)            # a paired browser's failures are unchanged


    def test_a_repository_showing_an_earlier_read_is_named_as_stale(self):
        got = js("hubState(items)", snapshot(views={"backlog": [
            {"repo": "alpha", "fetched_at": NOW, "items": [{"id": "B-1", "t": "one", "s": "open"}],
             "error": None, "stale": True, "fetch_error": "git fetch failed"},
            {"repo": "beta", "fetched_at": NOW, "items": [], "error": None, "stale": False, "fetch_error": None}]}))
        self.assertEqual(got["stale"], {"backlog": [{"repo": "alpha", "reason": "git fetch failed", "since": NOW}]})
        self.assertEqual(got["partial"], {})
        self.assertEqual([b["id"] for b in got["backlog"]], ["alpha:B-1"])


def mark(name):
    """A server value that would be markup, and would run, if the page ever read it as HTML."""
    return '<img src=x onerror=alert(1)><script>alert("%s")</script>' % name


def dom(body, snap, hash=""):
    return pagedom.run(NODE, page(), body, snap, hash)


def conflict(what):
    return {"status": 409, "body": {"ok": False, "error": {"code": "conflict", "message": what, "field": ""}}}


def record(**kw):
    """What GET /records/alpha/B-1 answers (kitlib.hubsite.backlog read_record), with `kw` over it."""
    rec = {"repo": "alpha", "id": "B-1", "version": "c1:s1", "where": "open", "readable": True,
           "fields": [["id", "B-1"], ["summary", "one"], ["scope", "tools/x.py"], ["effort", "s"]], "body": "Old body.\n",
           "blockers": [], "hold": {"owner_ask": None, "hold_ruling": None, "q": None}, "branch": None, "removed": None}
    rec.update(kw)
    return rec


LISTS = {
    "to_pm": {"P-1": {"status": "queued", "kind": "question", "text": "first", "version": 1, "created_at": NOW},
              "P-2": {"status": "queued", "kind": "fyi", "text": "second", "version": 1, "created_at": NOW}},
    "todos": {"T-1": {"status": "open", "title": "mine", "detail": "d", "from": "you", "version": 1, "added_at": NOW},
              "T-2": {"status": "open", "title": "theirs", "from": "pm", "version": 1, "added_at": NOW}},
    "questions": {"Q-1": {"status": "open", "title": "which", "context": "as asked", "version": 1, "asked_at": NOW,
                          "options": [{"label": "one"}, {"label": "two"}], "recommended": 0}},
}
BACKLOG = [{"repo": "alpha", "fetched_at": NOW, "error": None, "stale": False, "fetch_error": None,
            "items": [{"id": "B-1", "t": "one", "p": 1, "s": "open"}]}]


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class DomTest(unittest.TestCase):
    """The page's script run against a stand-in DOM (tests/hubsite/pagedom.py): what it draws and sends."""

    def test_server_text_with_markup_is_drawn_as_text(self):
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        names = ("q-title", "q-context", "q-from", "q-label", "q-description", "q-ref", "i-title", "i-text", "i-asked",
                 "p-text", "t-title", "t-detail", "t-command", "s-title", "s-story", "s-note", "b-title", "b-error",
                 "c-title", "c-stale", "k-branch", "gone", "account", "repo", "d-title", "d-summary", "d-from", "d-repo",
                 "d-ref", "r-field", "r-ask", "r-body", "r-branch")
        m = {n: mark(n) for n in names}
        rec = record(fields=[["id", "B-1"], ["summary", m["r-field"]], ["owner_ask", m["r-ask"]]], body=m["r-body"],
                     hold={"owner_ask": m["r-ask"], "hold_ruling": None, "q": None},
                     branch={"name": m["r-branch"], "source": "pulled_by", "exists": False, "error": None})
        snap = snapshot(
            unavailable={"agents": m["gone"]},
            collections={
                "questions": {"Q-1": {"status": "open", "title": m["q-title"], "context": m["q-context"], "from": m["q-from"],
                                      "options": [{"label": m["q-label"], "description": m["q-description"]}],
                                      "refs": [m["q-ref"]], "version": 1}},
                "inbox": {"I-1": {"title": m["i-title"], "text": m["i-text"], "asked": m["i-asked"], "read": False, "at": day}},
                "to_pm": {"P-1": {"status": "queued", "text": m["p-text"], "version": 1}},
                "todos": {"T-1": {"status": "open", "title": m["t-title"], "detail": m["t-detail"], "from": "pm",
                                  "commands": [m["t-command"]]}},
                "stories": {"S-1": {"status": "review", "title": m["s-title"], "story": m["s-story"], "review_note": m["s-note"]}},
                # The list of documents is text like every other card; only the reader shows a body (DocsTest).
                "pages": {"plan-1": {"title": m["d-title"], "summary": m["d-summary"], "from": m["d-from"], "repo": m["d-repo"],
                                     "refs": [m["d-ref"]], "at": day}},
                "page_html": {"plan-1": {"html": mark("d-body")}}},
            views={
                "repos": [{"name": m["repo"], "aliases": []}],
                "backlog": [{"repo": "alpha", "fetched_at": day, "items": [
                    {"id": "B-1", "t": m["b-title"], "p": 1, "s": "open"},
                    {"id": "B-2", "t": "parked one", "p": 2, "s": "parked", "parked_at": day, "branch": m["k-branch"]}]},
                    {"repo": "beta", "items": [], "error": m["b-error"]}],
                "completed": [{"repo": "alpha", "fetched_at": day, "days": 30, "stale": True, "fetch_error": m["c-stale"],
                               "items": [{"id": "B-0", "t": m["c-title"], "d": day}]}],
                "usage": {"available": True, "as_of": day, "accounts": {m["account"]: {"five": 12, "week": 95}}}})
        got = dom("""
            await start();
            let shown = text(body);
            const see = async sel => { await click($(sel)); shown += "\\n" + text(body); };
            await see("#qtabs [data-qt=inbox]");
            for (const s of ["topm", "todo", "docs", "info"]) await see("#sections [data-s=" + s + "]");
            await see("#ftabs [data-nf=agents]");
            await see("#sections [data-s=backlog]");
            for (const f of ["current", "parked", "done"]) await see("#stabs [data-sf=" + f + "]");
            server.route = c => c.path === "/records/alpha/B-1" ? { status: 200, body: REC } : undefined;
            await click($("#stabs [data-sf=current]")); await see("#list button.link");
            const risky = ["href", "src", "srcdoc", "srcset", "action", "formaction", "style"];
            const attrs = all().flatMap(e => Object.keys(e.attrs).filter(k => k.startsWith("on") || risky.includes(k)).map(k => e.tag + " " + k));
            return { shown, tags: [...new Set(created)], sinks, attrs, err: text($("#loaderr")) };
        """.replace("REC", json.dumps(rec)), snap)
        for name in names:
            self.assertIn(m[name], got["shown"], name)
        self.assertNotIn(mark("d-body"), got["shown"])                       # a body is never drawn into the page
        self.assertEqual(got["sinks"], [])
        self.assertEqual(got["attrs"], [])
        self.assertEqual(got["err"], "")
        made = set(got["tags"])
        self.assertIn("article", made)
        self.assertEqual(made & {"img", "script", "a", "iframe", "object", "embed", "link", "style", "base", "meta", "svg", "form"},
                         set())

    def test_an_unpaired_browser_is_told_to_pair_again_and_reads_again_slowly_until_it_is(self):
        # The server answers a call that carries no session 403, naming the field `role`; it sends no 401.
        # A refusal at a hub server restart must not leave the page dead until it is reloaded: it reads again
        # every 45 s, says so, and carries on by itself once the server accepts the session.
        for status, field in ((403, "role"), (403, ""), (401, "")):
            for where in ("snapshot", "wait"):
                got = dom("""
                    const SNAP = %s;
                    const no = { status: %d, body: { ok: false, error: { code: "forbidden", message: "this request carries no owner or agent credential", field: %s } } };
                    let refuse = SNAP;
                    server.route = c => refuse && c.path === "/snapshot" ? no : undefined;
                    const reads = () => calls.filter(c => c.path === "/snapshot").length;
                    const now = () => ({ err: text($("#loaderr")), hidden: $("#loaderr").hidden, reads: reads(), sleeps: sleeps(), waiting: waits.length });
                    await start();
                    if (!SNAP) { refuse = true; await answerWait(no.status, no.body); }
                    const first = now();
                    await fireTimers();
                    const again = now();                  // still refused: read once more, and wait as long again
                    refuse = false;
                    await fireTimers();
                    return { first, again, healed: now(), list: text($("#list")) };
                """ % (json.dumps(where == "snapshot"), status, json.dumps(field)),
                    snapshot(collections={"todos": {"T-1": {"status": "open", "title": "kept on the page", "from": "pm"}}}),
                    hash="#todo")
                with self.subTest(status=status, field=field, where=where):
                    first = got["first"]
                    self.assertIn("not paired with the hub (yet, or any more: it was unpaired, or its session ran out)", first["err"])
                    self.assertNotIn("restarted", first["err"])
                    self.assertIn("`python tools/hub.py open`", first["err"])
                    self.assertIn("in the kit folder", first["err"])
                    self.assertNotIn("no longer paired", first["err"])
                    self.assertNotIn("Trying again", first["err"])
                    self.assertNotIn("stopped reading", first["err"])
                    self.assertIn("every 45 s", first["err"])
                    self.assertFalse(first["hidden"])
                    self.assertEqual(first["sleeps"], [45000])                   # a slow retry, not a tight loop
                    self.assertEqual(first["waiting"], 0)
                    self.assertEqual(got["again"]["reads"], first["reads"] + 1)
                    self.assertIn("not paired with the hub", got["again"]["err"])
                    self.assertEqual(got["again"]["sleeps"], [45000])
                    self.assertEqual((got["healed"]["err"], got["healed"]["hidden"]), ("", True))
                    self.assertEqual(got["healed"]["waiting"], 1)                # reading the hub server again
                    self.assertIn("kept on the page", got["list"])
                    if where == "wait":
                        self.assertIn("may be out of date", first["err"])

    def test_a_list_that_cannot_be_drawn_leaves_the_page_not_ready_and_says_why(self):
        # A throw in render() on the first read: the counts stay drawn and the error is said at the top, but the
        # page is not marked ready, so its controls stay hidden and the next snapshot is drawn even when its
        # revision is lower, as after a hub server restart. Without that, the late-answer check drops it and the
        # page says "Waiting for the hub server" with no line at the top.
        got = dom("""
            let armed = false, fixed = false;
            const L = $("#list"), put = L.insertBefore.bind(L);
            L.insertBefore = (n, r) => { if (armed) throw new Error("drawing failed"); return put(n, r); };
            server.snap.revision = 9;
            server.route = c => { if (c.path === "/snapshot" && !fixed) armed = true; return undefined; };
            await start();
            const broken = { err: text($("#loaderr")), hidden: $("#loaderr").hidden, n: text($("#n-t")), add: $("#addform").hidden, sleeps: sleeps() };
            armed = false; fixed = true; server.snap.revision = 1;
            await fireTimers();
            return { broken, err: text($("#loaderr")), hidden: $("#loaderr").hidden, list: text($("#list")), add: $("#addform").hidden };
        """, snapshot(collections={"todos": {"T-1": {"status": "open", "title": "drawn at last", "from": "pm"}}}), hash="#todo")
        self.assertIn("could not show what the hub server sent (drawing failed)", got["broken"]["err"])
        self.assertFalse(got["broken"]["hidden"])
        self.assertEqual(got["broken"]["n"], "1")                                # the counts are drawn
        self.assertTrue(got["broken"]["add"])                                    # not ready: no controls
        self.assertEqual(got["broken"]["sleeps"], [2000])
        self.assertIn("drawn at last", got["list"])
        self.assertEqual((got["err"], got["hidden"], got["add"]), ("", True, False))

    def test_a_tunnel_access_refusal_is_not_unpaired_and_keeps_the_servers_words(self):
        # The tunnel listener refuses a call whose Cloudflare Access token it cannot accept with 403, field
        # `access` (kitlib.hubsite.tunnel.REFUSED): a read or a write, and pairing again cannot help it.
        words = "this listener needs a valid Cloudflare Access token"
        for where in ("snapshot", "wait"):
            got = dom("""
                const no = { status: 403, body: { ok: false, error: { code: "forbidden", message: %s, field: "access" } } };
                if (%s) server.route = c => c.path === "/snapshot" ? no : undefined;
                await start();
                if (!%s) await answerWait(no.status, no.body);
                return { err: text($("#loaderr")), hidden: $("#loaderr").hidden, sleeps: sleeps() };
            """ % (json.dumps(words), json.dumps(where == "snapshot"), json.dumps(where == "snapshot")),
                snapshot(collections={"todos": {"T-1": {"status": "open", "title": "kept on the page", "from": "pm"}}}),
                hash="#todo")
            with self.subTest(where=where):
                self.assertNotIn("not paired", got["err"])
                self.assertNotIn("hub.py open", got["err"])
                self.assertIn("refused the read (" + words + ")", got["err"])
                self.assertIn("Trying again", got["err"])
                self.assertFalse(got["hidden"])
                self.assertEqual(len(got["sleeps"]), 1)                  # it is tried again, not given up
        wrote = dom("""
            server.route = c => c.method !== "POST" ? undefined
              : { status: 403, body: { ok: false, error: { code: "forbidden", message: %s, field: "access" } } };
            await start();
            await type($("#pm-fields textarea"), "kept text");
            await fire($("#pmform"), "submit");
            return { said: text($("#pm-addstatus")), kept: $("#pm-fields textarea").value };
        """ % json.dumps(words), snapshot(collections=LISTS), hash="#topm")
        self.assertIn(words, wrote["said"])
        self.assertNotIn("not paired", wrote["said"])
        self.assertEqual(wrote["kept"], "kept text")

    def test_a_busy_server_is_given_its_wait_and_only_no_answer_reads_cannot_reach(self):
        got = dom("""
            let mode = "busy";
            const busy = { status: 429, body: { ok: false, error: { code: "busy", message: "too many requests", field: "" } }, headers: { "Retry-After": "7" } };
            server.route = c => c.path !== "/snapshot" ? undefined : mode === "busy" ? busy : mode === "down" ? new Error("connection refused") : undefined;
            const now = () => ({ err: text($("#loaderr")), hidden: $("#loaderr").hidden, sleeps: sleeps(), calls: calls.length });
            await start();
            const a = now();
            mode = "down"; await fireTimers();
            const b = now();
            mode = "ok"; await fireTimers();
            const ok = now();
            // A long-poll the server refuses, then a server that answers again: the notice goes without a change to wait for.
            await answerWait(500, { ok: false, error: { code: "internal", message: "store unreadable", field: "" } });
            const refused = now();
            await fireTimers();
            return { busy: a, down: b, ok, refused, back: now(), list: text($("#list")) };
        """, snapshot(collections={"todos": {"T-1": {"status": "open", "title": "read at last", "from": "pm"}}}), hash="#todo")
        self.assertIn("busy", got["busy"]["err"])
        self.assertIn("7 s", got["busy"]["err"])
        self.assertNotIn("Cannot reach", got["busy"]["err"])
        self.assertEqual(got["busy"]["sleeps"], [7000])
        self.assertEqual(got["busy"]["calls"], 1)
        self.assertTrue(got["down"]["err"].startswith("Cannot reach the hub server"), got["down"]["err"])
        self.assertIn("Trying again", got["down"]["err"])
        self.assertEqual(got["down"]["sleeps"], [4000])                          # the second failure in a row
        self.assertTrue(got["ok"]["hidden"])
        self.assertEqual(got["ok"]["err"], "")
        self.assertIn("read at last", got["list"])
        self.assertIn("refused the read (store unreadable)", got["refused"]["err"])
        self.assertIn("may be out of date", got["refused"]["err"])
        self.assertNotIn("Cannot reach", got["refused"]["err"])
        self.assertEqual((got["back"]["err"], got["back"]["hidden"]), ("", True))

    def test_an_open_edit_keeps_what_was_typed_through_a_refresh(self):
        got = dom("""
            await start();
            await click(button($("#pm-P-1"), "Edit"));
            const ta = $("#pm-P-1 textarea");
            await type(ta, "half written");
            const pm = () => ({ kept: $("#pm-P-1 textarea") && $("#pm-P-1 textarea").value, p2: text($("#pm-P-2")),
              focus: document.activeElement === ta && ta.isConnected, caret: ta.selectionStart });
            // Another item changes while the field does not have the focus.
            server.snap.collections.to_pm["P-2"].text = "changed by an agent"; server.snap.revision++;
            await wake();
            const other = pm();
            // Only the time of a backlog read moves.
            server.snap.views.backlog[0].fetched_at = "2026-10-07T12:05:00Z"; server.snap.revision++;
            await wake();
            const moved = pm();
            // Another item changes while the owner is in the field.
            ta.focus(); ta.setSelectionRange(4, 4);
            server.snap.collections.to_pm["P-2"].text = "changed again"; server.snap.revision++;
            await wake();
            const focused = pm();
            // The same for a to-do of the owner's own, and the edit is still there on coming back to its section.
            await click($("#sections [data-s=todo]"));
            await click(button($("#todo-T-1"), "Edit"));
            await type($("#todo-T-1 input[type=text]"), "a new title, half");
            server.snap.collections.todos["T-2"].title = "changed too"; server.snap.revision++;
            await wake();
            const todo = { kept: $("#todo-T-1 input[type=text]") && $("#todo-T-1 input[type=text]").value, t2: text($("#todo-T-2")) };
            await click($("#sections [data-s=topm]"));
            return { other, moved, focused, todo, back: pm().kept, err: text($("#loaderr")) };
        """, snapshot(collections=LISTS, views={"backlog": BACKLOG}), hash="#topm")
        self.assertEqual(got["err"], "")
        self.assertEqual(got["other"]["kept"], "half written")
        self.assertIn("changed by an agent", got["other"]["p2"])                 # the list was redrawn around the edit
        self.assertEqual(got["moved"]["kept"], "half written")
        self.assertEqual(got["focused"]["kept"], "half written")
        self.assertIn("changed again", got["focused"]["p2"])
        self.assertTrue(got["focused"]["focus"])
        self.assertEqual(got["focused"]["caret"], 4)
        self.assertEqual(got["todo"]["kept"], "a new title, half")
        self.assertIn("changed too", got["todo"]["t2"])
        self.assertEqual(got["back"], "half written")

    def test_after_a_conflict_an_edit_takes_the_new_version(self):
        for hash_, card, field, coll, path in (("#topm", "#pm-P-1", "textarea", "to_pm", "/to_pm/P-1/edit"),
                                               ("#todo", "#todo-T-1", "input[type=text]", "todos", "/todos/T-1/edit")):
            got = dom("""
                const CARD = %s, FIELD = CARD + " " + %s, COLL = %s, ID = CARD.slice(-3), PATH = %s;
                await start();
                await click(button($(CARD), "Edit"));
                await type($(FIELD), "my edit");
                let n = 0;
                server.route = c => {
                  if (c.path !== PATH) return undefined;
                  const d = server.snap.collections[COLL][ID]; server.snap.revision++;
                  if (++n === 1) { d.version = 3; d.text = d.title = "theirs"; return %s; }
                  d.version = 4; d.text = d.title = "my edit"; return { status: 200, body: { ok: true } };
                };
                await click(button($(CARD), "Save"));
                const refused = { status: text($(CARD + " .status")), kept: $(FIELD) && $(FIELD).value };
                await click(button($(CARD), "Save"));
                return { refused, sent: calls.filter(c => c.path === PATH).map(c => c.body.if_version), open: !!$(FIELD), after: text($(CARD)) };
            """ % (json.dumps(card), json.dumps(field), json.dumps(coll), json.dumps(path),
                   json.dumps(conflict("%s is at version 3, not 1" % coll))),
                snapshot(collections=LISTS), hash=hash_)
            with self.subTest(card=card):
                self.assertIn("changed on the hub server", got["refused"]["status"])
                self.assertEqual(got["refused"]["kept"], "my edit")
                self.assertEqual(got["sent"], [1, 3])
                self.assertFalse(got["open"])
                self.assertIn("my edit", got["after"])

    def test_after_a_conflict_an_answer_takes_the_new_version(self):
        got = dom("""
            await start();
            const pick = $("#q-Q-1-1"); pick.checked = true; await fire(pick, "change");
            let n = 0;
            server.route = c => {
              if (c.path !== "/questions/Q-1/answer") return undefined;
              const d = server.snap.collections.questions["Q-1"]; server.snap.revision++;
              if (++n === 1) { d.version = 2; d.context = "asked again, differently"; return %s; }
              d.version = 3; d.status = "answered"; d.answer = { choice: 1, label: "two", note: "" }; return { status: 200, body: { ok: true } };
            };
            await click(button($("#q-Q-1"), "Send answer"));
            const refused = { card: text($("#q-Q-1")), picked: $("#q-Q-1-1").checked };
            await click(button($("#q-Q-1"), "Send answer"));
            await click($("#tabs [data-f=answered]"));
            return { refused, sent: calls.filter(c => c.path === "/questions/Q-1/answer").map(c => [c.body.choice, c.body.if_version]), after: text($("#q-Q-1")) };
        """ % json.dumps(conflict("questions/Q-1 is at version 2, not 1")), snapshot(collections=LISTS))
        self.assertIn("changed on the hub server", got["refused"]["card"])
        self.assertIn("asked again, differently", got["refused"]["card"])
        self.assertTrue(got["refused"]["picked"])
        self.assertEqual(got["sent"], [[1, 1], [1, 2]])
        self.assertIn("Your answer: two", got["after"])

    def test_one_card_that_cannot_be_drawn_does_not_take_the_page_with_it(self):
        lists = {"questions": {"Q-1": {"status": "open", "title": "broken", "options": [None, {"label": "a"}], "version": 1},
                               "Q-2": {"status": "open", "title": "whole", "options": [{"label": "b"}], "version": 1}},
                 "todos": {"T-1": {"status": "open", "title": "a step", "from": "pm"}}}
        got = dom("""
            await start();
            const q = { cards: $$("#list article").map(a => text(a)), err: text($("#loaderr")), hidden: $("#loaderr").hidden, n: text($("#n-qq")) };
            await click($("#sections [data-s=todo]"));
            return { q, todo: text($("#list")), sleeps: sleeps() };
        """, snapshot(collections=lists))
        self.assertEqual(len(got["q"]["cards"]), 2)
        bad, good = got["q"]["cards"]
        self.assertTrue(bad.startswith("Q-1"), bad)
        self.assertIn("cannot be shown", bad)
        self.assertIn("whole", good)
        self.assertIn("b", good)
        self.assertEqual((got["q"]["err"], got["q"]["hidden"], got["q"]["n"]), ("", True, "2"))
        self.assertIn("a step", got["todo"])
        self.assertEqual(got["sleeps"], [])                                      # nothing failed, so nothing is retried

    def test_a_backlog_showing_an_earlier_read_says_so(self):
        entry = lambda repo, **kw: dict({"repo": repo, "fetched_at": NOW, "error": None, "stale": False,      # noqa: E731
                                         "fetch_error": None, "days": 30,
                                         "items": [{"id": "B-1", "t": "item of " + repo, "p": 1, "s": "parked", "d": NOW}]}, **kw)
        views = {"backlog": [entry("alpha", stale=True, fetch_error="git fetch failed"), entry("beta")],
                 "completed": [entry("alpha"), entry("beta", stale=True, error="no answer from the remote")]}
        got = dom("""
            await start();
            await click($("#stabs [data-sf=current]"));
            const current = text($("#list"));
            await click($("#stabs [data-sf=done]"));
            const done = $$("#list .gone").map(text);
            await click($("#stabs [data-sf=parked]"));
            return { current, done, parked: $$("#list .gone").map(text) };
        """, snapshot(views=views), hash="#backlog")
        self.assertIn("Out of date: the alpha part of the backlog view could not be read again (git fetch failed).", got["current"])
        # The time is the server's last fetch of the repository, and is said as that, not as the time of the read shown.
        self.assertIn("Its items are those of an earlier read; the hub server last fetched that repository ", got["current"])
        self.assertNotIn("last read", got["current"])
        self.assertIn("item of alpha", got["current"])                           # the last good items are still shown
        self.assertNotIn("the beta part of the backlog", got["current"])
        self.assertEqual(len(got["done"]), 1)
        self.assertIn("Out of date: the beta part of the completed view could not be read again (no answer from the remote).",
                      got["done"][0])
        self.assertEqual(len(got["parked"]), 1)
        self.assertIn("the alpha part of the backlog view", got["parked"][0])

    def test_a_wait_that_keeps_failing_backs_off_though_the_snapshot_answers(self):
        got = dom("""
            const no = () => answerWait(500, { ok: false, error: { code: "internal", message: "store unreadable", field: "" } });
            await start();
            const seen = [];
            for (let i = 0; i < 3; i++) { await no(); seen.push(sleeps()); await fireTimers(); }
            const reads = calls.filter(c => c.path === "/snapshot").length;
            await wake();                                   // a wait that answers ends the run of failures
            await no();
            return { seen, reads, after: sleeps() };
        """, snapshot())
        self.assertEqual(got["seen"], [[2000], [4000], [8000]])
        self.assertEqual(got["reads"], 4)                                        # the first read and one after each failure
        self.assertEqual(got["after"], [2000])

    def test_a_wait_that_says_nothing_is_ended_by_the_page(self):
        got = dom("""
            await start();
            const before = { waiting: waits.length, limits: timers.map(t => t.ms) };
            await fireTimers();                             // the 90 s the page gives a wait
            return { before, waiting: waits.length, err: text($("#loaderr")), sleeps: sleeps() };
        """, snapshot())
        self.assertEqual(got["before"], {"waiting": 1, "limits": [90000]})
        self.assertEqual(got["waiting"], 0)
        self.assertTrue(got["err"].startswith("Cannot reach the hub server"), got["err"])
        self.assertEqual(got["sleeps"], [2000])

    def test_an_answer_cut_off_in_its_body_reads_cannot_reach(self):
        got = dom("""
            let cut = true;
            server.route = c => c.path === "/snapshot" && cut ? { status: 200, body: null, cut: true } : undefined;
            await start();
            const first = { err: text($("#loaderr")), sleeps: sleeps() };
            cut = false; await fireTimers();
            return { first, err: text($("#loaderr")), list: text($("#list")) };
        """, snapshot(collections={"todos": {"T-1": {"status": "open", "title": "read at last", "from": "pm"}}}), hash="#todo")
        self.assertTrue(got["first"]["err"].startswith("Cannot reach the hub server"), got["first"]["err"])
        self.assertNotIn("refused", got["first"]["err"])
        self.assertEqual(got["first"]["sleeps"], [2000])
        self.assertEqual(got["err"], "")
        self.assertIn("read at last", got["list"])

    def test_an_unpaired_write_is_told_to_pair_again_and_another_refusal_keeps_the_servers_words(self):
        got = dom("""
            let field = "origin";
            const words = { origin: "the Origin header is not the hub server's own", role: "this request carries no owner or agent credential" };
            server.route = c => c.method !== "POST" ? undefined
              : { status: 403, body: { ok: false, error: { code: "forbidden", message: words[field], field } } };
            await start();
            await type($("#pm-fields textarea"), "kept text");
            await fire($("#pmform"), "submit");
            const origin = text($("#pm-addstatus"));
            field = "role";
            await fire($("#pmform"), "submit");
            return { origin, role: text($("#pm-addstatus")), kept: $("#pm-fields textarea").value, err: text($("#loaderr")) };
        """, snapshot(collections=LISTS), hash="#topm")
        self.assertIn("the Origin header is not the hub server's own", got["origin"])
        self.assertNotIn("not paired", got["origin"])
        self.assertIn("not paired with the hub", got["role"])
        self.assertIn("`python tools/hub.py open`", got["role"])
        self.assertNotIn("carries no owner", got["role"])
        self.assertEqual(got["kept"], "kept text")
        self.assertEqual(got["err"], "")

    def test_send_all_waits_for_an_open_edit(self):
        got = dom("""
            await start();
            await click(button($("#pm-P-1"), "Edit"));
            await type($("#pm-P-1 textarea"), "half written");
            const sends = () => calls.filter(c => c.path === "/to_pm/send").length;
            await click($("#pm-sendall"));
            const held = { say: text($("#pm-sendstatus")), sends: sends(), kept: $("#pm-P-1 textarea") && $("#pm-P-1 textarea").value };
            await click(button($("#pm-P-1"), "Cancel"));
            await click($("#pm-sendall"));
            return { held, sends: sends(), say: text($("#pm-sendstatus")) };
        """, snapshot(collections=LISTS), hash="#topm")
        self.assertEqual(got["held"]["say"], "Not sent: save or cancel the open edit first.")
        self.assertEqual(got["held"]["sends"], 0)
        self.assertEqual(got["held"]["kept"], "half written")
        self.assertEqual(got["sends"], 1)
        self.assertNotIn("open edit", got["say"])

    def test_an_edit_whose_item_is_gone_keeps_its_card_and_what_was_typed(self):
        got = dom("""
            await start();
            const lost = id => { const c = $(id); return c && { note: text(c.querySelector(".gone")), shown: !c.querySelector(".gone").hidden,
              fields: c.querySelectorAll("textarea, input[type=text]").map(f => f.value), save: !!button(c, "Save"), dismiss: !!button(c, "Dismiss") }; };
            await click(button($("#pm-P-1"), "Edit"));
            await type($("#pm-P-1 textarea"), "half written");
            // The item is sent meanwhile, in another tab.
            server.snap.collections.to_pm["P-1"].status = "sent"; server.snap.revision++;
            await wake();
            const pm = lost("#lost-pm-P-1");
            await click($("#pm-sendall"));                  // an edit that is gone does not hold Send all back
            const sends = calls.filter(c => c.path === "/to_pm/send").length;
            await click($("#pmtabs [data-pf=sent]"));
            const sent = { lost: !!$("#lost-pm-P-1"), own: text($("#pm-P-1")) };
            await click($("#pmtabs [data-pf=queued]"));
            const back = lost("#lost-pm-P-1");
            await click(button($("#lost-pm-P-1"), "Dismiss"));
            const dismissed = !$("#lost-pm-P-1") && $$("#list textarea").length === 0;
            // A to-do of the owner's own is deleted meanwhile.
            await click($("#sections [data-s=todo]"));
            await click(button($("#todo-T-1"), "Edit"));
            await type($("#todo-T-1 input[type=text]"), "a new title, half");
            await type($("#todo-T-1 textarea"), "and its detail");
            delete server.snap.collections.todos["T-1"]; server.snap.revision++;
            await wake();
            const todo = lost("#lost-todo-T-1");
            await click($("#ttabs [data-tf=done]"));
            const onDone = !!$("#lost-todo-T-1");
            await click(button($("#lost-todo-T-1"), "Dismiss"));
            return { pm, sends, sent, back, dismissed, todo, onDone, left: !!$("#lost-todo-T-1"), err: text($("#loaderr")) };
        """, snapshot(collections=LISTS), hash="#topm")
        notice = "This item is gone; your text is below, copy it."
        for card, fields in ((got["pm"], ["half written"]), (got["back"], ["half written"]),
                             (got["todo"], ["a new title, half", "and its detail"])):
            self.assertIsNotNone(card)
            self.assertIn(notice, card["note"])
            self.assertTrue(card["shown"])
            self.assertEqual(card["fields"], fields)
            self.assertEqual((card["save"], card["dismiss"]), (False, True))
        self.assertEqual(got["sends"], 1)
        self.assertFalse(got["sent"]["lost"])
        self.assertIn("first", got["sent"]["own"])                               # the sent item has its own card, the server's text
        self.assertTrue(got["dismissed"])
        self.assertTrue(got["onDone"])
        self.assertFalse(got["left"])
        self.assertEqual(got["err"], "")

    def test_a_save_on_an_item_that_is_gone_says_so_and_keeps_the_text(self):
        got = dom("""
            await start();
            await click(button($("#pm-P-1"), "Edit"));
            await type($("#pm-P-1 textarea"), "my edit");
            server.route = c => {
              if (c.path !== "/to_pm/P-1/edit") return undefined;
              server.snap.collections.to_pm["P-1"].status = "sent"; server.snap.revision++;
              return CONFLICT;
            };
            await click(button($("#pm-P-1"), "Save"));
            const c = $("#lost-pm-P-1");
            return { card: text(c), kept: c && c.querySelector("textarea").value, save: !!button(c, "Save"), dismiss: !!button(c, "Dismiss") };
        """.replace("CONFLICT", json.dumps(conflict("to_pm/P-1 is sent, not queued"))), snapshot(collections=LISTS), hash="#topm")
        self.assertIn("This item is gone; your text is below, copy it.", got["card"])
        self.assertNotIn("press Save again", got["card"])
        self.assertEqual(got["kept"], "my edit")
        self.assertEqual((got["save"], got["dismiss"]), (False, True))

    def test_a_conflict_shows_what_the_server_holds_now_as_text(self):
        theirs = mark("theirs")
        for hash_, card, field, coll, path in (("#topm", "#pm-P-1", "textarea", "to_pm", "/to_pm/P-1/edit"),
                                               ("#todo", "#todo-T-1", "input[type=text]", "todos", "/todos/T-1/edit")):
            got = dom("""
                const CARD = %s, FIELD = CARD + " " + %s, COLL = %s, ID = CARD.slice(-3), PATH = %s, THEIRS = %s;
                await start();
                await click(button($(CARD), "Edit"));
                await type($(FIELD), "my edit");
                const quiet = !$(CARD).textContent.includes("The hub server now holds:");
                let n = 0;
                server.route = c => {
                  if (c.path !== PATH) return undefined;
                  const d = server.snap.collections[COLL][ID]; server.snap.revision++;
                  if (++n === 1) { d.version = 3; d.text = d.title = THEIRS; d.detail = "their detail"; return %s; }
                  d.version = 4; d.text = d.title = "my edit"; return { status: 200, body: { ok: true } };
                };
                created.length = 0;
                await click(button($(CARD), "Save"));
                const refused = { card: text($(CARD)), status: text($(CARD + " .status")), kept: $(FIELD) && $(FIELD).value, made: [...new Set(created)] };
                await click(button($(CARD), "Save"));
                return { quiet, refused, sinks, sent: calls.filter(c => c.path === PATH).map(c => [c.body.if_version, c.body.text || c.body.title]), open: !!$(FIELD) };
            """ % (json.dumps(card), json.dumps(field), json.dumps(coll), json.dumps(path), json.dumps(theirs),
                   json.dumps(conflict("%s is at version 3, not 1" % coll))),
                snapshot(collections=LISTS), hash=hash_)
            with self.subTest(card=card):
                self.assertTrue(got["quiet"])
                self.assertIn("The hub server now holds:" + theirs, got["refused"]["card"])
                if coll == "todos":
                    self.assertIn("their detail", got["refused"]["card"])
                self.assertIn("changed on the hub server", got["refused"]["status"])
                self.assertEqual(got["refused"]["kept"], "my edit")
                self.assertEqual(got["sinks"], [])
                self.assertEqual(set(got["refused"]["made"]) & {"img", "script"}, set())
                self.assertEqual(got["sent"], [[1, "my edit"], [3, "my edit"]])
                self.assertFalse(got["open"])

    def test_a_conflict_whose_reread_fails_does_not_show_the_old_text_as_the_servers(self):
        down = {"status": 500, "body": {"ok": False, "error": {"code": "internal", "message": "store unreadable", "field": ""}}}
        for hash_, card, field, coll, path in (("#topm", "#pm-P-1", "textarea", "to_pm", "/to_pm/P-1/edit"),
                                               ("#todo", "#todo-T-1", "input[type=text]", "todos", "/todos/T-1/edit")):
            got = dom("""
                const CARD = %s, FIELD = CARD + " " + %s, COLL = %s, ID = CARD.slice(-3), PATH = %s;
                await start();
                await click(button($(CARD), "Edit"));
                await type($(FIELD), "my edit");
                let n = 0, down = true;
                server.route = c => {
                  if (c.path === "/snapshot" && down) return %s;
                  if (c.path !== PATH) return undefined;
                  const d = server.snap.collections[COLL][ID];
                  if (++n <= 2) { d.version = 3; d.text = d.title = "theirs"; server.snap.revision = 4; return %s; }
                  d.version = 4; d.text = d.title = "my edit"; server.snap.revision = 5; return { status: 200, body: { ok: true } };
                };
                const see = () => ({ card: text($(CARD)), status: text($(CARD + " .status")), kept: $(FIELD) && $(FIELD).value });
                await click(button($(CARD), "Save"));
                const unread = see();
                down = false;
                await click(button($(CARD), "Save"));
                const read = see();
                await click(button($(CARD), "Save"));
                return { unread, read, sent: calls.filter(c => c.path === PATH).map(c => c.body.if_version), open: !!$(FIELD) };
            """ % (json.dumps(card), json.dumps(field), json.dumps(coll), json.dumps(path), json.dumps(down),
                   json.dumps(conflict("%s is at version 3, not 1" % coll))),
                snapshot(collections=LISTS), hash=hash_)
            with self.subTest(card=card):
                self.assertEqual(got["unread"]["status"], "Not saved: this item changed on the hub server and could not be "
                                 "read again. Your text is kept; press Save again.")
                self.assertNotIn("The hub server now holds:", got["unread"]["card"])
                self.assertEqual(got["unread"]["kept"], "my edit")
                self.assertIn("The hub server now holds:theirs", got["read"]["card"])
                self.assertEqual(got["read"]["kept"], "my edit")
                self.assertEqual(got["sent"], [1, 1, 3])                         # the version moves only on a read that came
                self.assertFalse(got["open"])

    def test_every_card_shows_its_id_and_the_id_box_finds_it(self):
        snap = snapshot(collections=dict(LISTS, stories={"S-1": {"status": "review", "title": "a story", "story": "x"}}))
        got = dom("""
            await start();
            const ids = {};
            const idsIn = sel => [...document.querySelectorAll(sel + " .id")].map(e => text(e));
            ids.questions = idsIn("#list article");
            await click($("#sections [data-s=todo]")); ids.todos = idsIn("#list article");
            await type($("#idq"), "t-2");
            const found = [...document.querySelectorAll("#list article")].map(a => a.id);
            await type($("#idq"), "zzz");
            const none = text($("#list"));
            await click($("#sections [data-s=questions]"));
            const noneQ = text($("#list"));
            await type($("#idq"), "");
            await click($("#sections [data-s=todo]")); await type($("#idq"), "");
            await click($("#sections [data-s=backlog]")); ids.stories = idsIn("#list article");
            return { ids, found, none, noneQ };
        """, snap)
        self.assertEqual(got["ids"], {"questions": ["Q-1"], "todos": ["T-1", "T-2"], "stories": ["S-1"]})
        self.assertEqual(got["found"], ["todo-T-2"])
        self.assertIn('No match for "zzz"', got["none"])
        self.assertNotIn("Nothing to do", got["none"])
        self.assertIn('No match for "zzz"', got["noneQ"])
        self.assertNotIn("No open questions", got["noneQ"])

    def test_a_submitted_story_is_withdrawn_from_its_card_on_current_backlog(self):
        stories = {"S-1": {"status": "submitted", "title": "mine", "story": "x", "repo": "alpha", "version": 1},
                   "S-2": {"status": "questions", "title": "asked", "story": "x", "repo": "alpha", "version": 1},
                   "S-3": {"status": "added", "title": "filed", "story": "x", "repo": "alpha", "version": 1}}
        got = dom("""
            await start();
            const out = { n: text($("#n-s")), hot: $("#n-s").classList.contains("hot") };
            await click($("#sections [data-s=backlog]"));
            out.cards = $$("#list article").map(a => a.attrs.id);
            out.only = ["S-2", "S-3"].every(s => !button($("#story-" + s), "Withdraw"));
            server.route = c => c.path === "/stories/S-1/withdraw" && c.method === "POST" ? { status: 409, body: { ok: false, error: { code: "invalid", message: "not submitted", field: "status" } } } : undefined;
            await click(button($("#story-S-1"), "Withdraw"));
            out.refused = text($("#story-S-1 .act .status"));
            server.route = null;
            await click(button($("#story-S-1"), "Withdraw"));
            out.sent = calls.filter(c => c.path.endsWith("/withdraw")).map(c => c.method + " " + c.path);
            return out;
        """, snapshot(collections={"stories": stories}, views={"repos": [{"name": "alpha"}], "backlog": BACKLOG}))
        self.assertEqual((got["n"], got["hot"]), ("2", True))           # the Backlog button counts stories in progress
        self.assertEqual(got["cards"], ["story-S-2", "story-S-1", "story-S-3"])
        self.assertTrue(got["only"])                                       # only a submitted story can be withdrawn
        self.assertEqual(got["refused"], "Not saved: not submitted.")
        self.assertEqual(got["sent"], ["POST /stories/S-1/withdraw"] * 2)

    def test_a_follow_up_question_and_its_story_jump_to_each_other(self):
        lists = {"stories": {"S-1": {"status": "questions", "title": "mine", "story": "x", "repo": "alpha", "version": 1}},
                 "questions": {"Q-1": dict(LISTS["questions"]["Q-1"], story="S-1")}}
        got = dom("""
            await start();
            const chip = button($("#q-Q-1"), "story: ");
            const out = { chip: text(chip) };
            await click(chip);
            out.story = { section: $("#sec-backlog").getAttribute("aria-selected"), tab: $("#stab-current").getAttribute("aria-selected"),
                          card: !!$("#story-S-1") };
            await click(button($("#story-S-1"), "Q-1"));
            out.back = { section: $("#sec-questions").getAttribute("aria-selected"), card: !!$("#q-Q-1") };
            return out;
        """, snapshot(collections=lists, views={"repos": [{"name": "alpha"}], "backlog": BACKLOG}))
        self.assertEqual(got["chip"], "story: mine")
        self.assertEqual(got["story"], {"section": "true", "tab": "true", "card": True})
        self.assertEqual(got["back"], {"section": "true", "card": True})                 # the card's Follow-ups button

    def test_a_lost_to_do_card_has_a_tick_box_that_says_the_to_do_is_gone(self):
        got = dom("""
            await start();
            await click(button($("#todo-T-1"), "Edit"));
            await type($("#todo-T-1 input[type=text]"), "half");
            const box = () => { const b = $("#list article input.chk"); return { id: b.id, label: b.getAttribute("aria-label"), off: b.disabled }; };
            const before = box();
            delete server.snap.collections.todos["T-1"]; server.snap.revision++;
            await wake();
            const c = $("#lost-todo-T-1");
            return { before, after: box(), boxes: c.querySelectorAll("input.chk").length, kept: c.querySelector("input[type=text]").value };
        """, snapshot(collections=LISTS), hash="#todo")
        self.assertEqual(got["before"], {"id": "t-T-1", "label": "Save or cancel the edit before ticking this", "off": True})
        self.assertEqual(got["after"], {"id": "", "label": "This to-do is gone", "off": True})
        self.assertEqual(got["boxes"], 1)
        self.assertEqual(got["kept"], "half")

    def test_an_open_to_do_edit_stays_in_sight_when_its_to_do_leaves_the_tab_or_the_filter(self):
        lists = {"todos": {"T-1": {"status": "open", "title": "mine", "from": "you", "repo": "alpha", "version": 1, "added_at": NOW},
                           "T-2": {"status": "open", "title": "other", "from": "pm", "repo": "alpha", "version": 1, "added_at": NOW}}}
        got = dom("""
            await start();
            await click(button($("#todo-T-1"), "Edit"));
            await type($("#todo-T-1 input[type=text]"), "half written");
            const see = () => ({ kept: $("#todo-T-1 input[type=text]") ? $("#todo-T-1 input[type=text]").value : null, other: !!$("#todo-T-2"), lost: !!$("#lost-todo-T-1") });
            // Another repository is chosen in the filter.
            $("#repo").value = "beta"; await fire($("#repo"), "change");
            const filtered = see();
            $("#repo").value = ""; await fire($("#repo"), "change");
            // The to-do is ticked done meanwhile, in another tab.
            server.snap.collections.todos["T-1"].status = "done"; server.snap.revision++;
            await wake();
            const ticked = see();
            await click($("#ttabs [data-tf=done]"));
            const onDone = see();
            await click(button($("#todo-T-1"), "Cancel"));
            await click($("#ttabs [data-tf=open]"));
            return { filtered, ticked, onDone, after: !!$("#todo-T-1") };
        """, snapshot(collections=lists, views={"repos": [{"name": "alpha"}, {"name": "beta"}]}), hash="#todo")
        self.assertEqual(got["filtered"], {"kept": "half written", "other": False, "lost": False})
        self.assertEqual(got["ticked"], {"kept": "half written", "other": True, "lost": False})
        self.assertEqual(got["onDone"], {"kept": "half written", "other": False, "lost": False})
        self.assertFalse(got["after"])                                           # the edit over, the to-do is on its own tab only

    def test_a_write_whose_answer_is_cut_off_in_its_body_says_not_added_and_keeps_the_text(self):
        got = dom("""
            server.route = c => c.method === "POST" ? { status: 200, body: null, cut: true } : undefined;
            await start();
            await type($("#pm-fields textarea"), "kept text");
            await fire($("#pmform"), "submit");
            return { say: text($("#pm-addstatus")), kept: $("#pm-fields textarea").value };
        """, snapshot(collections=LISTS), hash="#topm")
        self.assertEqual(got["say"], "Not added: the hub server did not answer. Your text is kept.")
        self.assertEqual(got["kept"], "kept text")

    def test_the_to_pm_repo_menu_lists_the_repos_without_being_clicked(self):
        got = dom("""
            await start();
            return $("#pm-fields select").options.map(o => [o.value, text(o)]);
        """, snapshot(collections=LISTS, views={"repos": [{"name": "beta"}, {"name": "alpha"}]}), hash="#topm")
        self.assertEqual(got, [["", "No repo"], ["alpha", "alpha"], ["beta", "beta"]])

    def test_a_queued_note_says_it_is_saved_and_not_yet_sent(self):
        got = dom("""
            await start();
            await type($("#pm-fields textarea"), "a question");
            await fire($("#pmform"), "submit");
            return { say: text($("#pm-addstatus")), sent: calls.filter(c => c.path === "/to_pm").map(c => c.body) };
        """, snapshot(collections=LISTS), hash="#topm")
        self.assertEqual(got["say"], "Saved as EX-1. It is not sent yet: press Send all.")
        self.assertEqual(got["sent"], [{"kind": "question", "repo": "", "urgent": False, "text": "a question"}])

    def test_send_all_with_no_pm_listening_says_the_message_is_saved_for_when_one_connects(self):
        body = """
            server.route = c => c.path === "/to_pm/send" ? { status: 200, body: { ok: true, sent: ["P-1", "P-2"] } } : undefined;
            await start();
            await click($("#pm-sendall"));
            return text($("#pm-sendstatus"));
        """
        self.assertEqual(dom(body, snapshot(collections=LISTS), hash="#topm"),
                         "Sent 2. No PM is listening now: your message is saved and the PM gets it when one connects.")
        listening = snapshot(collections=LISTS, pm_seen={"waited": NOW, "pulled": None, "waiting": 1})
        self.assertEqual(dom(body, listening, hash="#topm"), "Sent 2. A PM is listening.")

    def test_send_all_says_a_pm_is_listening_when_the_send_itself_ended_the_pms_wait(self):
        got = dom("""
            server.route = c => {
                if (c.path !== "/to_pm/send") return undefined;
                server.snap.pm_seen = { waited: server.snap.pm_seen.waited, pulled: null, waiting: 0 };
                server.snap.revision++;
                return { status: 200, body: { ok: true, sent: ["P-1", "P-2"] } };
            };
            await start();
            await click($("#pm-sendall"));
            return { say: text($("#pm-sendstatus")), snapshots: calls.filter(c => c.path === "/snapshot").length };
        """, snapshot(collections=LISTS, pm_seen={"waited": NOW, "pulled": None, "waiting": 1}), hash="#topm")
        self.assertEqual(got["say"], "Sent 2. A PM is listening.")
        self.assertGreater(got["snapshots"], 1, "the page read the snapshot again after the send")

    def test_an_empty_to_pm_queue_says_to_add_first(self):
        hint = "Nothing is queued. Write your message below and press Add, then Send all."
        got = dom("""
            await start();
            const empty = { say: text($("#pm-sendstatus")), off: $("#pm-sendall").disabled };
            server.snap.collections.to_pm = { "P-1": { status: "queued", kind: "question", text: "first", version: 1 } }; server.snap.revision++;
            await wake();
            const queued = { say: text($("#pm-sendstatus")), off: $("#pm-sendall").disabled };
            server.route = c => c.path === "/to_pm/send" ? { status: 200, body: { ok: true, sent: ["P-1"] } } : undefined;
            await click($("#pm-sendall"));
            server.snap.collections.to_pm["P-1"].status = "sent"; server.snap.revision++;
            await wake();
            return { empty, queued, after: text($("#pm-sendstatus")) };
        """, snapshot(collections={"to_pm": {}}), hash="#topm")
        self.assertEqual(got["empty"], {"say": hint, "off": True})
        self.assertEqual(got["queued"], {"say": "", "off": False})
        self.assertTrue(got["after"].startswith("Sent 1."), got["after"])        # the hint never replaces what a send said

    def test_the_usage_chip_names_the_account_and_window_of_its_figure(self):
        def chip(accounts):
            return dom("""
                await start();
                return { chip: text($("#n-u")), hot: $("#n-u").classList.contains("hot") };
            """, snapshot(views={"usage": {"available": True, "as_of": NOW, "accounts": accounts}}), hash="#usage")
        # The highest figure of any account and either window, with whose it is and which window.
        self.assertEqual(chip({"1": {"five": 0, "week": 99}, "2": {"five": 0, "week": 99}, "3": {"five": 39, "week": 90}}),
                         {"chip": "99% wk · acct 1", "hot": True})
        self.assertEqual(chip({"1": {"five": 12, "week": 40}, "2": {"five": 97, "week": 41}}),
                         {"chip": "97% 5h · acct 2", "hot": True})
        self.assertEqual(chip({"team": {"five": 5, "week": 50}}), {"chip": "50% wk · team", "hot": False})
        self.assertEqual(chip({}), {"chip": "–", "hot": False})

    def test_parked_is_a_tab_under_backlog_and_its_count_shows_on_the_backlog_section(self):
        item = lambda i, at: {"id": "B-%d" % i, "t": "parked %d" % i, "p": 1, "s": "parked", "parked_at": at}      # noqa: E731
        entry = lambda repo, items: {"repo": repo, "fetched_at": NOW, "error": None, "stale": False,               # noqa: E731
                                     "fetch_error": None, "items": items}
        views = {"repos": [{"name": "alpha"}, {"name": "beta"}],
                 "backlog": [entry("alpha", [item(1, NOW), item(2, NOW), {"id": "B-9", "t": "open one", "p": 1, "s": "open"}]),
                             entry("beta", [item(3, NOW)])]}
        got = dom("""
            await start();
            const on = () => ({ say: text($("#n-sp")), off: $("#n-sp").hidden, hot: $("#n-sp").classList.contains("hot") });
            const out = { sections: $$("#sections .sec").map(b => b.dataset.s), first: on(), badge: $("#n-sp").parentNode.attrs.id };
            await click($("#sections [data-s=backlog]"));
            await click($("#stabs [data-sf=parked]"));
            out.tab = { chosen: $("#stab-parked").getAttribute("aria-selected"), n: text($("#n-p")),
                        section: $("#sec-backlog").getAttribute("aria-selected"), row: $("#stabs").hidden };
            out.cards = $$("#list .pk").map(c => c.attrs.id);
            $("#brepo").value = "beta"; await fire($("#brepo"), "change");
            out.beta = $$("#list .pk").map(c => c.attrs.id);
            server.snap.views.backlog = []; server.snap.revision++;
            await wake();
            out.none = on(); out.empty = text($("#list"));
            return out;
        """, snapshot(views=views))
        self.assertEqual(got["sections"], ["questions", "topm", "todo", "backlog", "docs", "info"])
        self.assertEqual(got["first"], {"say": "3 parked", "off": False, "hot": True})       # seen from any section
        self.assertEqual(got["badge"], "sec-backlog")
        self.assertEqual(got["tab"], {"chosen": "true", "n": "3", "section": "true", "row": False})
        self.assertEqual(got["cards"], ["pk-alpha:B-1", "pk-alpha:B-2", "pk-beta:B-3"])
        self.assertEqual(got["beta"], ["pk-beta:B-3"])                                     # the Backlog row's own picker filters it
        self.assertTrue(got["none"]["off"])
        self.assertIn("Nothing parked", got["empty"])

    def test_the_state_switch_sits_in_the_heading_row_and_still_selects(self):
        got = dom("""
            await start();
            const cls = id => $(id).attrs.class, picked = id => $$(id + " .tab").filter(t => t.getAttribute("aria-selected") === "true").map(t => t.dataset.f || t.dataset.if);
            const out = { row: [$("#qtabs").parentNode.attrs.id, $("#tabs").parentNode.attrs.id, $("#itabs").parentNode.attrs.id],
                          look: [cls("#qtabs"), cls("#tabs"), cls("#itabs")], open: picked("#tabs") };
            await click($("#tab-answered"));
            out.submitted = picked("#tabs"); out.shown = $$("#list .q").length;
            await click($("#qtab-answers"));
            out.hides = [$("#tabs").hidden, $("#itabs").hidden]; out.unread = picked("#itabs");
            await click($("#itab-read"));
            out.read = picked("#itabs");
            return out;
        """, snapshot(collections=LISTS))
        self.assertEqual(got["row"], ["qbar", "qbar", "qbar"])       # what is shown at the left, its state at the right of the same row
        self.assertEqual(got["look"], ["tabs", "tabs sw", "tabs sw"])
        self.assertEqual((got["open"], got["submitted"], got["shown"]), (["open"], ["answered"], 0))
        self.assertEqual((got["hides"], got["unread"], got["read"]), ([True, False], ["unread"], ["read"]))

    def test_cancel_forgets_what_a_refused_answer_said(self):
        lists = {"questions": {"Q-1": {"status": "answered", "title": "which", "version": 2, "asked_at": NOW,
                                       "options": [{"label": "one"}, {"label": "two"}],
                                       "answer": {"choice": 0, "label": "one", "note": "", "at": NOW}}}}
        got = dom("""
            await start();
            await click($("#tabs [data-f=answered]"));
            await click(button($("#q-Q-1"), "Change answer"));
            server.route = c => {
              if (c.path !== "/questions/Q-1/answer") return undefined;
              server.snap.collections.questions["Q-1"].version = 3; server.snap.revision++;
              return CONFLICT;
            };
            await click(button($("#q-Q-1"), "Save new answer"));
            const refused = text($("#q-Q-1"));
            await click(button($("#q-Q-1"), "Cancel"));
            await click(button($("#q-Q-1"), "Change answer"));
            return { refused, again: text($("#q-Q-1")), open: !!button($("#q-Q-1"), "Save new answer") };
        """.replace("CONFLICT", json.dumps(conflict("questions/Q-1 is at version 3, not 2"))), snapshot(collections=lists))
        self.assertIn("changed on the hub server", got["refused"])
        self.assertTrue(got["open"])
        self.assertNotIn("changed on the hub server", got["again"])

    DOWN = {"status": 500, "body": {"ok": False, "error": {"code": "internal", "message": "store unreadable", "field": ""}}}

    def test_an_answer_that_was_sent_and_not_read_back_says_it_was_sent_and_is_not_sent_twice(self):
        answered = {"Q-1": dict(LISTS["questions"]["Q-1"], status="answered", version=2,
                                answer={"choice": 0, "label": "one", "note": "", "at": NOW})}
        for name, lists, before, send in (("open", LISTS, "", "Send answer"),
                                          ("answered", dict(LISTS, questions=answered),
                                           'await click($("#tabs [data-f=answered]")); await click(button($("#q-Q-1"), "Change answer"));',
                                           "Save new answer")):
            got = dom("""
                await start();
                BEFORE
                const pick = $("#q-Q-1-1"); pick.checked = true; await fire(pick, "change");
                let down = true;
                server.route = c => {
                  if (c.path === "/snapshot" && down) return DOWN;
                  if (c.path !== "/questions/Q-1/answer") return undefined;
                  const d = server.snap.collections.questions["Q-1"]; server.snap.revision++;
                  d.version++; d.status = "answered"; d.answer = { choice: 1, label: "two", note: "" };
                  return { status: 200, body: { ok: true } };
                };
                await click(button($("#q-Q-1"), SEND));
                const unread = { card: text($("#q-Q-1")), send: !!button($("#q-Q-1"), SEND), change: !!button($("#q-Q-1"), "Change answer"),
                                 radios: $$("#q-Q-1 input").length };
                down = false;
                await wake();
                await click($("#tabs [data-f=answered]"));
                return { unread, sent: calls.filter(c => c.path === "/questions/Q-1/answer").length, after: text($("#q-Q-1")) };
            """.replace("BEFORE", before).replace("DOWN", json.dumps(self.DOWN)).replace("SEND", json.dumps(send)),
                snapshot(collections=lists))
            with self.subTest(card=name):
                self.assertIn("Your answer was sent.", got["unread"]["card"])
                self.assertIn("could not be read again", got["unread"]["card"])
                self.assertNotIn("Your answer: ", got["unread"]["card"])           # not the answer from before it either
                # nothing on the card sends again: the server would refuse a second send of an old version
                self.assertEqual((got["unread"]["send"], got["unread"]["change"], got["unread"]["radios"]), (False, False, 0))
                self.assertEqual(got["sent"], 1)
                self.assertIn("Your answer: two", got["after"])                    # the read that came shows the answer
                self.assertNotIn("Your answer was sent.", got["after"])

    def test_a_refused_answer_whose_reread_fails_does_not_point_at_the_old_question_as_the_new_one(self):
        got = dom("""
            await start();
            const pick = $("#q-Q-1-1"); pick.checked = true; await fire(pick, "change");
            let n = 0, down = true;
            server.route = c => {
              if (c.path === "/snapshot" && down) return DOWN;
              if (c.path !== "/questions/Q-1/answer") return undefined;
              const d = server.snap.collections.questions["Q-1"];
              if (++n <= 2) { d.version = 2; d.context = "asked again, differently"; server.snap.revision = 4; return CONFLICT; }
              d.version = 3; d.status = "answered"; d.answer = { choice: 1, label: "two", note: "" }; server.snap.revision = 5;
              return { status: 200, body: { ok: true } };
            };
            const see = () => ({ card: text($("#q-Q-1")), status: text($("#q-Q-1 .status")), picked: $("#q-Q-1-1").checked,
                                 send: !!button($("#q-Q-1"), "Send answer") });
            await click(button($("#q-Q-1"), "Send answer"));
            const unread = see();
            await click(button($("#q-Q-1"), "Send answer"));
            const again = see();
            down = false;
            await wake();
            const read = see();
            await click(button($("#q-Q-1"), "Send answer"));
            await click($("#tabs [data-f=answered]"));
            return { unread, again, read, sent: calls.filter(c => c.path === "/questions/Q-1/answer").map(c => [c.body.choice, c.body.if_version]),
                     after: text($("#q-Q-1")) };
        """.replace("DOWN", json.dumps(self.DOWN)).replace("CONFLICT", json.dumps(conflict("questions/Q-1 is at version 2, not 1"))),
            snapshot(collections=LISTS))
        for seen in (got["unread"], got["again"]):
            self.assertTrue(seen["status"].startswith("Not sent: "), seen["status"])
            self.assertIn("could not be read again", seen["status"])
            self.assertNotIn("as it is now", seen["card"])                         # the card is still the question as it was
            self.assertIn("as asked", seen["card"])
            self.assertNotIn("asked again, differently", seen["card"])
            self.assertIn("Your draft is kept", seen["status"])
            self.assertEqual((seen["picked"], seen["send"]), (True, True))
        # once a read comes, the card is the changed question and the line is the one about it
        self.assertIn("asked again, differently", got["read"]["card"])
        self.assertIn("check it against the question as it is now", got["read"]["status"])
        self.assertNotIn("could not be read again", got["read"]["card"])
        self.assertTrue(got["read"]["picked"])
        self.assertEqual(got["sent"], [[1, 1], [1, 1], [1, 2]])                    # the version moves only on a read that came
        self.assertIn("Your answer: two", got["after"])

    def test_the_backlog_tab_is_kept_over_a_reload(self):
        views = {"repos": [{"name": "alpha"}], "backlog": []}
        stored = dom("""
            await start();
            await click($("#sections [data-s=backlog]"));
            const first = $$("#stabs .tab").filter(t => t.getAttribute("aria-selected") === "true").map(t => t.dataset.sf);
            await click($("#stabs [data-sf=parked]"));
            return { first, section: localStorage.getItem("oq.section"), tab: localStorage.getItem("oq.sfilter") };
        """, snapshot(views=views))
        self.assertEqual(stored, {"first": ["current"], "section": "backlog", "tab": "parked"})

        def reload(keys, hash_=""):
            return dom("""
                for (const [k, v] of Object.entries(KEYS)) localStorage.setItem(k, v);
                await start();
                return { section: $$("#sections .sec").filter(b => b.getAttribute("aria-selected") === "true").map(b => b.dataset.s),
                         tab: $$("#stabs .tab").filter(t => t.getAttribute("aria-selected") === "true").map(t => t.dataset.sf),
                         row: $("#stabs").hidden, list: text($("#list")) };
            """.replace("KEYS", json.dumps(keys)), snapshot(views=views), hash=hash_)
        # the page as the first run left it: the reload is on Backlog > Parked, not on Current backlog
        got = reload({"oq.section": stored["section"], "oq.sfilter": stored["tab"]})
        self.assertEqual((got["section"], got["tab"], got["row"]), (["backlog"], ["parked"], False))
        self.assertIn("Nothing parked", got["list"])
        for tab in ("current", "done"):
            with self.subTest(tab=tab):
                got = reload({"oq.section": "backlog", "oq.sfilter": tab})
                self.assertEqual((got["section"], got["tab"]), (["backlog"], [tab]))
        # a link to Parked beats the stored tab; a stored value that is no tab is not taken
        self.assertEqual(reload({"oq.section": "backlog", "oq.sfilter": "done"}, "#parked")["tab"], ["parked"])
        self.assertEqual(reload({"oq.section": "backlog", "oq.sfilter": "nowhere"})["tab"], ["current"])
        # the retired New story tab, stored by an older page, opens Current backlog
        self.assertEqual(reload({"oq.section": "backlog", "oq.sfilter": "new"})["tab"], ["current"])

    def test_a_section_stored_as_parked_by_an_older_page_still_opens_backlog_parked(self):
        views = {"repos": [{"name": "alpha"}], "backlog": []}

        def reload(keys):
            return dom("""
                for (const [k, v] of Object.entries(KEYS)) localStorage.setItem(k, v);
                await start();
                return { section: $$("#sections .sec").filter(b => b.getAttribute("aria-selected") === "true").map(b => b.dataset.s),
                         tab: $$("#stabs .tab").filter(t => t.getAttribute("aria-selected") === "true").map(t => t.dataset.sf),
                         list: text($("#list")) };
            """.replace("KEYS", json.dumps(keys)), snapshot(views=views))
        got = reload({"oq.section": "parked"})
        self.assertEqual((got["section"], got["tab"]), (["backlog"], ["parked"]))
        self.assertIn("Nothing parked", got["list"])
        # a tab chosen since, on the page as it is now, is the later word
        self.assertEqual(reload({"oq.section": "parked", "oq.sfilter": "done"})["tab"], ["done"])

    def test_a_usage_tie_goes_to_the_weekly_window_whichever_account_has_it(self):
        def chip(accounts):
            return dom("""
                await start();
                return text($("#n-u"));
            """, snapshot(views={"usage": {"available": True, "as_of": NOW, "accounts": accounts}}), hash="#usage")
        # the first account's 5 h window ties with a later account's week: the week is the one named
        self.assertEqual(chip({"1": {"five": 99, "week": 10}, "2": {"five": 0, "week": 99}}), "99% wk · acct 2")
        self.assertEqual(chip({"1": {"five": 50, "week": 10}, "2": {"five": 50, "week": 10}, "3": {"five": 3, "week": 50}}),
                         "50% wk · acct 3")
        # two weeks tie: the first account by name; two 5 h windows tie with no week beside them: the first too
        self.assertEqual(chip({"2": {"five": 99, "week": 80}, "1": {"five": 80, "week": 80}, "3": {"five": 0, "week": 99}}),
                         "99% wk · acct 3")
        self.assertEqual(chip({"1": {"five": 0, "week": 70}, "2": {"five": 70, "week": 70}}), "70% wk · acct 1")
        self.assertEqual(chip({"1": {"five": 70, "week": 1}, "2": {"five": 70, "week": 2}}), "70% 5h · acct 1")
        self.assertEqual(chip({"1": {"five": 0, "week": 0}, "2": {"five": 0, "week": 0}}), "0% wk · acct 1")

    def test_a_stale_repository_whose_fetch_time_moves_does_not_redraw_under_a_typing_owner(self):
        stale = [dict(BACKLOG[0], stale=True, error="the local read failed")]
        got = dom("""
            await start();
            await click(button($("#pm-P-1"), "Edit"));
            const ta = $("#pm-P-1 textarea");
            await type(ta, "half written"); ta.focus();
            const before = $("#pm-P-2");
            server.snap.views.backlog[0].fetched_at = "2026-10-07T12:05:00Z"; server.snap.revision++;
            await wake();
            const still = $("#pm-P-2") === before;
            // A real change is still drawn.
            server.snap.views.backlog[0].error = "another reason"; server.snap.revision++;
            await wake();
            return { still, redrawn: $("#pm-P-2") !== before, kept: ta.value, focus: document.activeElement === ta };
        """, snapshot(collections=LISTS, views={"backlog": stale}), hash="#topm")
        self.assertTrue(got["still"])
        self.assertTrue(got["redrawn"])
        self.assertEqual(got["kept"], "half written")
        self.assertTrue(got["focus"])

    # A time the browser cannot format: the one thing a card of these kinds reads that can throw.
    UNSHOWABLE = """
        const realTime = Date.prototype.toLocaleString;
        Date.prototype.toLocaleString = function(...a){ if (this.getUTCFullYear() === 1999) throw new Error("a time that cannot be shown"); return realTime.apply(this, a); };
        const cards = () => $$("#list article").map(text);
    """
    BAD_TIME = "1999-01-01T00:00:00Z"

    def one_bad_one_good(self, cards, bad, good):
        self.assertEqual(len(cards), 2, cards)
        broken = [c for c in cards if "cannot be shown" in c]
        self.assertEqual(len(broken), 1, cards)
        self.assertTrue(broken[0].startswith(bad), broken[0])
        self.assertTrue(any(good in c and "cannot be shown" not in c for c in cards), cards)

    def test_a_to_pm_to_do_story_or_inbox_card_that_cannot_be_drawn_stands_alone(self):
        bad = self.BAD_TIME
        lists = {"to_pm": {"P-1": {"status": "queued", "text": "pm bad", "version": 1, "created_at": bad},
                           "P-2": {"status": "queued", "text": "pm good", "version": 1, "created_at": NOW}},
                 "todos": {"T-1": {"status": "open", "title": "todo bad", "from": "pm", "added_at": bad},
                           "T-2": {"status": "open", "title": "todo good", "from": "pm", "added_at": NOW}},
                 "stories": {"S-1": {"status": "submitted", "title": "story bad", "story": "x", "submitted_at": bad},
                             "S-2": {"status": "submitted", "title": "story good", "story": "x", "submitted_at": NOW}},
                 "inbox": {"I-1": {"title": "inbox bad", "text": "x", "read": False, "at": bad},
                           "I-2": {"title": "inbox good", "text": "x", "read": False, "at": NOW}}}
        got = dom(self.UNSHOWABLE + """
            await start();
            const out = { topm: cards() };
            await click($("#sections [data-s=todo]")); out.todo = cards();
            await click($("#qtabs [data-qt=inbox]")); out.inbox = cards();
            await click($("#sections [data-s=backlog]")); await click($("#stabs [data-sf=current]")); out.story = cards();
            out.err = text($("#loaderr")); out.sleeps = sleeps();
            return out;
        """, snapshot(collections=lists), hash="#topm")
        self.one_bad_one_good(got["topm"], "P-1", "pm good")
        self.one_bad_one_good(got["todo"], "T-1", "todo good")
        self.one_bad_one_good(got["inbox"], "I-1", "inbox good")
        self.one_bad_one_good(got["story"], "S-1", "story good")
        self.assertEqual((got["err"], got["sleeps"]), ("", []))

    def test_a_parked_or_usage_card_that_cannot_be_drawn_stands_alone(self):
        views = {"backlog": [{"repo": "alpha", "fetched_at": NOW, "error": None, "stale": False, "fetch_error": None, "items": [
                     {"id": "B-1", "t": "parked bad", "p": 1, "s": "parked", "parked_at": self.BAD_TIME},
                     {"id": "B-2", "t": "parked good", "p": 1, "s": "parked", "parked_at": NOW}]}],
                 "usage": {"available": True, "as_of": self.BAD_TIME, "accounts": {"one": {"five": 12, "week": 95}}}}
        got = dom(self.UNSHOWABLE + """
            await start();
            const out = { parked: cards() };
            await click($("#sections [data-s=info]")); await click($("#ftabs [data-nf=usage]"));
            out.usage = cards(); out.note = text($("#list .unote"));
            await click($("#ftabs [data-nf=agents]"));
            out.err = text($("#loaderr")); out.sleeps = sleeps();
            return out;
        """, snapshot(views=views), hash="#parked")
        self.one_bad_one_good(got["parked"], "alpha:B-1", "parked good")
        self.assertEqual(len(got["usage"]), 1)
        self.assertTrue(got["usage"][0].startswith("one"), got["usage"])
        self.assertIn("cannot be shown", got["usage"][0])
        self.assertIn("Read on this machine", got["note"])                       # the rest of the view is drawn
        self.assertEqual((got["err"], got["sleeps"]), ("", []))

    # A pending approval as kitlib.hubsite.api's add route stores it, diff lines and all (kitlib.hubsite.approvals.diff).
    APPROVALS = {
        "A-1": {"status": "pending", "repo": "github.com/team/alpha", "title": "Raise the cap", "head": "a" * 40,
                "reason": "the cap is too low", "version": 2, "created_at": NOW,
                "files": [{"path": "conf.txt", "base_blob": "b" * 40, "blob": "c" * 40, "content": "cap = 9\n"}],
                "diff": ["--- a/conf.txt\n", "+++ b/conf.txt\n", "@@ -1 +1 @@\n", "-cap = 3\n", "+cap = 9 <img src=x>\n"]},
        "A-2": {"status": "approved", "repo": "github.com/team/alpha", "title": "Already decided", "head": "a" * 40,
                "version": 3, "created_at": NOW, "files": [], "diff": []}}

    def test_a_pending_approval_shows_its_diff_and_approve_posts_decide_with_its_version(self):
        got = dom("""
            await start();
            const c = $("#approval-A-1");
            const before = { card: text(c), diff: text(c.querySelector("pre")), approve: button(c, "Approve").disabled,
                             deny: button(c, "Deny").disabled, decided: !!$("#approval-A-2"), n: text($("#n-qq")), q: !!$("#q-Q-1") };
            server.route = r => {
              if (r.path !== "/approvals/A-1/decide") return undefined;
              const d = server.snap.collections.approvals["A-1"]; d.status = "approved"; d.version = 3; server.snap.revision++;
              return { status: 200, body: { ok: true } };
            };
            await click(button(c, "Approve"));
            return { before, sent: calls.filter(r => r.method === "POST").map(r => [r.path, r.body]), after: !!$("#approval-A-1"), tags: [...new Set(created)] };
        """, snapshot(strong=True, collections=dict(LISTS, approvals=self.APPROVALS)))
        b = got["before"]
        for part in ("A-1", "github.com/team/alpha", "Raise the cap", "a" * 40, "the cap is too low"):
            self.assertIn(part, b["card"], part)
        self.assertEqual(b["diff"], "--- a/conf.txt\n+++ b/conf.txt\n@@ -1 +1 @@\n-cap = 3\n+cap = 9 <img src=x>")
        self.assertEqual((b["approve"], b["deny"], b["decided"], b["q"], b["n"]), (False, False, False, True, "2"))
        self.assertEqual(got["sent"], [["/approvals/A-1/decide", {"decision": "approve", "if_version": 2}]])
        self.assertFalse(got["after"])                                          # approved: no longer pending
        self.assertNotIn("img", got["tags"])

    def test_approve_and_deny_need_a_strong_session_and_a_403_shows_the_servers_words(self):
        weak = dom("""
            await start();
            const c = $("#approval-A-1");
            return { approve: button(c, "Approve").disabled, deny: button(c, "Deny").disabled, card: text(c) };
        """, snapshot(collections={"approvals": self.APPROVALS}))
        self.assertEqual((weak["approve"], weak["deny"]), (True, True))           # pagedom fires a disabled button; a browser does not
        self.assertIn("need a strong session", weak["card"])
        words = "POST /approvals/{id}/decide needs the owner logged in through Cloudflare Access"
        got = dom("""
            await start();
            server.route = r => r.path === "/approvals/A-1/decide"
              ? { status: 403, body: { ok: false, error: { code: "forbidden", message: %s, field: "role" } } } : undefined;
            const c = $("#approval-A-1");
            await click(button(c, "Deny"));
            return { card: text(c), approve: button(c, "Approve").disabled, sent: calls.filter(r => r.method === "POST").map(r => r.body),
                     err: text($("#loaderr")) };
        """ % json.dumps(words), snapshot(strong=True, collections={"approvals": self.APPROVALS}))
        self.assertIn("Not decided: " + words, got["card"])
        self.assertNotIn("not paired", got["card"])
        self.assertFalse(got["approve"])                                        # the owner may try again
        self.assertEqual(got["sent"], [{"decision": "deny", "if_version": 2}])
        self.assertEqual(got["err"], "")


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class RecordPageTest(unittest.TestCase):
    """The record page (docs/action-hub.md "The record page"): a row's id opens it, each section is edited
    at the version read, delete takes the typed id, and New story opens a blank record."""

    # Built, so this file holds no ruling id or flag for tests/scrub to flag.
    RULING = "R-" + "9001"
    ASKS = "the priority is the owner's: give " + "--" + "ruling" + " with the ruling id"

    def page(self, body, **values):
        values.setdefault("REC", record())
        head = "".join("const %s = %s;\n" % (k, json.dumps(v)) for k, v in values.items())
        return dom(head + """
            await start();
            await click($("#sections [data-s=backlog]"));
        """ + body, snapshot(views={"repos": [{"name": "alpha"}], "backlog": BACKLOG}))

    @staticmethod
    def refusal(code, message):
        return {"status": 400, "body": {"ok": False, "error": {"code": code, "message": message, "field": ""}}}

    def test_a_rows_id_opens_its_record_and_a_section_edit_is_sent_at_the_version_read(self):
        after = record(version="c2:s2", fields=[["id", "B-1"], ["summary", "one"], ["scope", "tools/x.py"],
                                                ["effort", "m"], ["priority", "2"]])
        got = self.page("""
            server.route = c => c.method === "GET" && c.path === "/records/alpha/B-1" ? { status: 200, body: REC }
              : c.path === "/records/alpha/B-1/edit" ? (c.body.ruling ? { status: 200, body: { ok: true, lines: ["SET B-1"], record: AFTER } } : REFUSED)
              : undefined;
            await click(button($("#list"), "B-1"));
            const out = { reads: calls.filter(c => c.path.startsWith("/records")).map(c => c.method + " " + c.path),
                          heads: $$("#list .rec .pk h2").map(text), shown: text($("#list")) };
            const fields = $$("#list .rec .pk")[0];
            await click(button(fields, "Edit fields"));
            const ta = fields.querySelector("textarea"), ruling = fields.querySelector("input");
            out.text = ta.value; out.rulingFirst = ruling.hidden;
            await type(ta, ta.value.replace("effort: s", "effort: m") + "\\npriority: 2");
            await click(button(fields, "Save"));
            out.refused = text(fields.querySelector(".status")); out.rulingAsked = !ruling.hidden;
            ruling.value = RULING;
            await click(button(fields, "Save"));
            out.sent = calls.filter(c => c.path.endsWith("/edit")).map(c => c.body);
            out.after = text($("#list"));
            await click(button($("#list"), "Back to the backlog"));
            out.back = !$("#list .rec") && !!button($("#list"), "B-1");
            return out;
        """, AFTER=after, REFUSED=self.refusal("invalid", self.ASKS), RULING=self.RULING)
        self.assertEqual(got["reads"], ["GET /records/alpha/B-1"])
        self.assertEqual(got["heads"], ["Fields", "Blocker or hold", "Body", "Branch", "Delete"])
        self.assertIn("summary: one", got["shown"])
        self.assertEqual(got["text"], "id: B-1\nsummary: one\nscope: tools/x.py\neffort: s")
        self.assertTrue(got["rulingFirst"])                                       # asked for only when the store asks
        self.assertEqual(got["refused"], "Not saved: " + self.ASKS)
        self.assertTrue(got["rulingAsked"])
        change = {"version": "c1:s1", "changes": {"effort": ["m"], "priority": ["2"]}, "unset": []}
        self.assertEqual(got["sent"], [change, dict(change, ruling=self.RULING)])
        self.assertIn("priority: 2", got["after"])
        self.assertTrue(got["back"])

    def test_a_parked_rows_id_opens_its_record(self):
        views = {"repos": [{"name": "alpha"}],
                 "backlog": [dict(BACKLOG[0], items=[{"id": "B-4", "t": "waits", "p": 1, "s": "parked", "parked_at": NOW}])]}
        got = dom("""
            server.route = c => c.method === "GET" && c.path === "/records/alpha/B-4" ? { status: 200, body: REC } : undefined;
            await start();
            await click($("#sections [data-s=backlog]"));
            await click($("#stabs [data-sf=parked]"));
            await click(button($$("#list .pk")[0], "B-4"));
            return { reads: calls.filter(c => c.path.startsWith("/records")).map(c => c.method + " " + c.path), page: !!$("#list .rec") };
        """.replace("REC", json.dumps(record(id="B-4", fields=[["id", "B-4"], ["summary", "waits"]]))), snapshot(views=views))
        self.assertEqual(got["reads"], ["GET /records/alpha/B-4"])
        self.assertTrue(got["page"])

    def test_delete_waits_for_a_reason_and_the_typed_id_then_shows_the_record_deleted(self):
        gone = record(where="completed", removed={"how": "deleted", "commit": "abc", "at": "2026-10-17", "subject": "x"})
        got = self.page("""
            let deleted = false;
            server.route = c => c.method === "GET" && c.path === "/records/alpha/B-1" ? { status: 200, body: deleted ? GONE : REC }
              : c.path === "/records/alpha/B-1/delete" ? (deleted = true, { status: 200, body: { ok: true, lines: ["DELETED B-1"] } })
              : undefined;
            await click(button($("#list"), "B-1"));
            const box = $$("#list .rec .pk").find(p => text(p.querySelector("h2")) === "Delete");
            await click(button(box, "Delete…"));
            const go = button(box, "Delete B-1"), [reason, typed] = box.querySelectorAll("input");
            const out = { start: go.disabled };
            await type(reason, "a duplicate of B-2"); out.noId = go.disabled;
            await type(typed, "B-2"); out.wrongId = go.disabled;
            await type(typed, "B-1"); out.ready = go.disabled;
            await click(go);
            out.sent = calls.filter(c => c.path.endsWith("/delete")).map(c => c.body);
            out.after = text($("#list")); out.noDelete = !button($("#list"), "Delete") && !button($("#list"), "Edit");
            return out;
        """, GONE=gone)
        self.assertEqual([got["start"], got["noId"], got["wrongId"], got["ready"]], [True, True, True, False])
        self.assertEqual(got["sent"], [{"why": "a duplicate of B-2", "confirm": "B-1"}])
        self.assertIn("This record is deleted", got["after"])
        self.assertTrue(got["noDelete"])                                           # a removed record is read only

    def test_new_story_opens_a_blank_record_and_shows_the_stores_refusal(self):
        template = {"repo": "alpha", "id_grammar": "B-<n>", "id_pattern": "^B-[0-9]+$", "phases": [], "fields": [],
                    "states": [], "blocked_on": {"plain": [], "prefixes": []}, "efforts": ["s", "m", "l", "-"],
                    "priorities": [], "priority_by": []}
        store = 'Z:/repo names no "backlog".store in .kit.json'
        got = self.page("""
            let refuse = true;
            server.route = c => c.method === "GET" && c.path === "/records/alpha/new" ? { status: 200, body: TEMPLATE }
              : c.method === "POST" && c.path === "/records/alpha" ? (refuse ? STORE : { status: 200, body: { ok: true, lines: ["ADDED B-7"], record: REC } })
              : c.method === "GET" && c.path === "/records/alpha/B-7" ? { status: 200, body: REC }
              : undefined;
            const out = { tabs: $$("#stabs .tab").map(t => t.dataset.sf), button: !$("#rec-new").hidden };
            await click($("#rec-new"));
            const pg = $("#list .rec"), [id, summary] = pg.querySelectorAll("input"), scope = pg.querySelector("textarea");
            const go = button(pg, "Create");
            out.start = go.disabled;
            await type(id, "B-7"); await type(summary, "a new one"); out.noScope = go.disabled;
            await type(scope, "tools/a.py\\n\\ntools/b.py\\n"); out.ready = go.disabled;
            await click(go);
            out.refused = text(pg.querySelector(".status"));
            refuse = false;
            await click(go);
            out.sent = calls.filter(c => c.method === "POST").map(c => c.path + " " + JSON.stringify(c.body));
            out.reads = calls.filter(c => c.method === "GET" && c.path.startsWith("/records")).map(c => c.path);
            return out;
        """, TEMPLATE=template, STORE=self.refusal("store", store))
        self.assertEqual(got["tabs"], ["current", "parked", "done"])
        self.assertTrue(got["button"])
        self.assertEqual([got["start"], got["noScope"], got["ready"]], [True, True, False])
        self.assertEqual(got["refused"], "Not added: " + store)
        body = json.dumps({"id": "B-7", "summary": "a new one", "scope": ["tools/a.py", "tools/b.py"], "effort": "s"},
                          separators=(",", ":"))
        self.assertEqual(got["sent"], ["/records/alpha " + body] * 2)
        self.assertEqual(got["reads"], ["/records/alpha/new", "/records/alpha/B-7"])


DOC_CSP = ("<meta http-equiv=\"Content-Security-Policy\" "
           "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'\">")
# A diagram as a published document carries it: an inline svg, and an svg given as a data: image.
DIAGRAM = ('<h1>Plan</h1><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
           '<img alt="flow" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=">')


def docs_snap():
    return snapshot(
        collections={
            "pages": {"plan-1": {"title": "The <b>plan</b>", "repo": "alpha", "from": "worker", "summary": "what we build",
                                 "refs": ["Q-1"], "at": "2026-10-03T00:00:00Z"},
                      "research-2": {"title": "Research notes", "repo": "beta", "at": "2026-10-04T00:00:00Z"},
                      "bare-3": {"title": "No body yet", "repo": "alpha", "at": "2026-10-01T00:00:00Z"}},
            "page_html": {"plan-1": {"html": DIAGRAM}, "research-2": {"html": "<p>notes</p>"}}},
        views={"repos": [{"name": "alpha", "aliases": []}, {"name": "beta", "aliases": []}]})


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class DocsTest(unittest.TestCase):
    """The Docs section and its reader, under the stand-in DOM. What a browser's sandbox and content policy then do
    with the frame is not something node can show: these pin what the page hands the browser."""

    def test_the_docs_section_lists_documents_as_text_newest_first(self):
        got = dom("""
            await start();
            const count = text($("#n-d"));
            await click($("#sections [data-s=docs]"));
            const cards = $$("#list article").map(a => a.id);
            const plan = $("#doc-plan-1");
            return { count, cards, plan: text(plan), title: text(plan.querySelector("h2")), idq: $("#idq").hidden,
                     bare: { disabled: $("#dr-bare-3").disabled, text: text($("#doc-bare-3")) },
                     read: !$("#dr-plan-1").disabled, tags: [...new Set(created)], sinks };
        """, docs_snap())
        self.assertEqual(got["count"], "3")
        self.assertEqual(got["cards"], ["doc-research-2", "doc-plan-1", "doc-bare-3"])
        self.assertEqual(got["title"], "The <b>plan</b>")                    # markup in a title is drawn as text
        for part in ("plan-1", "alpha", "from worker", "what we build", "Refs: Q-1", "Read here"):
            self.assertIn(part, got["plan"], part)
        self.assertFalse(got["idq"])
        self.assertTrue(got["read"])
        self.assertTrue(got["bare"]["disabled"])                            # a page with no body cannot be opened
        self.assertIn("no body", got["bare"]["text"])
        self.assertNotIn("iframe", got["tags"])                             # nothing is framed until Read here
        self.assertEqual(got["sinks"], [])

    def test_the_repo_menu_and_the_id_box_narrow_the_list(self):
        got = dom("""
            await start();
            const ids = () => $$("#list article").map(a => a.id);
            const sel = $("#repo"); sel.value = "beta"; await fire(sel, "change");
            const beta = ids();
            sel.value = ""; await fire(sel, "change");
            await type($("#idq"), "plan");
            const plan = ids();
            await type($("#idq"), "nothing like it");
            return { beta, plan, none: text($("#list")) };
        """, docs_snap(), hash="#docs")
        self.assertEqual(got["beta"], ["doc-research-2"])
        self.assertEqual(got["plan"], ["doc-plan-1"])
        self.assertIn('No match for "nothing like it"', got["none"])

    def test_an_empty_docs_section_says_where_documents_come_from(self):
        got = dom("""await start(); return text($("#list"));""", snapshot(), hash="#docs")
        self.assertIn("No documents yet", got)
        self.assertIn("page_html/", got)

    def test_read_here_opens_one_sandboxed_frame_with_the_body_unchanged(self):
        got = dom("""
            await start();
            const hidden0 = $("#reader").hidden;
            await click($("#dr-plan-1"));
            const frames = all().filter(e => e.tag === "iframe");
            const f = frames[0];
            const open = { hidden: $("#reader").hidden, title: text($("#reader-title")), focus: document.activeElement.id,
                           inert: $(".wrap").inert, frames: frames.length, attrs: f.attrs, srcdoc: f.srcdoc,
                           inReader: $("#reader-body").contains(f) };
            await click($("#reader-close"));
            const closed = { hidden: $("#reader").hidden, frames: all().filter(e => e.tag === "iframe").length,
                             focus: document.activeElement.id, inert: $(".wrap").inert };
            await click($("#dr-research-2"));
            const again = all().filter(e => e.tag === "iframe").map(e => e.srcdoc);
            let stopped = false;
            for (const f of $("#reader").listeners.keydown) f({ key: "Escape", preventDefault(){ stopped = true; } });
            const esc = { hidden: $("#reader").hidden, focus: document.activeElement.id, stopped };
            return { hidden0, open, closed, again, esc, sinks };
        """, docs_snap(), hash="#docs")
        self.assertTrue(got["hidden0"])
        o = got["open"]
        self.assertFalse(o["hidden"])
        self.assertEqual(o["title"], "The <b>plan</b>")
        self.assertEqual(o["focus"], "reader-close")
        self.assertTrue(o["inert"])
        self.assertEqual(o["frames"], 1)
        self.assertTrue(o["inReader"])
        self.assertEqual(o["attrs"]["sandbox"], "")                         # a sandbox that grants nothing
        self.assertEqual(sorted(o["attrs"]), ["class", "sandbox", "title"])
        self.assertEqual(o["srcdoc"], DOC_CSP + DIAGRAM)                     # the policy first, then the body as published
        self.assertEqual(got["closed"], {"hidden": True, "frames": 0, "focus": "dr-plan-1", "inert": False})
        self.assertEqual(got["again"], [DOC_CSP + "<p>notes</p>"])
        self.assertEqual(got["esc"], {"hidden": True, "focus": "dr-research-2", "stopped": True})
        self.assertEqual(got["sinks"], [])


class PageTextTest(unittest.TestCase):
    def setUp(self):
        self.p = page()
        self.markup, self.script = self.p.split("<script>")[0], self.p.split("<script>")[1]

    def test_the_hidden_attribute_hides_whatever_display_rule_an_element_has(self):
        """A browser lets any author `display` rule beat its own rule for `hidden`; the stand-in DOM has no CSS and
        cannot see that. So the page's style must carry the one rule that makes `hidden` win, and nothing above it."""
        style = re.search(r"<style[^>]*>(.*?)</style>", self.p, re.S).group(1)
        self.assertRegex(style, r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}")
        self.assertEqual(re.findall(r"display\s*:[^;}]*!important", style), ["display:none !important"])
        # Not a check of nothing: elements the page hides do carry an author display rule.
        self.assertRegex(style, r"(?m)^\.tabs\{display:flex")
        self.assertRegex(style, r"(?m)^form\.add\{display:grid")
        self.assertRegex(self.markup, r'<div class="tabs"[^>]*\shidden>')
        self.assertRegex(self.markup, r'<form class="add"[^>]*\shidden[ >]')

    def test_no_artifact_store_call(self):
        for gone in ("window.claude", "onSnapshot", ".collection(", "sendToClaude", "db.", "user.can("):
            self.assertNotIn(gone, self.p, gone)

    def test_server_text_never_becomes_markup(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                     "createContextualFragment", "eval(", "new Function", "javascript:"):
            self.assertNotIn(sink, self.p, sink)
        self.assertIn('if (k === "class") n.className = v; else if (k === "text") n.textContent = v;', self.script)
        # The one place server html goes: the reader's frame, behind the policy (test_the_reader_is_one_locked_frame).
        self.assertEqual(self.p.count("srcdoc"), 1)
        self.assertIn("frame.srcdoc = DOC_CSP + html;", self.script)

    def test_the_reader_is_one_locked_frame(self):
        """The page makes one frame, with a sandbox that grants nothing, and gives it the body behind a policy that
        loads nothing from the network. What the browser then enforces is the owner's real-browser check."""
        self.assertEqual(self.script.count('"iframe"'), 1)
        self.assertRegex(self.script, r'const frame = el\("iframe", \{class:"docframe", sandbox: "", title: [^}]*\}\);')
        self.assertNotIn("allow-", self.p)
        self.assertEqual(re.findall(r"\bsandbox\s*:\s*[^,}]*", self.script), ['sandbox: ""'])   # set once, to nothing
        self.assertNotRegex(self.script, r"\.sandbox\b")                  # and never changed afterwards
        self.assertIn('const DOC_CSP = "<meta http-equiv=\\"Content-Security-Policy\\" content=\\"default-src \'none\'; '
                      'style-src \'unsafe-inline\'; img-src data:; form-action \'none\'; base-uri \'none\'\\">";', self.script)
        self.assertNotIn("createElement(\"iframe\"", self.script)
        self.assertEqual(self.p.count("<iframe"), 0)

    def test_no_attribute_that_loads_or_navigates_is_ever_set(self):
        """The page builds no link, image or frame, so no server value can become an address the browser follows."""
        keys = r"(?:href|src|srcset|srcdoc|action|formaction|poster|background|ping|xlink:href)"
        self.assertEqual(re.findall(r'[{,\s]"?%s"?\s*:' % keys, self.script), [])           # an el() attribute
        self.assertEqual(re.findall(r'setAttribute\(\s*"%s"' % keys, self.script), [])
        # A property set: none, but the reader frame's one body (test_the_reader_is_one_locked_frame).
        self.assertEqual(re.findall(r"\.%s\s*=[^=]" % keys.replace("srcdoc|", ""), self.script), [])
        self.assertEqual(re.findall(r"\.srcdoc\s*=[^=]", self.script), [".srcdoc = "])
        self.assertEqual(re.findall(r"<[^>]*\s%s\s*=" % keys, self.markup), [])
        self.assertNotIn("location.assign", self.script)
        self.assertNotIn("location.replace", self.script)
        self.assertNotIn("window.open", self.script)
        # Every attribute name comes from the page's own code: el() takes the key its caller wrote, the rest name one.
        names = re.findall(r"setAttribute\(([^,]*),", self.script)
        self.assertEqual(sorted(set(names)), ['"aria-selected"', "k"])
        self.assertEqual(names.count("k"), 1)
        self.assertIn("else n.setAttribute(k, v === true ? \"\" : v);", self.script)

    def test_nothing_outside_the_page_is_loaded(self):
        self.assertIsNone(re.search(r"https?:", self.p))
        self.assertIsNone(re.search(r"(?<![:\w])//[\w-]+\.[a-z]{2,}", self.markup))
        for tag in ("<link", "<img", "<iframe", "<object", "<embed", "<base", "@import", "url("):
            self.assertNotIn(tag, self.p, tag)

    def test_markup_fits_a_strict_content_policy(self):
        """No inline handler and no style attribute, so a policy needs only the one script and the one style."""
        self.assertEqual(re.findall(r"<[^>]*\son[a-z]+\s*=", self.markup), [])
        self.assertEqual(re.findall(r"<[^>]*\sstyle\s*=", self.markup), [])
        self.assertEqual(self.p.count("<style"), 1)
        self.assertNotIn('"style"', self.script)
        self.assertNotIn("style:", self.script)

    def test_only_the_decided_routes(self):
        self.assertEqual(re.findall(r'call\("GET", "(/[a-z_]+)', self.script), ["/snapshot", "/wait", "/records"])
        roots = set(re.findall(r'(?:write|hub\.post)\("(/[a-z_]+)', self.script))
        self.assertEqual(roots, {"/questions", "/todos", "/stories", "/records", "/to_pm", "/inbox", "/approvals"})
        self.assertEqual(re.findall(r'"/approvals/" \+ seg\([^)]*\) \+ "(/[a-z]+)"', self.script), ["/decide"])
        for route in ("/alert", "/logout", "/sessions", "/session", "/pair", "/alias", "/aliases", "/pages", "/page",
                      "/health", "/notices"):
            self.assertNotIn('"%s' % route, self.script, route)
        self.assertEqual(re.findall(r'"/inbox[^"]*"[^;]*?"(/[a-z]+)"', self.script), ["/read"])


if __name__ == "__main__":
    unittest.main()
