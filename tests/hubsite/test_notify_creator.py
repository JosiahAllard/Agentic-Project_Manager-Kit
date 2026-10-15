"""kitlib.hubsite.api routes ("Routes" in its docstring): a pending item goes to the session that
created it (its `from` key) while that session can be reached, and to the PM when it cannot, when
it was handed over and not handled within the ack window, or after a server restart.
"""
import threading
import time
import unittest

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, OWNER

from tests.hubsite.test_direct_questions import DirectCase


class RouteCase(DirectCase):
    def setUp(self):
        super().setUp()
        self.t = [1000.0]
        self.api = api.Api(self.store, clock=self.api.clock, session_keys=self.session_keys,
                           now=lambda: self.t[0])

    def todo(self, who='kit', done=True):
        tid = self.ok('POST', '/todos', AGENT, {'title': 'x', 'commands': ['dir'],
                                                'from': who})['id']
        if done:
            self.ok('POST', '/todos/%s/done' % tid, OWNER, {})
        return tid

    def wait(self, timeout, **query):
        q = {'pending': '1', 'timeout': str(timeout)}
        q.update(query)
        return self.ok('GET', '/wait', AGENT, query=q)

    def routes(self):
        return {p['id']: p['route'] for p in self.pending()}

    def heard(self, key='kit'):
        """A wait for key that ends at once with nothing: the key is reachable."""
        self.wait(0, **{'for': key})


class ReachTest(RouteCase):
    def test_a_creator_never_heard_from_is_unreachable_and_the_pm_has_its_item(self):
        tid = self.todo()
        self.assertEqual(self.routes(), {tid: 'pm'})
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [tid])
        # the creator still lists its own item
        self.assertEqual(self.ids(self.pending(**{'for': 'kit'})), [tid])

    def test_a_creator_heard_within_the_grace_has_its_item_and_the_pm_does_not(self):
        self.heard()
        tid = self.todo()
        self.t[0] += api.REACH_GRACE_S - 1
        self.assertEqual(self.routes(), {tid: 'kit'})
        self.assertEqual(self.pending(**{'for': 'pm'}), [])
        self.t[0] += 2
        self.assertEqual(self.routes(), {tid: 'pm'})
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [tid])

    def test_a_blocked_creator_wait_keeps_it_reachable_past_the_grace(self):
        real, during = self.store.wait, []

        def blocked(since, timeout):
            self.t[0] += api.REACH_GRACE_S * 10
            during.append(dict(self.routes()))
            return real(since, 0)
        tid = self.todo()
        self.store.wait = blocked
        try:
            self.wait(0.05, **{'for': 'zeta'})       # zeta has no item: its wait blocks
        finally:
            self.store.wait = real
        self.assertEqual(during[0], {tid: 'pm'}, 'a wait for zeta made kit reachable')
        mark = len(during)
        self.store.wait = blocked
        try:
            # it leaves tid out, so nothing is handed over: only its being blocked counts
            self.wait(0.05, **{'for': 'kit', 'except': tid})
        finally:
            self.store.wait = real
        self.assertEqual(during[mark], {tid: 'kit'})
        self.assertEqual(self.routes(), {tid: 'kit'}, 'its end was not heard')
        self.t[0] += api.REACH_GRACE_S + 1
        self.assertEqual(self.routes(), {tid: 'pm'})

    def test_an_owner_call_or_a_pm_wait_makes_no_session_reachable(self):
        tid = self.todo()
        self.ok('GET', '/wait', OWNER, query={'pending': '1', 'for': 'kit', 'timeout': '0'})
        self.wait(0)
        self.wait(0, **{'for': 'pm'})
        self.assertEqual(self.routes(), {tid: 'pm'})

    def test_a_restart_routes_every_item_to_the_pm(self):
        self.heard()
        tid = self.todo()
        self.ids(self.pending(**{'for': 'kit'}))
        self.assertEqual(self.routes(), {tid: 'kit'})
        self.api = api.Api(self.store, clock=self.api.clock, session_keys=self.session_keys,
                           now=lambda: self.t[0])
        self.assertEqual(self.routes(), {tid: 'pm'})

    def test_a_free_text_from_is_the_pms_whatever_waits(self):
        self.heard('kit')
        tid = self.todo(who='repo-lead-x9')         # a free-text session name, not a key
        self.assertEqual([(p['id'], p['from'], p['route']) for p in self.pending()],
                         [(tid, 'pm', 'pm')])


