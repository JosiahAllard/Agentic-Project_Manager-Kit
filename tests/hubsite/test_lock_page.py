"""The hub server's schedule page, kitlib/hubsite/web/lock.html.

The page is its own file, linked from the Action Hub page. Its script runs under node against the
stand-in DOM (tests/hubsite/pagedom.py) with a scripted server: what it draws from a lock view, how
it follows the lock (GET /lock/view, then GET /lock/wait from the revision it holds), how it backs
off, that server text is drawn as text, and the owner's changes: each one POST to its lock route,
the answer's view drawn at once, a refusal said with the dialog kept, and a control this login may
not use disabled with the reason. What node cannot show without a browser is checked on
the file's text: one transport, only the lock routes, no HTML sink, no outside resource, and a
colour from the server only after it reads as #rrggbb.
"""
import datetime
import json
import os
import re
import shutil
import unittest

from kitlib.hubsite import httpd, lockdoor, serve
from tests.hubsite import httpfix, pagedom
from tests.hubsite.httpfix import get
from tests.lock import helpers as lh
from kitlib.lock import api as lock_api

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "kitlib", "hubsite", "web")
PAGE = os.path.join(WEB, "lock.html")
INDEX = os.path.join(WEB, "index.html")
NODE = shutil.which("node")

NOW = "2026-10-20T02:48:09-04:00"            # the PC's time in the view: 06:48 UTC
DAY0 = "2026-10-20T04:00:00Z"                # the PC's midnight that starts that day


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def mark(name):
    """A server value that would be markup, and would run, if the page ever read it as HTML."""
    return '<img src=x onerror=alert(1)><script>alert("%s")</script>' % name


def division():
    d = {"threads": 24, "cpu_slot_threads": 2, "cpu_slots": 12, "gpu_share": [1, 1], "half_cpu_slots": 6,
         "half_gpu_slots": 1, "agent_cap": 4, "medium_cores": 2}
    return {"division": d, "default_division": d, "owner_division": {}, "wanted_division": d,
            "cpu_slots": 12, "gpu_slots": 2, "agent_cap": 4, "changed": False, "reason": ""}


def view(**kw):
    """A lock view as kitlib.lockui.model to_json gives it, with `kw` over it."""
    v = {"now": NOW, "zone": "Eastern Daylight Time", "holds": [], "agent_holds": [], "runs": [], "colour": {},
         "strip": [], "lanes": 0, "details": [], "queue": [], "bookings": [],
         "cores": {"head": "12 cpu slots, 2 gpu slots", "cpu": [], "gpu": []},
         "memory": {"free_mb": 8192, "total_mb": 32768}, "warnings": [],
         "game": {"enabled": False, "hold": None, "slots": [], "would_take": [4, 5], "games": [], "running": None},
         "division": division()}
    v.update(kw)
    return v


def answer(v=None, rev=7, writer="owner", writes=None, **kw):
    """What GET /lock/view answers: the view, its revision, who the lock takes the hub for, and what this caller
    may change."""
    out = {"lock_revision": rev, "view": v if v is not None else view(), "writer": writer,
           "writes": writes if writes is not None else {"lock": True, "strong": True, "why": ""}}
    out.update(kw)
    return out


STATS = {"range": {"days": 7}, "rows": [{"kind": "engine-test", "repo": "alpha", "runs": 3, "done": 2, "failed": 1,
                                         "interrupted": 0, "held_s": {"median": 300, "p90": 600, "max": 900},
                                         "wait_s": {"median": 5, "max": 60}, "cpu_s": 1200, "peak_cpu_cores": 3.5,
                                         "peak_mem_mb": 2048}]}

# The scripted server: GET /lock/view answers VIEW (the test may change it), GET /lock/stats answers STATS, and a
# GET /lock/wait hangs until the test answers it.
SERVER = """
  let VIEW = %s;
  const STATS = %s;
  server.route = c => c.path === "/lock/view" ? { status: 200, body: VIEW }
    : c.path.startsWith("/lock/stats") ? { status: 200, body: STATS } : undefined;
"""


def dom(body, first=None, stats=None):
    src = SERVER % (json.dumps(first if first is not None else answer()), json.dumps(stats if stats is not None else STATS))
    return pagedom.run(NODE, page(), src + body, None)


