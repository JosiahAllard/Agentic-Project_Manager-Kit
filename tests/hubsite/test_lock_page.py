"""The hub server's schedule page, kitlib/hubsite/web/lock.html.

The page is its own file, linked from the Action Hub page. Its script runs under node against the
stand-in DOM (tests/hubsite/pagedom.py) with a scripted server: what it draws from a lock view, how
it follows the lock (GET /lock/view, then GET /lock/wait from the revision it holds), how it backs
off, and that server text is drawn as text. What node cannot show without a browser is checked on
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
        self.assertEqual(re.findall(r"<[^>]*\s%s\s*=[^>]*>" % keys, self.markup), ['<a href="/">'])
        for gone in ("location", "window.open"):
            self.assertNotIn(gone, self.script, gone)
        names = re.findall(r"setAttribute\(([^,]*),", self.script)
        self.assertEqual(sorted(set(names)), ['"aria-pressed"', "k"])

    def test_nothing_outside_the_page_is_loaded(self):
        self.assertIsNone(re.search(r"https?:", self.p))
        self.assertIsNone(re.search(r"(?<![:\w])//[\w-]+\.[a-z]{2,}", self.markup))
        for tag in ("<link", "<img", "<iframe", "<object", "<embed", "<base", "@import", "url("):
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
        self.assertEqual(self.script.count("hub.call(") + self.script.count("this.call("), 3)
        self.assertEqual(self.script.count("fetch("), 1)
        self.assertIn('base: "/api/v1",', self.script)
        self.assertIn('headers: { Accept: "application/json", "X-Hub-Via": "web" }', self.script)
        self.assertIn('credentials: "same-origin"', self.script)


if __name__ == "__main__":
    unittest.main()
