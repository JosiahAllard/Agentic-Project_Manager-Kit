"""The hub page's Lock notices tab (kitlib/hubsite/web/index.html): the machine lock's lock-window
notices, which the server sends in the snapshot as views.notices (kitlib.hubsite.derived.notices),
drawn under Info. The Docs reader the old artifact had is the Docs section, tested in test_page.py.

The page's pure block runs under node; the tab runs against the stand-in DOM of
tests/hubsite/pagedom.py.
"""
import json
import os
import re
import shutil
import unittest

from kitlib.proc import spawn
from tests.hubsite import pagedom

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = os.path.join(ROOT, "kitlib", "hubsite", "web", "index.html")
NODE = shutil.which("node")


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def js(expr, items):
    """expr, which sees `items` and the page's pure functions, evaluated under node, as JSON."""
    m = re.search(r"// pm-pure:begin\r?\n(.*?)// pm-pure:end", page(), re.S)
    assert m, "pm-pure markers missing from kitlib/hubsite/web/index.html"
    src = m.group(1) + "\nconst items = %s;\nconsole.log(JSON.stringify(%s));" % (json.dumps(items), expr)
    out = spawn.run([NODE, "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def dom(body, snap, hash=""):
    return pagedom.run(NODE, page(), body, snap, hash)


def snapshot(**kw):
    d = {"revision": 3, "pending": [], "collections": {}, "views": {}, "unavailable": {},
         "pm_seen": {"waited": None, "pulled": None, "waiting": 0}}
    d.update(kw)
    return d


def notice(run, phase, at, **kw):
    d = {"id": "L-%s-%s" % (run, phase), "run": run, "phase": phase, "at": at, "repo": "alpha",
         "from": "machine lock", "kind": "lock-window",
         "title": "Heavy run %s: alpha/%s (gate)" % ("started" if phase == "start" else "ended", run),
         "text": "Run %s %s." % (run, "holds 4 cpu slots" if phase == "start" else "ended (exit 0)")}
    d.update(kw)
    return d


def mark(name):
    """A server value that would be markup, and would run, if the page ever read it as HTML."""
    return '<img src=x onerror=alert(1)><script>alert("%s")</script>' % name


NOTICES = [notice("r1", "start", "2026-10-07T10:00:00+00:00"),
           notice("r1", "end", "2026-10-07T10:30:00+00:00"),
           notice("r2", "start", "2026-10-07T11:00:00+00:00")]

OPEN_INFO_NOTICES = """
    await start();
    await click($("#sections [data-s=info]"));
    await click($("#ftabs [data-nf=notices]"));
"""


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class NoticeStateTest(unittest.TestCase):
    def test_notices_are_newest_first_and_read_as_text(self):
        odd = [None, "x", {"title": "no id"}, {"id": 7, "title": "number id"},
               {"id": "L-r3-start", "phase": "start", "run": "r3", "title": ["x"], "text": {"a": 1},
                "repo": 5, "at": "2026-10-07T09:00:00+00:00"}]
        got = js("hubState(items).notices", snapshot(views={"notices": NOTICES + odd}))
        self.assertEqual([n["id"] for n in got], ["L-r2-start", "L-r1-end", "L-r1-start", "L-r3-start"])
        r3 = got[-1]
        self.assertEqual((r3["title"], r3["text"], r3["repo"], r3["run"], r3["phase"]), ("", "", "5", "r3", "start"))
        self.assertEqual(got[0]["title"], NOTICES[2]["title"])

    def test_no_or_odd_notices_view_is_an_empty_list(self):
        for snap in ({}, None, snapshot(views={"notices": "x"}), snapshot(views={"notices": {"a": 1}})):
            self.assertEqual(js("hubState(items).notices", snap), [], snap)

    def test_a_run_is_open_while_it_has_a_start_and_no_end_notice(self):
        self.assertEqual(js("openRuns(hubState(items).notices)", snapshot(views={"notices": NOTICES})), ["r2"])
        self.assertEqual(js("openRuns(hubState(items).notices)", snapshot(views={"notices": NOTICES[:2]})), [])
        # An end notice whose start is gone (deleted, or not written yet) opens nothing.
        self.assertEqual(js("openRuns(hubState(items).notices)", snapshot(views={"notices": NOTICES[1:2]})), [])


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class NoticeTabTest(unittest.TestCase):
    def test_the_tab_draws_each_notice_newest_first_with_the_open_runs(self):
        got = dom(OPEN_INFO_NOTICES + """
            const cards = $$("#list article");
            return { ids: cards.map(c => c.id), shown: text($("#list")), n: text($("#n-n")),
                     hot: $("#n-n").classList.contains("hot"), hash: location.hash };
        """, snapshot(views={"notices": NOTICES}))
        self.assertEqual(got["ids"], ["ln-L-r2-start", "ln-L-r1-end", "ln-L-r1-start"])
        for n in NOTICES:
            self.assertIn(n["title"], got["shown"])
            self.assertIn(n["text"], got["shown"])
        self.assertIn("1 run started with no end notice yet", got["shown"])
        self.assertEqual((got["n"], got["hot"]), ("1", True))
        self.assertEqual(got["hash"], "#notices")

    def test_no_open_run_is_a_quiet_count(self):
        got = dom(OPEN_INFO_NOTICES + """
            return { shown: text($("#list")), n: text($("#n-n")), hot: $("#n-n").classList.contains("hot") };
        """, snapshot(views={"notices": NOTICES[:2]}))
        self.assertEqual((got["n"], got["hot"]), ("0", False))
        self.assertIn("Every run that started has its end notice.", got["shown"])

    def test_notice_text_with_markup_is_drawn_as_text(self):
        bad = notice("r9", "start", "2026-10-07T12:00:00+00:00", id=mark("id"), title=mark("title"),
                     text=mark("text"), repo=mark("repo"))
        got = dom(OPEN_INFO_NOTICES + """
            const risky = ["href", "src", "srcdoc", "srcset", "action", "formaction", "style"];
            const attrs = all().flatMap(e => Object.keys(e.attrs).filter(k => k.startsWith("on") || risky.includes(k)).map(k => e.tag + " " + k));
            return { shown: text($("#list")), sinks, attrs, tags: [...new Set(created)] };
        """, snapshot(views={"notices": [bad]}))
        for part in ("id", "title", "text", "repo"):
            self.assertIn(mark(part), got["shown"], part)
        self.assertEqual(got["sinks"], [])
        self.assertEqual(got["attrs"], ["a href"])                    # the header's one fixed link, to lock.html
        self.assertEqual(set(got["tags"]) & {"img", "script", "iframe", "a"}, set())

    def test_the_hash_opens_the_tab_and_back_returns(self):
        got = dom("""
            await start();
            const nf = () => $$("#ftabs .tab").filter(b => b.getAttribute("aria-selected") === "true").map(b => b.dataset.nf);
            const first = { nf: nf(), shown: text($("#list")), hash: location.hash };
            await click($("#ftabs [data-nf=agents]"));
            await back();
            return { first, after: { nf: nf(), hash: location.hash } };
        """, snapshot(views={"notices": NOTICES}), hash="#notices")
        self.assertEqual(got["first"]["nf"], ["notices"])
        self.assertEqual(got["first"]["hash"], "#notices")
        self.assertIn(NOTICES[0]["title"], got["first"]["shown"])
        self.assertEqual(got["after"], {"nf": ["notices"], "hash": "#notices"})

    def test_an_unavailable_or_empty_view_says_so(self):
        got = dom(OPEN_INFO_NOTICES + "return text($(\"#list\"));",
                  snapshot(views={"notices": None}, unavailable={"notices": "OSError"}))
        self.assertIn("The notices view is unavailable: OSError", got)
        got = dom(OPEN_INFO_NOTICES + "return text($(\"#list\"));", snapshot(views={"notices": []}))
        self.assertIn("No lock notices", got)
        self.assertNotIn("unavailable", got)

    def test_only_the_latest_hundred_are_drawn_and_the_rest_counted(self):
        many = [notice("r%03d" % i, "end", "2026-10-07T%02d:%02d:00+00:00" % (i // 60, i % 60)) for i in range(105)]
        got = dom(OPEN_INFO_NOTICES + """
            return { cards: $$("#list article").length, first: $$("#list article")[0].id, shown: text($("#list")) };
        """, snapshot(views={"notices": many}))
        self.assertEqual(got["cards"], 100)
        self.assertEqual(got["first"], "ln-L-r104-end")
        self.assertIn("5 older notices are not shown.", got["shown"])


if __name__ == "__main__":
    unittest.main()