class ServedTest(httpfix.ListenerCase):
    def test_the_listener_serves_the_schedule_page(self):
        lis = self.listener(static_dir=serve.STATIC_DIR)
        r = get(lis.port, "/lock.html")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), ["text/html; charset=utf-8"])
        with open(PAGE, "rb") as f:
            self.assertEqual(r.body, f.read())
        for name, value in httpd.SECURITY_HEADERS:
            self.assertEqual(r.header(name), [value])
        self.assertEqual(self.errors(), [])

    def test_the_action_hub_page_links_to_it_and_it_links_back(self):
        with open(INDEX, encoding="utf-8") as f:
            index = f.read()
        head = index.split("</header>")[0]
        self.assertEqual(re.findall(r'<a href="([^"]*)"', head), ["/lock.html"])
        self.assertEqual(re.findall(r'<a href="([^"]*)"', page().split("</header>")[0]), ["/"])


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class RealViewTest(lh.StoreCase):
    """The page against what the hub's lock door gives from a real lock store."""

    def test_the_first_view_is_drawn_and_server_text_stays_text(self):
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        made = lock_api.hold_create(lh.OWNER, {"owner": "owner", "scope": "whole", "start": iso(now + datetime.timedelta(hours=2)),
                                               "end": iso(now + datetime.timedelta(hours=3)), "note": mark("note")}, store=self.store)
        self.assertTrue(made.get("ok"), made)
        self.live_record("kit-1", slug=mark("slug"), cores=[0, 1])
        got = lockdoor.LockDoor(make_store=lambda: self.store, actor=lh.OWNER).view()
        got["writes"] = {"lock": True, "strong": True, "why": ""}
        out = dom("""
            await start();
            const risky = ["href", "src", "srcdoc", "srcset", "action", "formaction", "style"];
            const attrs = all().flatMap(e => Object.keys(e.attrs).filter(k => k.startsWith("on") || risky.includes(k)).map(k => e.tag + " " + k));
            return { shown: text(body), sinks, attrs, tags: [...new Set(created)], calls, err: text($("#loaderr")),
                     zone: text($("#zone")), holds: $$("#holds tr[data-hold]").length, runs: $$("#runs tr[data-run]").length,
                     cells: $$("#grid td").length, own: $$("#grid td.own").length };
        """, got)
        self.assertEqual(out["err"], "")
        self.assertIn(mark("note"), out["shown"])
        self.assertIn(mark("slug"), out["shown"])
        self.assertEqual(out["sinks"], [])
        self.assertEqual(out["attrs"], ["a href"])               # the header's link back, the page's own text
        self.assertNotIn("script", out["tags"])
        self.assertNotIn("img", out["tags"])
        self.assertIn(got["view"]["zone"], out["zone"])
        self.assertEqual(out["holds"], 1)
        self.assertEqual(out["runs"], 1)
        self.assertEqual(out["cells"], 7 * 48)
        self.assertGreaterEqual(out["own"], 2)                  # the hour's hold covers two half hours, or three
        self.assertEqual([(c["method"], c["path"]) for c in out["calls"]][:3],
                         [("GET", "/lock/view"), ("GET", "/lock/stats?days=7"),
                          ("GET", "/lock/wait?since=%s&timeout=30" % got["lock_revision"])])


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class DrawTest(unittest.TestCase):
    def test_the_week_is_drawn_in_the_pcs_time_whatever_zone_the_browser_is_in(self):
        hold = {"id": "H-1", "owner": "owner", "scope": "whole", "start": "2026-10-20T14:00:00Z",
                "end": "2026-10-20T15:00:00Z", "note": "dinner", "covers": {"cpu": [0, 1], "gpu": [0]}, "live": False}
        half = dict(hold, id="H-2", scope="half", start="2026-10-21T04:00:00Z", end="2026-10-21T04:30:00Z", note="")
        agent = {"id": "A-1", "owner": "agent", "repo": "alpha", "slug": "s", "scope": "slots", "start": "2026-10-22T04:00:00Z",
                 "end": "2026-10-22T05:00:00Z", "covers": {"cpu": [2]}, "lost": {}, "lost_to": []}
        v = view(holds=[hold, half], agent_holds=[agent])
        out = dom("""
            await start();
            const t = iso => String(Date.parse(iso));
            const cell = iso => $("#grid td[data-t=" + t(iso) + "]");
            const cls = iso => (cell(iso) || {className: "missing"}).className;
            const r = { head: text($("#weekhead")), days: $$("#grid thead th").map(text),
              first: cls("DAY0"), own: [cls("2026-10-20T14:00:00Z"), cls("2026-10-20T14:30:00Z"), cls("2026-10-20T15:00:00Z")],
              half: cls("2026-10-21T04:00:00Z"), agent: cls("2026-10-22T04:30:00Z"),
              now: cls("2026-10-20T06:30:00Z"), before: cls("2026-10-20T06:00:00Z"),
              tip: cell("2026-10-20T14:00:00Z").title, rows: $$("#grid tbody th").slice(0, 3).map(text),
              earlier: $("#earlier").disabled, thisweek: $("#thisweek").disabled };
            await click($("#earlier"));
            r.back1 = text($("#weekhead"));
            await click($("#earlier"));
            r.back2 = { head: text($("#weekhead")), off: $("#earlier").disabled, own: $$("#grid td.own").length };
            await click($("#thisweek"));
            for (let i = 0; i < 5; i++) await click($("#later"));
            r.ahead = { head: text($("#weekhead")), off: $("#later").disabled };
            return r;
        """.replace("DAY0", DAY0), answer(v))
        self.assertEqual(out["head"], "Tue 20 Oct to Mon 26 Oct, PC time.")
        self.assertEqual(out["days"][1:], ["Tue 20 Oct", "Wed 21 Oct", "Thu 22 Oct", "Fri 23 Oct", "Sat 24 Oct",
                                           "Sun 25 Oct", "Mon 26 Oct"])
        self.assertEqual(out["rows"], ["00:00", "", "01:00"])
        self.assertIn("past", out["first"])                     # 00:00 PC time is before 02:48
        self.assertEqual(out["own"][:2], ["own", "own"])         # 10:00 to 11:00 PC time
        self.assertEqual(out["own"][2], "")
        self.assertEqual(out["half"], "half")
        self.assertEqual(out["agent"], "agent")
        self.assertEqual(out["now"], "now")
        self.assertEqual(out["before"], "past")
        self.assertEqual(out["tip"], "Tue 20 Oct 10:00: you whole: dinner")
        self.assertEqual((out["earlier"], out["thisweek"]), (False, True))
        self.assertEqual(out["back1"], "Tue 13 Oct to Mon 19 Oct, PC time.")
        self.assertEqual(out["back2"], {"head": "Tue 6 Oct to Mon 12 Oct, PC time.", "off": True, "own": 0})
        self.assertEqual(out["ahead"], {"head": "Tue 10 Nov to Mon 16 Nov, PC time.", "off": True})

    def test_now_slots_holds_runs_and_the_texts(self):
        colour = {"r-1": "#6f9fd8", "r-2": "red;background:url(x)"}
        cores = {"head": "cpu: 12 slots", "cpu": [
            {"slot": 0, "title": "cpu 0", "holder": "r-1", "background": False, "hold": None, "outline": None, "label": "kit-1"},
            {"slot": 1, "title": "cpu 1", "holder": "r-2", "background": True, "hold": "H-1", "outline": "whole", "label": "kit-2"},
            {"slot": 2, "title": "cpu 2", "holder": None, "background": False, "hold": None, "outline": None, "label": "free"}],
            "gpu": [{"slot": 0, "title": "gpu 0", "holder": None, "background": False, "hold": None, "outline": "agent", "label": ""}]}
        run = {"id": "r-1", "state": "held", "repo": "alpha", "slug": "s1", "kind": "build", "since": "2026-10-20T06:00:00Z",
               "held_since": "2026-10-20T06:10:00Z", "expected_end": "2026-10-20T07:10:00Z", "cores": [0, 1], "gpus": [],
               "background": False, "overrun": False, "usage": {"peak_cpu_cores": 1.5, "peak_mem_mb": 512}}
        v = view(colour=colour, cores=cores, runs=[run, dict(run, id="r-2", slug="s2")], warnings=["memory is low"],
                 strip=[{"type": "run", "id": "r-1", "open": True, "late": False, "text": "alpha/s1 build",
                         "start": "2026-10-20T02:10:00-04:00", "end": None, "expect_end": "2026-10-20T03:10:00-04:00", "lane": 0}],
                 details=[["r-1 ", ["c:r-1"]], ["late", ["bad"]], ["plain", []]], queue=[], bookings=[["b", ["dim"]]])
        out = dom("""
            await start();
            const box = $$("#cpu .box");
            return { boxes: box.map(b => [b.className, b.style.borderLeftColor || "", text(b)]),
              gpu: $("#gpu .box").className, warn: text($("#warnings")), hidden: $("#warnings").hidden,
              runs: $$("#runs tr[data-run]").map(r => [text(r.firstChild), r.firstChild.style.borderLeft || ""]),
              details: $$("#details span").map(s => [s.className, s.style.color || "", text(s)]),
              queue: text($("#queue")), bookings: $$("#bookings span").map(s => s.className),
              strip: $$("#strip li").map(l => [text(l), l.style.borderLeftColor || ""]),
              memory: text($("#memory")), head: text($("#slothead")), writer: $("#writer").hidden,
              runcell: $("#grid td[data-t=" + Date.parse("2026-10-20T06:30:00Z") + "]").style.borderLeftColor || "" };
        """, answer(v))
        self.assertEqual(out["boxes"], [["box held", "#6f9fd8", "kit-1"], ["box held bg o-whole", "", "kit-2"],
                                        ["box free", "", "free"]])
        self.assertEqual(out["gpu"], "box free o-agent")
        self.assertEqual((out["warn"], out["hidden"]), ("memory is low", False))
        self.assertEqual(out["runs"], [["r-1", "6px solid #6f9fd8"], ["r-2", ""]])
        self.assertEqual(out["details"], [["", "#6f9fd8", "r-1 "], ["bad", "", "late"], ["", "", "plain"]])
        self.assertEqual(out["queue"], "Nothing waits.")
        self.assertEqual(out["bookings"], ["dim"])
        self.assertEqual(out["strip"], [["alpha/s1 build", "#6f9fd8"]])
        self.assertEqual(out["memory"], "Memory: 8.0 GB free of 32.0 GB.")
        self.assertEqual(out["head"], "cpu: 12 slots")
        self.assertTrue(out["writer"])                          # the owner may change everything: no note
        self.assertEqual(out["runcell"], "#6f9fd8")

    def test_the_zone_is_named_and_typed_times_are_said_to_be_read_in_it(self):
        out = dom("await start(); return text($('#zone'));")
        offset = "UTC" + "-04:00"                               # split: the scrub reads the joined text as a record id
        self.assertEqual(out, "Times on this page are the PC's local time: Eastern Daylight Time (%s). "
                              "Times you type are read in that zone too." % offset)

    def test_who_may_write_is_shown(self):
        why = {"agent": "the machine lock takes this hub for an agent", "tunnel": "only the strong owner, through Access"}
        cases = [answer(writer="agent", writes={"lock": False, "strong": False, "why": why["agent"]}),
                 answer(writes={"lock": True, "strong": False, "why": why["tunnel"]}),
                 answer(writes={"lock": False, "strong": False, "why": ""})]
        got = [dom("await start(); return [text($('#writer')), $('#writer').hidden];", c) for c in cases]
        self.assertEqual(got[0], ["Read only: " + why["agent"], False])
        self.assertEqual(got[1], ["Some changes are closed: " + why["tunnel"], False])
        self.assertEqual(got[2], ["Read only: this login may not change the machine lock.", False])

    def test_what_ran_is_read_for_the_days_chosen(self):
        out = dom("""
            await start();
            const r = { first: $$("#stats tbody tr").map(tr => tr.children.map(text)), pressed: $$("#statdays button").map(b => b.getAttribute("aria-pressed")) };
            await click($("#statdays [data-days=30]"));
            r.asked = calls.filter(c => c.path.startsWith("/lock/stats")).map(c => c.path);
            r.pressed30 = $$("#statdays button").map(b => b.getAttribute("aria-pressed"));
            server.route = c => c.path.startsWith("/lock/stats") ? { status: 400, body: { ok: false, error: { code: "invalid", message: "days is out of range", field: "days" } } } : undefined;
            await click($("#statdays [data-days=90]"));
            r.why = text($("#stats"));
            return r;
        """)
        self.assertEqual(out["first"], [["engine-test", "alpha", "3", "2", "1", "0", "5 min / 10 min / 15 min",
                                         "5 s / 60 s", "20 min", "3.5", "2.0 GB"]])
        self.assertEqual(out["pressed"], ["false", "true", "false", "false"])
        self.assertEqual(out["asked"], ["/lock/stats?days=7", "/lock/stats?days=30"])
        self.assertEqual(out["pressed30"], ["false", "false", "true", "false"])
        self.assertEqual(out["why"], "The run history could not be read: days is out of range")


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class FollowTest(unittest.TestCase):
    """The page follows the lock: one view, then a wait from the revision it holds, again and again."""

    def test_a_change_is_drawn_and_the_next_wait_starts_from_its_revision(self):
        nxt = answer(view(warnings=["changed"]), rev=8, changed=True)
        out = dom("""
            await start();
            await answerWait(200, NEXT);
            return { calls: calls.map(c => c.path), warn: text($("#warnings")), err: text($("#loaderr")) };
        """.replace("NEXT", json.dumps(nxt)))
        self.assertEqual(out["calls"], ["/lock/view", "/lock/stats?days=7", "/lock/wait?since=7&timeout=30",
                                        "/lock/stats?days=7", "/lock/wait?since=8&timeout=30"])
        self.assertEqual((out["warn"], out["err"]), ("changed", ""))

    def test_a_wait_with_no_change_waits_again_without_reading_what_ran(self):
        out = dom("""
            await start();
            await answerWait(200, VIEW);
            return calls.map(c => c.path);
        """)
        self.assertEqual(out, ["/lock/view", "/lock/stats?days=7", "/lock/wait?since=7&timeout=30",
                               "/lock/wait?since=7&timeout=30"])

    def test_a_busy_server_is_waited_out_as_it_asks_then_the_view_is_read_again(self):
        out = dom("""
            await start();
            await answerWait(429, { ok: false, error: { code: "rate_limited", message: "slow down", field: "" } }, { "Retry-After": "7" });
            const r = { err: text($("#loaderr")), sleeps: sleeps(), shown: text($("#slothead")) };
            VIEW = Object.assign({}, VIEW, { lock_revision: 9 });
            await fireTimers();
            r.calls = calls.map(c => c.path).slice(3);
            r.after = [text($("#loaderr")), $("#loaderr").hidden];
            return r;
        """)
        self.assertIn("busy and asked this page to wait. Reading again in 7 s.", out["err"])
        self.assertIn("may be out of date", out["err"])
        self.assertEqual(out["sleeps"], [7000])
        self.assertEqual(out["shown"], "12 cpu slots, 2 gpu slots")       # what it showed stays up
        self.assertEqual(out["calls"], ["/lock/view", "/lock/stats?days=7", "/lock/wait?since=9&timeout=30"])
        self.assertEqual(out["after"], ["", True])

    def test_failures_back_off_doubling(self):
        out = dom("""
            await start();
            const down = { status: 503, body: { ok: false, error: { code: "unavailable", message: "the lock store is shut", field: "lock" } } };
            server.route = c => down;
            const seen = [];
            await answerWait(503, down.body);
            for (let i = 0; i < 7; i++) { seen.push(timers.map(t => t.ms)); await fireTimers(); }
            return { seen, err: text($("#loaderr")) };
        """)
        self.assertEqual(out["seen"], [[1000], [2000], [4000], [8000], [16000], [30000], [30000]])
        self.assertIn("The machine lock cannot be read now (the lock store is shut).", out["err"])

    def test_an_unpaired_browser_is_told_how_to_pair_and_waits_long(self):
        out = dom("""
            server.route = c => ({ status: 401, body: { ok: false, error: { code: "unauthorized", message: "no session", field: "" } } });
            await start();
            return { err: text($("#loaderr")), sleeps: sleeps(), zone: $("#zone").hidden };
        """)
        self.assertIn("not paired with the hub", out["err"])
        self.assertIn("python tools/hub.py open", out["err"])
        self.assertEqual(out["sleeps"], [45000])
        self.assertTrue(out["zone"])                                      # no view: no zone to name

    def test_a_dropped_answer_is_a_network_failure(self):
        out = dom("""
            server.route = c => c.path === "/lock/view" ? { status: 200, body: VIEW, cut: true } : undefined;
            await start();
            return { err: text($("#loaderr")), sleeps: sleeps() };
        """)
        self.assertIn("Cannot reach the hub server (the hub server did not answer). Trying again in 1 s.", out["err"])
        self.assertEqual(out["sleeps"], [1000])

    def test_an_answer_with_no_view_is_not_drawn(self):
        out = dom("""
            server.route = c => c.path === "/lock/view" ? { status: 200, body: { lock_revision: 3 } } : undefined;
            await start();
            return { err: text($("#loaderr")), cells: $$("#grid td").length };
        """)
        self.assertIn("could not show what the hub server sent (the hub server sent no lock view)", out["err"])
        self.assertEqual(out["cells"], 0)


