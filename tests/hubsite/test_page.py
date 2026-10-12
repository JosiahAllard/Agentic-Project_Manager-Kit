"""The hub server's web page, kitlib/hubsite/web/index.html.

The page is one HTML file. Its pure functions sit between the `pm-pure:begin` and `pm-pure:end`
markers, as in claude/hub/action-hub.html, so the same node-run tests apply (tests/hubtopm) and
the new pure functions (the snapshot's shape, the Parked groups, the PM line, the retry delay) are
tested the same way. What node cannot show without a browser is checked on the file's text: one
transport, no artifact-store call, no HTML sink, no outside resource, and only the decided routes.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import unittest

from kitlib.hubsite import httpd, serve
from kitlib.hubsite.api import BASE
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


def js(expr, items):
    """Evaluate expr (which sees `items` and the page's pure functions) under node; return its JSON."""
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
        for k in ("docs", "inbox", "stories", "topm", "repos", "backlog", "completed", "parked", "usage", "agents"):
            self.assertEqual(got[k], [], k)
        self.assertEqual(got["gone"], snap.json()["unavailable"])


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class LogicTest(unittest.TestCase):
    """The tests of tests/hubtopm, on the ported page's pure block."""

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
            for k in ("docs", "inbox", "todos", "stories", "topm", "repos", "backlog", "completed", "parked", "usage", "agents"):
                self.assertEqual(got[k], [], (k, snap))
        self.assertEqual(js("hubState(items).revision", {}), 0)

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
                 "c-title", "c-stale", "k-branch", "gone", "account", "repo")
        m = {n: mark(n) for n in names}
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
                "stories": {"S-1": {"status": "review", "title": m["s-title"], "story": m["s-story"], "review_note": m["s-note"]}}},
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
            for (const s of ["topm", "todo", "info"]) await see("#sections [data-s=" + s + "]");
            await see("#ftabs [data-nf=agents]");
            await see("#sections [data-s=backlog]");
            for (const f of ["current", "parked", "done"]) await see("#stabs [data-sf=" + f + "]");
            const risky = ["href", "src", "srcdoc", "srcset", "action", "formaction", "style"];
            const attrs = all().flatMap(e => Object.keys(e.attrs).filter(k => k.startsWith("on") || risky.includes(k)).map(k => e.tag + " " + k));
            return { shown, tags: [...new Set(created)], sinks, attrs, err: text($("#loaderr")) };
        """, snap)
        for name in names:
            self.assertIn(m[name], got["shown"], name)
        self.assertEqual(got["sinks"], [])
        self.assertEqual(got["attrs"], [])
        self.assertEqual(got["err"], "")
        made = set(got["tags"])
        self.assertIn("article", made)
        self.assertEqual(made & {"img", "script", "a", "iframe", "object", "embed", "link", "style", "base", "meta", "svg", "form"},
                         set())

    def test_an_unpaired_browser_is_told_to_pair_again_and_stops_reading(self):
        # The server answers a call that carries no session 403, naming the field `role`; it sends no 401.
        for status, field in ((403, "role"), (403, ""), (401, "")):
            for where in ("snapshot", "wait"):
                got = dom("""
                    const no = { status: %d, body: { ok: false, error: { code: "forbidden", message: "this request carries no owner or agent credential", field: %s } } };
                    if (%s) server.route = c => c.path === "/snapshot" ? no : undefined;
                    await start();
                    if (!%s) await answerWait(no.status, no.body);
                    const first = { err: text($("#loaderr")), hidden: $("#loaderr").hidden, calls: calls.length, sleeps: sleeps() };
                    await fireTimers(); await fireTimers();
                    return { first, calls: calls.length, waiting: waits.length, list: text($("#list")) };
                """ % (status, json.dumps(field), json.dumps(where == "snapshot"), json.dumps(where == "snapshot")),
                    snapshot(collections={"todos": {"T-1": {"status": "open", "title": "kept on the page", "from": "pm"}}}),
                    hash="#todo")
                with self.subTest(status=status, field=field, where=where):
                    self.assertIn("no longer paired", got["first"]["err"])
                    self.assertIn("`hub open` on the PC", got["first"]["err"])
                    self.assertNotIn("Trying again", got["first"]["err"])
                    self.assertFalse(got["first"]["hidden"])
                    self.assertEqual(got["first"]["sleeps"], [])                 # no later try is waiting
                    self.assertEqual(got["calls"], got["first"]["calls"])        # and none is made
                    self.assertEqual(got["waiting"], 0)
                    if where == "wait":
                        self.assertIn("kept on the page", got["list"])
                        self.assertIn("may be out of date", got["first"]["err"])

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
        self.assertNotIn("no longer paired", got["origin"])
        self.assertIn("no longer paired", got["role"])
        self.assertIn("`hub open` on the PC", got["role"])
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
                        section: $("#sec-backlog").getAttribute("aria-selected"), form: $("#storyform").hidden, row: $("#stabs").hidden };
            out.cards = $$("#list .pk").map(c => c.attrs.id);
            $("#brepo").value = "beta"; await fire($("#brepo"), "change");
            out.beta = $$("#list .pk").map(c => c.attrs.id);
            server.snap.views.backlog = []; server.snap.revision++;
            await wake();
            out.none = on(); out.empty = text($("#list"));
            return out;
        """, snapshot(views=views))
        self.assertEqual(got["sections"], ["questions", "topm", "todo", "backlog", "info"])
        self.assertEqual(got["first"], {"say": "3 parked", "off": False, "hot": True})       # seen from any section
        self.assertEqual(got["badge"], "sec-backlog")
        self.assertEqual(got["tab"], {"chosen": "true", "n": "3", "section": "true", "form": True, "row": False})
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
            await click($("#sections [data-s=backlog]")); await click($("#stabs [data-sf=new]")); out.story = cards();
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


