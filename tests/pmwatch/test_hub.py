"""The HUB event: pm_watch's check of the hub server's GET health `code` (kitlib.pmwatch.hub_check):
hub code newer than the running server has landed, so the owner restarts it. The settings and the
client are fakes; nothing is asked of a real server."""
import os
import unittest

from kitlib import pmstart
from kitlib import pmwatch as W
from kitlib.hubsite import codestale, serve


class Settings(object):
    port = 8790


class Client(object):
    def __init__(self, health=None, error=None):
        self.health, self.error, self.asked = health, error, []

    def get(self, path, timeout=None):
        self.asked.append((path, timeout))
        if self.error is not None:
            raise self.error
        return self.health


STALE = {"ok": True, "code": {"stale": True, "restart": "x"}}
FRESH = {"ok": True, "code": {"stale": False, "restart": None}}


class HubEventTest(unittest.TestCase):
    def watcher(self, client, state=None, load=lambda: Settings()):
        check = W.hub_check(load=load, make_client=lambda port: client)
        w = W.Watcher(state if state is not None else {}, [], lambda: {}, os.path.join("nowhere", "alerts"),
                      os.path.join("nowhere", "sessions.json"), lambda n: None, branch_poll=0, hub=check)
        w.start(0.0)
        return w

    def test_stale_is_one_event_with_the_owners_step_then_silent_and_a_relaunch_stays_silent(self):
        c = Client(STALE)
        w = self.watcher(c)
        ev = w.poll(1.0)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0], "HUB " + codestale.text(pmstart.HUB_TOOL))
        self.assertIn('python "%s" restart' % pmstart.HUB_TOOL, ev[0])
        self.assertEqual(c.asked[0][0], "/health")
        self.assertEqual(w.poll(2.0), [])
        self.assertIs(w.s["hub_stale"], True)
        self.assertEqual(self.watcher(c, state=dict(w.s)).poll(3.0), [])

    def test_the_hub_is_asked_at_most_once_per_branch_poll(self):
        c = Client(STALE)
        check = W.hub_check(load=lambda: Settings(), make_client=lambda port: c)
        w = W.Watcher({}, [], lambda: {}, os.path.join("nowhere", "alerts"),
                      os.path.join("nowhere", "sessions.json"), lambda n: None, branch_poll=300, hub=check)
        w.start(0.0)
        w.poll(1.0)
        w.poll(2.0)
        w.poll(300.0)
        self.assertEqual(len(c.asked), 1)
        w.poll(301.0)
        self.assertEqual(len(c.asked), 2)

    def test_a_restart_clears_the_memory_and_a_later_stale_is_said_again(self):
        c = Client(STALE)
        w = self.watcher(c)
        self.assertEqual(len(w.poll(1.0)), 1)
        c.health = FRESH
        self.assertEqual(w.poll(2.0), [])
        self.assertIsNone(w.s["hub_stale"])
        c.health = STALE
        self.assertEqual(len(w.poll(3.0)), 1)

    def test_a_current_hub_or_one_that_predates_the_field_is_silent(self):
        for health in (FRESH, {"ok": True}, {"ok": True, "code": None}, {"ok": True, "code": "stale"}, "x", None):
            self.assertEqual(self.watcher(Client(health)).poll(1.0), [], health)

    def test_a_hub_that_is_down_is_no_event_and_no_failure_even_after_three_polls(self):
        down = [serve.NotRunning(8790), pmstart.NoHubToken()]
        if hasattr(pmstart, "NoHubPointer"):
            down.append(pmstart.NoHubPointer())
        for err in down:
            w = self.watcher(Client(error=err))
            for n in range(1, 5):
                self.assertEqual(w.poll(float(n)), [], err)
            self.assertEqual(w.fails["hub"], 0)

    def test_a_down_hub_re_arms_the_event(self):
        c = Client(STALE)
        w = self.watcher(c)
        self.assertEqual(len(w.poll(1.0)), 1)
        c.health, c.error = None, serve.NotRunning(8790)
        self.assertEqual(w.poll(2.0), [])
        c.health, c.error = STALE, None
        self.assertEqual(len(w.poll(3.0)), 1)

    def test_any_other_failure_is_an_error_event_on_the_third_in_a_row_and_never_raises(self):
        w = self.watcher(Client(error=RuntimeError("boom")))
        self.assertEqual(w.poll(1.0), [])
        self.assertEqual(w.poll(2.0), [])
        self.assertEqual(w.poll(3.0), ["ERROR hub check: boom"])

    def test_settings_that_cannot_be_had_are_a_failed_check_not_a_dead_watcher(self):
        def load():
            raise pmstart.HubSettingsError("bad key")
        w = self.watcher(Client(STALE), load=load)
        for n in (1.0, 2.0):
            self.assertEqual(w.poll(n), [])
        self.assertEqual(len(w.poll(3.0)), 1)


if __name__ == "__main__":
    unittest.main()