# The scripted server for changes: the reads as SERVER, and a POST answered by REPLY(call), by default the write's
# answer: {ok, result: RESULT} with VIEW (the lock's view after it) over it.
WSERVER = SERVER + """
  let RESULT = {};
  let REPLY = c => ({ status: 200, body: Object.assign({ ok: true, result: RESULT }, VIEW) });
  const reads = server.route;
  server.route = c => c.method === "POST" ? REPLY(c) : reads(c);
  const posts = () => calls.filter(c => c.method === "POST").map(c => [c.path, c.body]);
  const refuse = (status, code, message, field) => () => ({ status, body: { ok: false, error: { code, message, field: field || "lock" } } });
  const said = () => ({ acted: text($("#acted")), acterr: text($("#acterr")) });
  const row = (kind, id) => $("#" + kind + " tr[data-" + (kind === "holds" ? "hold" : "run") + "=" + id + "]");
"""


def wdom(body, first=None):
    src = WSERVER % (json.dumps(first if first is not None else answer()), json.dumps(STATS))
    return pagedom.run(NODE, page(), src + body, None)


HOLD = {"id": "H-1", "owner": "owner", "scope": "whole", "start": "2026-10-20T14:00:00Z", "end": "2026-10-20T15:00:00Z",
        "note": "dinner", "covers": {"cpu": [0, 1], "gpu": [0]}, "live": False}