class PageTextTest(unittest.TestCase):
    def setUp(self):
        self.p = page()
        self.markup, self.script = self.p.split("<script>")[0], self.p.split("<script>")[1]

    def test_one_inline_script_and_one_transport(self):
        self.assertEqual(self.p.count("<script"), 1)
        self.assertEqual(self.p.count("fetch("), 1)
        self.assertIn("fetch(this.base + path, init)", self.p)
        self.assertIn('base: "/api/v1"', self.p)
        self.assertNotIn("XMLHttpRequest", self.p)
        self.assertNotIn("WebSocket", self.p)
        self.assertNotIn("EventSource", self.p)
        self.assertNotIn("sendBeacon", self.p)

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

    def test_state_filters_are_a_quiet_text_switch_and_what_filters_stay_pills(self):
        """No test here runs a browser: this pins the classes and the style rules the look hangs on, not the look."""
        lists = {}
        for cls, rest in re.findall(r'<div class="([^"]*)" role="tablist"([^>]*)>', self.markup):
            name = re.search(r'\bid="(\w+)"', rest) or re.search(r'data-for="(\w+)"', rest)
            lists[name.group(1)] = cls
        self.assertEqual(lists.pop("sections"), "sections")                      # the top row is neither kind
        self.assertEqual(lists, {"qtabs": "tabs", "stabs": "tabs", "ftabs": "tabs",                # WHAT is shown: pills
                                 "tabs": "tabs sw", "itabs": "tabs sw", "ttabs": "tabs sw", "topm": "tabs sw"})      # its STATE: quiet text
        # Still real buttons in a tablist, each with aria-selected.
        for row in re.findall(r'<div class="tabs sw".*?</div>', self.markup, re.S):
            buttons = re.findall(r"<(\w+)([^>]*)>", row)[1:]
            tabs = [a for t, a in buttons if t == "button"]
            self.assertGreaterEqual(len(tabs), 2, row)
            for a in tabs:
                self.assertRegex(a, r'class="tab" role="tab"[^>]*aria-selected="(true|false)"')
        style = re.search(r"<style[^>]*>(.*?)</style>", self.p, re.S).group(1)
        rule = lambda sel: re.search(r"(?m)^" + re.escape(sel) + r"\{([^}]*)\}", style).group(1)      # noqa: E731
        plain = rule(".sw .tab")
        for part in ("border:0", "background:none", "border-radius:0"):
            self.assertIn(part, plain)
        chosen = rule('.sw .tab[aria-selected="true"]')
        self.assertIn("text-decoration:underline", chosen)
        self.assertIn("background:none", chosen)                                 # the pill's filled look does not come through
        self.assertIn('content:"\\00b7"', rule(".sw .tab+.tab::before"))          # a middle dot between the choices
        self.assertLess(style.index('.tab[aria-selected="true"]{'), style.index(".sw .tab{"))       # the quiet rules come later, so they win
        self.assertNotIn("\x00", self.p)

    def test_no_artifact_store_call(self):
        for gone in ("window.claude", "onSnapshot", ".collection(", "sendToClaude", "db.", "user.can("):
            self.assertNotIn(gone, self.p, gone)

    def test_server_text_never_becomes_markup(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "srcdoc",
                     "createContextualFragment", "eval(", "new Function", "javascript:"):
            self.assertNotIn(sink, self.p, sink)
        self.assertIn('if (k === "class") n.className = v; else if (k === "text") n.textContent = v;', self.script)

    def test_no_attribute_that_loads_or_navigates_is_ever_set(self):
        """The page builds no link, image or frame, so no server value can become an address the browser follows."""
        keys = r"(?:href|src|srcset|srcdoc|action|formaction|poster|background|ping|xlink:href)"
        self.assertEqual(re.findall(r'[{,\s]"?%s"?\s*:' % keys, self.script), [])           # an el() attribute
        self.assertEqual(re.findall(r'setAttribute\(\s*"%s"' % keys, self.script), [])
        self.assertEqual(re.findall(r"\.%s\s*=[^=]" % keys, self.script), [])                # a property set
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
        self.assertEqual(re.findall(r'call\("GET", "(/[a-z_]+)', self.script), ["/snapshot", "/wait"])
        roots = set(re.findall(r'(?:write|hub\.post)\("(/[a-z_]+)', self.script))
        self.assertEqual(roots, {"/questions", "/todos", "/stories", "/to_pm", "/inbox"})
        for route in ("/alert", "/logout", "/sessions", "/session", "/pair", "/alias", "/aliases", "/pages", "/page",
                      "/health", "/notices"):
            self.assertNotIn('"%s' % route, self.script, route)
        self.assertEqual(re.findall(r'"/inbox[^"]*"[^;]*?"(/[a-z]+)"', self.script), ["/read"])

    def test_the_owner_edits_and_deletes_only_their_own_to_dos(self):
        s = self.script
        self.assertRegex(s, r'"/todos/" \+ seg\([^)]*\) \+ "/edit"')
        self.assertRegex(s, r'"/todos/" \+ seg\([^)]*\) \+ "/delete"')
        card = s[s.index("function todoCard"):]
        own = card.index('from === "you"')
        self.assertLess(own, card.index('"/edit"'))
        self.assertLess(own, card.index('"/delete"'))
        self.assertIn("Mark urgent", card)

    def test_sections(self):
        self.assertRegex(self.markup, r'data-sf="parked"[^>]*>Parked')          # Parked is a tab of the Backlog row,
        self.assertNotIn('data-s="parked"', self.markup)                         # no longer a section of its own
        order = [self.markup.index('data-sf="%s"' % f) for f in ("new", "current", "parked", "done")]
        self.assertEqual(order, sorted(order))
        self.assertRegex(self.markup, r'data-s="topm"[^>]*>To PM')
        self.assertLess(self.markup.index('data-s="questions"'), self.markup.index('data-s="topm"'))
        self.assertLess(self.markup.index('data-s="topm"'), self.markup.index('data-s="todo"'))
        self.assertRegex(self.markup, r'<div[^>]*id="pmseen"[^>]*role="status"')
        self.assertNotRegex(self.markup, r"(?i)<button[^>]*>[^<]*(alert|log ?out|sign out|end sessions)")
        self.assertNotRegex(self.script, r'text: ?"[^"]*(Alert the PM|Log ?out|Sign out|End sessions)')


if __name__ == "__main__":
    unittest.main()
