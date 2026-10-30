"""The hub page keeps itself up to date: the watch loop of kitlib/hubsite/web/index.html.

The page reads GET /snapshot, then long-polls GET /wait. These tests run the page under node
(tests/hubsite/pagedom.py) and show what it does when that long-poll is cut, when the tab comes
back after a phone slept, and when the tunnel's sign-in sends it a redirect.
"""
import unittest

from tests.hubsite.test_page import NODE, dom, js, snapshot


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class PollFallbackTest(unittest.TestCase):

    def test_the_next_try_is_capped_only_when_the_snapshot_answered(self):
        got = js("[1, 2, 3, 4, 5, 9].map(n => hubNextMs(n, 0, false, true))", None)
        self.assertEqual(got, [2000, 4000, 8000, 15000, 15000, 15000])
        got = js("[4, 5, 9].map(n => hubNextMs(n, 0, false, false))", None)
        self.assertEqual(got, [16000, 32000, 60000])
        # a wait the server asked for, and an unpaired browser, keep their own wait
        self.assertEqual(js("hubNextMs(5, 120, false, true)", None), 120000)
        self.assertEqual(js("hubNextMs(1, 0, true, true)", None), 45000)

    def test_a_long_poll_that_is_always_cut_still_follows_the_hub_by_polling(self):
        got = dom("""
            const cut = () => answerWait(524, null);                 // a proxy's timeout page: no JSON
            await start();
            const seen = [];
            for (let i = 0; i < 5; i++) { await cut(); seen.push(sleeps()); await fireTimers(); }
            server.snap.collections = { todos: { "T-1": { status: "open", title: "made while cut", from: "pm" } } };
            server.snap.revision++;
            await cut(); await fireTimers();
            return { seen, list: text($("#list")), err: text($("#loaderr")),
                     reads: calls.filter(c => c.path === "/snapshot").length };
        """, snapshot(), hash="#todo")
        self.assertEqual(got["seen"], [[2000], [4000], [8000], [15000], [15000]])
        self.assertIn("made while cut", got["list"])
        self.assertEqual(got["err"], "")
        self.assertEqual(got["reads"], 7)                      # the first read and one after each cut wait


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class NudgeTest(unittest.TestCase):

    def test_the_tab_shown_again_ends_the_sleep_and_reads_at_once(self):
        got = dom("""
            await start();
            for (let i = 0; i < 4; i++) { await answerWait(500, { ok: false, error: { code: "internal", message: "x", field: "" } }); await fireTimers(); }
            await answerWait(500, { ok: false, error: { code: "internal", message: "x", field: "" } });
            const asleep = sleeps();
            const before = calls.filter(c => c.path === "/snapshot").length;
            server.snap.collections = { todos: { "T-1": { status: "open", title: "made while away", from: "pm" } } };
            server.snap.revision++;
            await visible(true);
            return { asleep, after: sleeps(), read: calls.filter(c => c.path === "/snapshot").length - before,
                     list: text($("#list")), waiting: waits.length };
        """, snapshot(), hash="#todo")
        self.assertEqual(got["asleep"], [15000])
        self.assertEqual(got["after"], [])                      # the sleep was ended, not left to run
        self.assertEqual(got["read"], 1)
        self.assertIn("made while away", got["list"])
        self.assertEqual(got["waiting"], 1)                     # and the page waits again

    def test_back_online_ends_a_wait_in_flight_with_nothing_said(self):
        got = dom("""
            await start();
            const before = calls.filter(c => c.path === "/snapshot").length;
            server.snap.collections = { todos: { "T-1": { status: "open", title: "missed on a dead line", from: "pm" } } };
            server.snap.revision++;
            await fireWin("online");
            return { read: calls.filter(c => c.path === "/snapshot").length - before, list: text($("#list")),
                     err: text($("#loaderr")), sleeps: sleeps(), waiting: waits.length };
        """, snapshot(), hash="#todo")
        self.assertEqual(got["read"], 1)
        self.assertIn("missed on a dead line", got["list"])
        self.assertEqual(got["err"], "")
        self.assertEqual(got["sleeps"], [])
        self.assertEqual(got["waiting"], 1)

    def test_a_nudge_during_a_read_that_is_refused_is_not_lost(self):
        got = dom("""
            let armed = false;
            server.route = c => {
              if (!armed || c.path !== "/snapshot") return undefined;
              armed = false; fireWin("online");                  // the nudge lands while this read is out
              return { status: 500, body: { ok: false, error: { code: "internal", message: "x", field: "" } } };
            };
            await start();
            const before = calls.filter(c => c.path === "/snapshot").length;
            armed = true; await wake();
            return { read: calls.filter(c => c.path === "/snapshot").length - before,
                     sleeps: sleeps(), waiting: waits.length, err: text($("#loaderr")) };
        """, snapshot())
        self.assertEqual(got["sleeps"], [])                     # no back-off is slept through
        self.assertEqual(got["read"], 2)                        # the refused read, and one at once after it
        self.assertEqual(got["waiting"], 1)
        self.assertEqual(got["err"], "")


@unittest.skipUnless(NODE, "node is needed to run the page's logic")
class SignInTest(unittest.TestCase):

    def test_a_redirect_is_the_tunnels_sign_in_and_says_reload(self):
        got = dom("""
            let gone = false;
            server.route = c => gone && c.path.startsWith("/wait") ? { status: 0, body: null, type: "opaqueredirect" } : undefined;
            await start();
            gone = true; await answerWait(200, { ok: true, revision: server.snap.revision });
            return { err: text($("#loaderr")), sleeps: sleeps() };
        """, snapshot())
        self.assertTrue(got["err"].startswith("The tunnel's sign-in has run out or was refused: reload this page"), got["err"])
        self.assertEqual(got["sleeps"], [45000])


if __name__ == "__main__":
    unittest.main()