AGENT_HOLD = {"id": "A-1", "owner": "agent", "repo": "alpha", "slug": "s", "scope": "slots",
              "start": "2026-10-20T06:00:00Z", "end": "2026-10-20T08:00:00Z", "covers": {"cpu": [2]}, "lost": {},
              "lost_to": [], "live": True}
RUN = {"id": "r-1", "state": "running", "repo": "alpha", "slug": "kit-1", "kind": "engine-test",
       "since": "2026-10-20T06:00:00Z", "cores": [0, 1], "gpus": []}


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class WriteTest(unittest.TestCase):
    """The page's changes: each one POST to its lock route with the body the hub's lock door reads, the answer's
    view drawn at once, and a refusal said in the owner's words with the dialog kept."""

    def test_a_window_picked_on_the_grid_is_reserved(self):
        out = wdom("""
            await start();
            const cell = iso => $("#grid td[data-t=" + String(Date.parse(iso)) + "]");
            const r = { before: { shown: !$("#pick").hidden } };
            await click(cell("2026-10-20T14:30:00Z"));
            r.one = { picked: text($("#picked")), cells: $$("#grid td.pick").length, whole: $("#reservewhole").disabled };
            await click(cell("2026-10-20T14:00:00Z"));
            r.two = { picked: text($("#picked")), cells: $$("#grid td.pick").length, whole: $("#reservewhole").disabled,
                      half: $("#reservehalf").disabled };
            await type($("#picknote"), "dinner");
            RESULT = { hold: { id: "H-9", scope: "whole", start: "2026-10-20T14:00:00Z", end: "2026-10-20T15:00:00Z" },
                       notes: ["it overlaps run r-1"] };
            VIEW = Object.assign({}, VIEW, { view: Object.assign({}, VIEW.view, { holds: [Object.assign({}, RESULT.hold, { owner: "owner" })] }) });
            await click($("#reservewhole"));
            r.after = Object.assign(said(), { posts: posts(), shown: !$("#pick").hidden, note: $("#picknote").value,
                      own: $$("#grid td.own").length });
            await click(cell("2026-10-20T06:00:00Z"));
            await click(cell("2026-10-20T07:00:00Z"));
            r.past = text($("#picked"));
            await click(cell("2026-10-20T09:00:00Z"));
            r.third = text($("#picked"));
            await click($("#pickclear"));
            r.cleared = { shown: !$("#pick").hidden, cells: $$("#grid td.pick").length };
            return r;
        """)
        self.assertEqual(out["before"], {"shown": False})
        self.assertEqual(out["one"], {"picked": "From Tue 20 Oct 10:30: now click the half hour the window ends in.",
                                      "cells": 1, "whole": True})
        self.assertEqual(out["two"], {"picked": "Reserve from Tue 20 Oct 10:00 to Tue 20 Oct 11:00, PC time.",
                                      "cells": 2, "whole": False, "half": False})
        self.assertEqual(out["after"]["posts"], [["/lock/holds", {"kind": "reserve", "scope": "whole",
                                                                  "start": "2026-10-20T14:00:00Z",
                                                                  "end": "2026-10-20T15:00:00Z", "note": "dinner"}]])
        self.assertEqual(out["after"]["acted"], "Held the whole machine to Tue 20 Oct 11:00. it overlaps run r-1")
        self.assertEqual(out["after"]["acterr"], "")
        self.assertEqual((out["after"]["shown"], out["after"]["note"]), (False, ""))
        self.assertEqual(out["after"]["own"], 2)                # the answer's view is drawn at once
        self.assertEqual(out["past"], "Reserve from now to Tue 20 Oct 03:30, PC time.")
        self.assertEqual(out["third"], "From Tue 20 Oct 05:00: now click the half hour the window ends in.")
        self.assertEqual(out["cleared"], {"shown": False, "cells": 0})

    def test_a_typed_hold_is_sent_as_typed_and_a_refusal_keeps_the_dialog(self):
        out = wdom("""
            await start();
            const r = { open: !$("#holdform").hidden };
            await click($("#newhold"));
            r.shown = { form: !$("#holdform").hidden, cpu: !$("#hcpu").hidden, focus: document.activeElement === $("#hend") };
            await click(button($("#holdscope"), "Named slots"));
            r.slots = { cpu: !$("#hcpu").hidden, gpu: !$("#hgpu").hidden,
                        pressed: $$("#holdscope button").map(b => b.getAttribute("aria-pressed")) };
            await type($("#hstart"), "18:00"); await type($("#hend"), "+2h"); await type($("#hnote"), "render");
            await type($("#hcpuv"), "0-3"); await type($("#hgpuv"), "0");
            REPLY = refuse(400, "invalid", "the machine lock refused: cpu slot 40 is not on this machine", "cpu");
            await click($("#holdgo"));
            r.refused = Object.assign(said(), { form: !$("#holdform").hidden, end: $("#hend").value, go: $("#holdgo").disabled });
            REPLY = c => ({ status: 200, body: Object.assign({ ok: true, result: { hold: { scope: "slots", end: "2026-10-20T19:00:00Z" } } }, VIEW) });
            await click($("#holdgo"));
            r.made = Object.assign(said(), { form: !$("#holdform").hidden, end: $("#hend").value, posts: posts() });
            await click(button($("#holdscope"), "Half"));
            await type($("#hend"), "20:00");
            await click($("#holdgo"));
            r.half = posts()[2];
            return r;
        """)
        self.assertFalse(out["open"])
        self.assertEqual(out["shown"], {"form": True, "cpu": False, "focus": True})
        self.assertEqual(out["slots"], {"cpu": True, "gpu": True, "pressed": ["false", "false", "true"]})
        self.assertEqual(out["refused"], {"acted": "", "acterr": "Not changed: the machine lock refused: cpu slot 40 is not on this machine.",
                                          "form": True, "end": "+2h", "go": False})
        body = {"kind": "hold", "scope": "slots", "start": "18:00", "end": "+2h", "note": "render", "cpu": "0-3", "gpu": "0"}
        self.assertEqual(out["made"]["posts"], [["/lock/holds", body], ["/lock/holds", body]])
        self.assertEqual(out["made"]["acted"], "Held slots to Tue 20 Oct 15:00.")
        self.assertEqual((out["made"]["acterr"], out["made"]["form"], out["made"]["end"]), ("", False, ""))
        self.assertEqual(out["half"], ["/lock/holds", {"kind": "hold", "scope": "half", "start": "", "end": "20:00",
                                                       "note": "", "cpu": "", "gpu": ""}])

    def test_a_hold_is_edited_and_released_after_a_second_click(self):
        out = wdom("""
            await start();
            const r = {};
            await click(button(row("holds", "H-1"), "Edit"));
            r.edit = { form: !$("#editform").hidden, what: text($("#editwhat")), note: $("#enote").value, start: $("#estart").value };
            await type($("#eend"), "12:00");
            REPLY = refuse(409, "conflict", "the machine lock is busy, try again: another change is being written");
            await click($("#editgo"));
            r.busy = Object.assign(said(), { form: !$("#editform").hidden });
            RESULT = { hold: { id: "H-1", start: "2026-10-20T14:00:00Z", end: "2026-10-20T16:00:00Z" } };
            REPLY = c => ({ status: 200, body: Object.assign({ ok: true, result: RESULT }, VIEW) });
            await click($("#editgo"));
            r.edited = Object.assign(said(), { form: !$("#editform").hidden });
            const n = posts().length;
            await click(button(row("holds", "A-1"), "Release"));
            r.ask = { sent: posts().length - n, buttons: row("holds", "A-1").querySelectorAll("button").map(text) };
            await click(button(row("holds", "A-1"), "Keep it"));
            r.kept = { sent: posts().length - n, buttons: row("holds", "A-1").querySelectorAll("button").map(text) };
            await click(button(row("holds", "A-1"), "Release"));
            RESULT = { hold: { id: "A-1" } };
            await click(button(row("holds", "A-1"), "Release it"));
            r.released = Object.assign(said(), { buttons: row("holds", "A-1").querySelectorAll("button").map(text) });
            r.posts = posts();
            return r;
        """, answer(view(holds=[HOLD], agent_holds=[AGENT_HOLD])))
        self.assertEqual(out["edit"], {"form": True, "note": "dinner", "start": "",
                                       "what": "Hold H-1, you whole: Tue 20 Oct 10:00 to Tue 20 Oct 11:00. "
                                               "Type times as for New hold; leave a field blank to keep it."})
        self.assertEqual(out["busy"], {"acted": "", "form": True,
                                       "acterr": "Not changed: the machine lock is busy, try again: another change is being written."})
        self.assertEqual(out["edited"], {"acted": "Changed hold H-1: Tue 20 Oct 10:00 to Tue 20 Oct 12:00.", "acterr": "", "form": False})
        self.assertEqual(out["ask"], {"sent": 0, "buttons": ["Release it", "Keep it"]})
        self.assertEqual(out["kept"], {"sent": 0, "buttons": ["Edit", "Release"]})
        self.assertEqual(out["released"], {"acted": "Released hold A-1.", "acterr": "", "buttons": ["Edit", "Release"]})
        edit = ["/lock/holds/H-1/edit", {"start": "", "end": "12:00", "note": "dinner"}]
        self.assertEqual(out["posts"], [edit, edit, ["/lock/holds/A-1/end", {}]])

    def test_a_run_is_ended_after_a_second_click_and_a_waiting_one_moved_to_the_top(self):
        queue = [["r-2 alpha/kit-2 waits for 2 cpu slots", ["head"]], ["", ["move:r-2"]], ["\\n", []]]
        out = wdom("""
            await start();
            const r = { top: $$("#queue button").map(text) };
            await click(button(row("runs", "r-1"), "End run"));
            r.ask = { sent: posts().length, buttons: row("runs", "r-1").querySelectorAll("button").map(text) };
            RESULT = { id: "r-1", requested: true };
            await click(button(row("runs", "r-1"), "End run r-1"));
            r.ended = said();
            RESULT = { id: "r-2", position: 1 };
            await click(button($("#queue"), "Move to top"));
            r.moved = said();
            REPLY = c => new Error("connection reset");
            await click(button($("#queue"), "Move to top"));
            r.lost = said();
            r.posts = posts();
            r.shown = text($("#queue"));
            return r;
        """, answer(view(runs=[RUN], queue=queue)))
        self.assertEqual(out["top"], ["Move to top"])
        self.assertEqual(out["ask"], {"sent": 0, "buttons": ["End run r-1", "Let it run"]})
        self.assertEqual(out["ended"], {"acted": "Asked run r-1 to end.", "acterr": ""})
        self.assertEqual(out["moved"], {"acted": "Run r-2 is now first in line.", "acterr": ""})
        self.assertEqual(out["lost"]["acted"], "")
        self.assertIn("did not answer, so the change may or may not have been made", out["lost"]["acterr"])
        self.assertEqual(out["posts"], [["/lock/runs/r-1/end", {"confirm": "r-1"}], ["/lock/runs/r-2/top", {}],
                                        ["/lock/runs/r-2/top", {}]])
        self.assertIn("r-2 alpha/kit-2 waits for 2 cpu slots", out["shown"])

    def test_game_mode_and_the_games_list(self):
        games = [{"exe": "alpha.exe", "name": "Alpha Strike", "anti_cheat": False, "pin": True},
                 {"exe": "beta.exe", "name": "beta.exe", "anti_cheat": True, "pin": False}]
        out = wdom("""
            await start();
            const r = { label: text($("#gametoggle")) };
            RESULT = { enabled: true };
            VIEW = Object.assign({}, VIEW, { view: Object.assign({}, VIEW.view, { game: Object.assign({}, VIEW.view.game, { enabled: true }) }) });
            await click($("#gametoggle"));
            r.on = Object.assign(said(), { label: text($("#gametoggle")) });
            await click($("#gamesedit"));
            r.text = $("#gamestext").value;
            await type($("#gamestext"), "gamma.exe pin Gamma\\n");
            RESULT = { games: [{ exe: "gamma.exe", name: "Gamma", pin: true }] };
            await click($("#gamesgo"));
            r.saved = Object.assign(said(), { form: !$("#gamesform").hidden });
            r.posts = posts();
            return r;
        """, answer(view(game={"enabled": False, "hold": None, "slots": [], "would_take": [4, 5], "games": games,
                               "running": None})))
        self.assertEqual(out["label"], "Turn game mode on")
        self.assertEqual(out["on"], {"acted": "Game mode is on.", "acterr": "", "label": "Turn game mode off"})
        self.assertEqual(out["text"], "alpha.exe  pin  Alpha Strike\nbeta.exe  nopin\n")
        self.assertEqual(out["saved"], {"acted": "The games list has 1 game.", "acterr": "", "form": False})
        self.assertEqual(out["posts"], [["/lock/game_mode", {"enabled": True}],
                                        ["/lock/games", {"text": "gamma.exe pin Gamma\n"}]])

    def test_the_division_fields_and_the_defaults(self):
        out = wdom("""
            await start();
            await click($("#divedit"));
            const field = k => $("#divfields input[data-key=" + k + "]");
            const r = { keys: $$("#divfields input").map(f => f.dataset.key), share: field("gpu_share").value,
                        cap: field("agent_cap").value, hint: field("gpu_share").placeholder };
            await type(field("agent_cap"), "3");
            RESULT = { applied: false, reason: "runs hold the slots" };
            await click($("#divgo"));
            r.saved = Object.assign(said(), { form: !$("#divform").hidden });
            RESULT = { applied: true, reason: null };
            await click($("#divdefaults"));
            r.defaults = said();
            r.posts = posts();
            return r;
        """)
        keys = ["threads", "cpu_slot_threads", "cpu_slots", "gpu_share", "half_cpu_slots", "half_gpu_slots",
                "agent_cap", "medium_cores"]
        self.assertEqual(out["keys"], keys)
        # A fresh machine (the owner set nothing): the fields are blank, the in-force value only a hint.
        self.assertEqual((out["share"], out["cap"]), ("", ""))
        self.assertEqual(out["hint"], "1, 1")
        self.assertEqual(out["saved"], {"acted": "The new division waits: runs hold the slots.", "acterr": "", "form": False})
        self.assertEqual(out["defaults"], {"acted": "The new division is in force.", "acterr": ""})
        self.assertEqual(out["posts"], [["/lock/division", {"fields": {"agent_cap": "3"}}],
                                        ["/lock/division", {"defaults": True}]])

    def test_the_division_dialog_keeps_only_what_the_owner_set(self):
        # As the schedule window does: the owner's own keys fill the fields, the rest stay blank (the default),
        # and a cleared field goes back to the default.
        d = division()
        d["owner_division"] = {"agent_cap": 6, "gpu_share": [2, 1]}
        out = wdom("""
            await start();
            await click($("#divedit"));
            const field = k => $("#divfields input[data-key=" + k + "]");
            const r = { filled: $$("#divfields input").filter(f => f.value).map(f => [f.dataset.key, f.value]) };
            await type(field("gpu_share"), "");
            await type(field("medium_cores"), "3");
            RESULT = { applied: true, reason: null };
            await click($("#divgo"));
            r.posts = posts();
            return r;
        """, answer(view(division=d)))
        self.assertEqual(out["filled"], [["gpu_share", "2, 1"], ["agent_cap", "6"]])
        self.assertEqual(out["posts"], [["/lock/division", {"fields": {"agent_cap": "6", "medium_cores": "3"}}]])

    GATED = """
        await start();
        const need = $$("[data-need]");
        return { strong: need.filter(b => b.dataset.need === "strong").map(b => [b.disabled, b.title]),
                 lock: need.filter(b => b.dataset.need === "lock").map(b => [b.disabled, b.title]),
                 writer: text($("#writer")), count: need.length };
    """

    def test_what_this_login_may_not_change_is_disabled_and_says_why(self):
        why = "the holds and the division need the owner's own sign-in"
        out = wdom(self.GATED, answer(view(holds=[HOLD], runs=[RUN]), writes={"lock": True, "strong": False, "why": why}))
        # reserve whole and half, New hold, Make the hold, Change the hold, Change the division, Use the defaults,
        # Save the division, and the hold's Edit and Release
        self.assertEqual(out["strong"], [[True, why]] * 10)
        # Turn game mode on, Change the games list, Save the games list, and the run's End run
        self.assertEqual(out["lock"], [[False, ""]] * 4)
        self.assertEqual(out["writer"], "Some changes are closed: " + why)

    def test_a_hub_the_lock_takes_for_an_agent_changes_nothing(self):
        why = "the machine lock takes this hub for an agent"
        out = wdom(self.GATED, answer(view(holds=[HOLD], runs=[RUN]), writer="agent",
                                      writes={"lock": False, "strong": False, "why": why}))
        self.assertEqual(out["strong"], [[True, why]] * 10)
        self.assertEqual(out["lock"], [[True, why]] * 4)
        self.assertEqual(out["writer"], "Read only: " + why)

    def test_the_owner_may_change_everything_but_reserve_waits_for_a_window(self):
        out = wdom(self.GATED, answer(view(holds=[HOLD], runs=[RUN])))
        self.assertEqual(out["strong"], [[True, ""], [True, ""]] + [[False, ""]] * 8)
        self.assertEqual(out["lock"], [[False, ""]] * 4)
        self.assertEqual(out["writer"], "")