class HandOverTest(RouteCase):
    def test_a_handed_item_is_the_creators_for_the_ack_window_then_the_pms(self):
        self.heard()
        tid = self.todo()
        self.assertEqual(self.ids(self.wait(0, **{'for': 'kit'})['pending']), [tid])
        # handed over: the creator has it even when its wait has not come back
        self.t[0] += api.ACK_WINDOW_S - 1
        self.assertEqual(self.routes(), {tid: 'kit'})
        self.assertEqual(self.pending(**{'for': 'pm'}), [])
        self.t[0] += 2
        self.assertEqual(self.routes(), {tid: 'pm'})
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [tid])

    def test_the_window_counts_from_the_first_hand_over_only(self):
        self.heard()
        tid = self.todo()
        self.pending(**{'for': 'kit'})
        self.t[0] += api.ACK_WINDOW_S - 1
        self.heard()
        self.pending(**{'for': 'kit'})             # a creator that reads it again each time
        self.t[0] += 2
        self.assertEqual(self.routes(), {tid: 'pm'})

    def test_an_acked_item_is_pending_for_nobody(self):
        self.heard()
        tid = self.todo()
        self.pending(**{'for': 'kit'})
        self.ok('POST', '/todos/%s/ack' % tid, AGENT, {})
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.pending(**{'for': 'pm'}), [])
        self.assertEqual(self.pending(**{'for': 'kit'}), [])

    def test_a_reopened_and_done_again_item_is_handed_over_afresh(self):
        self.heard()
        tid = self.todo()
        self.pending(**{'for': 'kit'})
        self.ok('POST', '/todos/%s/done' % tid, OWNER, {'done': False})
        self.assertEqual(self.pending(), [])
        self.t[0] += api.ACK_WINDOW_S + 1
        self.ok('POST', '/todos/%s/done' % tid, OWNER, {})
        self.heard()
        self.assertEqual(self.routes(), {tid: 'kit'}, 'the old hand-over was kept')


class QuestionRouteTest(RouteCase):
    def test_an_answered_question_from_a_live_creator_is_in_both_lists(self):
        self.heard()
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.assertEqual(self.routes(), {qid: 'kit'})
        self.assertEqual(self.ids(self.pending(**{'for': 'kit'})), [qid])
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [qid])


class PmWaitTest(RouteCase):
    def test_a_pm_wait_sleeps_through_a_creators_item_and_wakes_when_it_falls_back(self):
        self.api = api.Api(self.store, clock=self.api.clock, session_keys=self.session_keys)
        self.api.ack_window_s = 0.6
        self.heard()
        tid = self.todo()
        self.assertEqual(self.ids(self.wait(0, **{'for': 'kit'})['pending']), [tid])
        revision = self.store.revision
        out = {}

        def waiter():
            out['got'] = self.wait(20, **{'for': 'pm'})
        start = time.monotonic()
        t = threading.Thread(target=waiter)
        t.start()
        self.addCleanup(t.join, 30)
        time.sleep(0.2)
        self.assertTrue(t.is_alive(), 'the creator\'s item ended the PM\'s wait')
        t.join(10)
        self.assertFalse(t.is_alive(), 'the fallback did not wake the PM\'s wait')
        self.assertLess(time.monotonic() - start, 5)
        got = out['got']
        self.assertEqual((self.ids(got['pending']), got['timed_out']), ([tid], False))
        self.assertEqual(self.store.revision, revision, 'something wrote to wake it')

    def test_a_creator_wait_ends_on_its_own_item_and_the_pm_wait_does_not(self):
        self.api = api.Api(self.store, clock=self.api.clock, session_keys=self.session_keys)
        out = {}

        def creator():
            out['kit'] = self.wait(20, **{'for': 'kit'})

        def pm():
            out['pm'] = self.wait(1, **{'for': 'pm'})
        kit = threading.Thread(target=creator)
        kit.start()
        self.addCleanup(kit.join, 30)
        time.sleep(0.2)
        tid = self.todo()
        kit.join(10)
        self.assertEqual(self.ids(out['kit']['pending']), [tid])
        pmt = threading.Thread(target=pm)
        pmt.start()
        self.addCleanup(pmt.join, 30)
        pmt.join(10)
        self.assertEqual((out['pm']['pending'], out['pm']['timed_out']), ([], True))


if __name__ == '__main__':
    unittest.main()