def hist(id, repo, kind, result, end, **kw):
    """A finished run as the view's strip carries it (kitlib.lockui.model: type hist, with repo, kind, result)."""
    return dict({"type": "hist", "id": id, "open": False, "late": False, "text": "ran %s/%s (%s)" % (repo, id, kind),
                 "repo": repo, "kind": kind, "result": result, "start": end, "end": end, "expect_end": None, "lane": 0}, **kw)


HISTORY = [hist("r-1", "alpha", "build", "ok", "2026-10-19T10:00:00-04:00"),
           hist("r-2", "alpha", "engine-test", "failed", "2026-10-19T11:00:00-04:00"),
           hist("r-3", "beta", "build", "cancelled", "2026-10-19T12:00:00-04:00"),
           hist("r-4", "beta", "engine-test", "ok", "2026-10-19T13:00:00-04:00")]


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class ScheduleTest(unittest.TestCase):
    """The schedule page's sections and its History, collapsed and filtered in the client."""

    def test_the_four_sections_are_drawn_and_history_is_not_in_the_running_strip(self):
        run = {"id": "r-9", "type": "run", "open": True, "late": False, "text": "alpha/s9 build", "start": NOW, "end": None}
        out = dom("""
            await start();
            const h = id => text($("#" + id));
            return { heads: ["h-running", "h-next", "h-holds", "h-history"].map(h), strip: $$("#strip li").map(text),
                     queue: h("queue"), rows: $$("#hist tr[data-hist]").length };
        """, answer(view(strip=[run] + HISTORY)))
        self.assertEqual(out["heads"], ["Running now", "Next up", "Holds", "History"])
        self.assertEqual(out["strip"], ["alpha/s9 build"])
        self.assertEqual(out["queue"], "Nothing waits.")
        self.assertEqual(out["rows"], 4)

    def test_history_is_collapsed_by_default(self):
        out = dom("await start(); return $('#history').getAttribute('open');")
        self.assertIsNone(out)
        self.assertEqual(re.findall(r"<details[^>]*>", page()), ['<details class="hist" id="history">'])

    def test_the_filters_narrow_the_rows_by_repo_kind_and_result(self):
        out = dom("""
            await start();
            const rows = () => $$("#hist tr[data-hist]").map(r => r.dataset.hist);
            const chip = (sel, val) => $$(sel + " button").find(b => b.dataset.val === val);
            const r = { all: rows(), repos: $$("#frepo button").map(text), kinds: $$("#fkind button").map(text),
                        results: $$("#fresult button").map(text), count: text($("#histcount")) };
            await click(chip("#frepo", "beta"));
            r.repo = rows(); r.pressed = chip("#frepo", "beta").getAttribute("aria-pressed");
            await click(chip("#fkind", "build"));
            r.repoKind = rows();
            await click(chip("#fresult", "ok"));
            r.none = [rows(), text($("#hist")), text($("#histcount"))];
            await click(chip("#frepo", ""));
            r.kindResult = rows();
            await click(chip("#fkind", ""));
            r.result = rows();
            await click(chip("#fresult", ""));
            r.reset = rows();
            return r;
        """, answer(view(strip=HISTORY)))
        self.assertEqual(out["all"], ["r-4", "r-3", "r-2", "r-1"])           # newest first
        self.assertEqual(out["repos"], ["All repository", "alpha", "beta"])
        self.assertEqual(out["kinds"], ["All kind", "build", "engine-test"])
        self.assertEqual(out["results"], ["All result", "cancelled", "failed", "ok"])
        self.assertEqual(out["count"], "Showing 4 of 4 finished runs.")
        self.assertEqual((out["repo"], out["pressed"]), (["r-4", "r-3"], "true"))
        self.assertEqual(out["repoKind"], ["r-3"])
        self.assertEqual(out["none"], [[], "No finished run matches.", "Showing 0 of 4 finished runs."])
        self.assertEqual(out["kindResult"], ["r-1"])
        self.assertEqual(out["result"], ["r-4", "r-1"])
        self.assertEqual(out["reset"], ["r-4", "r-3", "r-2", "r-1"])

    def test_a_filter_survives_a_redraw_of_the_lock(self):
        nxt = answer(view(strip=HISTORY[:3]), rev=8, changed=True)
        out = dom("""
            await start();
            await click($$("#fresult button").find(b => b.dataset.val === "ok"));
            await answerWait(200, NEXT);
            return $$("#hist tr[data-hist]").map(r => r.dataset.hist);
        """.replace("NEXT", json.dumps(nxt)), answer(view(strip=HISTORY)))
        self.assertEqual(out, ["r-1"])


class PageTextTest(unittest.TestCase):
    def setUp(self):
        self.p = page()
        self.markup, self.script = self.p.split("<script>")[0], self.p.split("<script>")[1]

    def test_the_hidden_attribute_hides_whatever_display_rule_an_element_has(self):
        style = re.search(r"<style[^>]*>(.*?)</style>", self.p, re.S).group(1)
        self.assertRegex(style, r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;?\s*\}")
        self.assertEqual(re.findall(r"display\s*:[^;}]*!important", style), ["display:none !important"])

    def test_server_text_never_becomes_markup(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                     "createContextualFragment", "eval(", "new Function", "javascript:", "srcdoc", "iframe"):
            self.assertNotIn(sink, self.p, sink)
        self.assertIn('if (k === "class") n.className = v; else if (k === "text") n.textContent = v;', self.script)

    def test_no_attribute_that_loads_or_navigates_is_ever_set(self):
        keys = r"(?:href|src|srcset|srcdoc|action|formaction|poster|background|ping|xlink:href)"
        self.assertEqual(re.findall(r'[{,\s]"?%s"?\s*:' % keys, self.script), [])
        self.assertEqual(re.findall(r'setAttribute\(\s*"%s"' % keys, self.script), [])
        self.assertEqual(re.findall(r"\.%s\s*=[^=]" % keys, self.script), [])
        # The markup's one link is the page's own text: back to the Action Hub.
        self.assertEqual(re.findall(r"<[^>]*\s%s\s*=[^>]*>" % keys, self.markup),
                         ['<link rel="stylesheet" href="hub.css">', '<a href="/">'])
        for gone in ("location", "window.open"):
            self.assertNotIn(gone, self.script, gone)
        names = re.findall(r"setAttribute\(([^,]*),", self.script)
        self.assertEqual(sorted(set(names)), ['"aria-pressed"', '"title"', "k"])

    def test_nothing_outside_the_page_is_loaded(self):
        self.assertIsNone(re.search(r"https?:", self.p))
        self.assertIsNone(re.search(r"(?<![:\w])//[\w-]+\.[a-z]{2,}", self.markup))
        # The one stylesheet is the shared hub.css, served beside the page; any other link is refused.
        self.assertEqual(re.findall(r"<link\b[^>]*>", self.p), ['<link rel="stylesheet" href="hub.css">'])
        for tag in ("<img", "<iframe", "<object", "<embed", "<base", "@import", "url("):
            self.assertNotIn(tag, self.p, tag)

    def test_markup_fits_a_strict_content_policy(self):
        self.assertEqual(re.findall(r"<[^>]*\son[a-z]+\s*=", self.markup), [])
        self.assertEqual(re.findall(r"<[^>]*\sstyle\s*=", self.markup), [])
        self.assertEqual(self.p.count("<style"), 1)
        self.assertEqual(self.p.count("<script"), 1)
        self.assertNotIn('"style"', self.script)
        self.assertNotIn("style:", self.script)

    def test_a_colour_from_the_server_is_used_only_as_rrggbb(self):
        self.assertIn('return typeof c === "string" && /^#[0-9a-fA-F]{6}$/.test(c) ? c : "";', self.script)
        sets = re.findall(r"\.style\.(\w+)\s*=([^;]*);", self.script)
        self.assertTrue(sets)
        for prop, value in sets:
            self.assertIn(prop, ("color", "borderLeftColor", "borderLeft"), prop)
            self.assertRegex(value, r'^ (?:"6px solid " \+ )?colourOf\([^()]*(?:\([^()]*\))?\)$', value)

    def test_only_the_lock_routes(self):
        self.assertEqual(sorted(re.findall(r'call\("(GET|POST)", "(/[a-z_/]+)', self.script)),
                         [("GET", "/lock/stats"), ("GET", "/lock/view"), ("GET", "/lock/wait")])
        # Every change is one POST, from write(), to a lock route the page names.
        self.assertEqual(re.findall(r'call\("POST", (\w+)', self.script), ["path"])
        self.assertEqual(self.script.count("hub.call(") + self.script.count("this.call("), 4)
        self.assertEqual(sorted(set(re.findall(r'write\("([^"]*)"', self.script))),
                         ["/lock/division", "/lock/game_mode", "/lock/games", "/lock/holds", "/lock/holds/",
                          "/lock/runs/"])
        self.assertEqual(len(re.findall(r'write\(', self.script)), 11)   # the definition and ten changes
        self.assertEqual(self.script.count("fetch("), 1)
        self.assertIn('base: "/api/v1",', self.script)
        self.assertIn('headers: { Accept: "application/json", "X-Hub-Via": "web" }', self.script)
    def test_every_write_is_still_wired_to_its_route_and_body(self):
        wired = ['write("/lock/runs/" + seg(id) + "/top", {}',
                 'write("/lock/holds/" + seg(id) + "/end", {}',
                 'write("/lock/runs/" + seg(id) + "/end", {confirm: id}',
                 'write("/lock/holds", {kind: "reserve", scope, start: iso(p.s), end: iso(p.e), note: val("#picknote")}',
                 'write("/lock/holds", {kind: "hold", scope: state.scope, start: val("#hstart"), end: val("#hend"), note: val("#hnote")',
                 'write("/lock/holds/" + seg(id) + "/edit", {start: val("#estart"), end: val("#eend"), note: val("#enote")}',
                 'write("/lock/game_mode", {enabled: on}',
                 'write("/lock/games", {text: val("#gamestext")}',
                 'write("/lock/division", {fields}',
                 'write("/lock/division", {defaults: true}']
        for w in wired:
            self.assertEqual(self.script.count(w), 1, w)
        # The second click before a release or an end, and the buttons that start each write.
        for ident in ("reservewhole", "reservehalf", "holdgo", "editgo", "gametoggle", "gamesgo", "divgo", "divdefaults"):
            self.assertIn('id="%s"' % ident, self.markup)

    def test_the_schedule_has_its_four_sections_and_history_starts_closed(self):
        for h in ("h-running", "h-next", "h-holds", "h-history"):
            self.assertEqual(self.markup.count('<h2 id="%s">' % h), 1, h)
        order = [self.markup.index('id="%s"' % h) for h in ("h-running", "h-next", "h-holds", "h-history")]
        self.assertEqual(order, sorted(order))
        self.assertNotRegex(self.markup, r"<details[^>]*\sopen")

        self.assertIn('credentials: "same-origin"', self.script)


if __name__ == "__main__":
    unittest.main()
