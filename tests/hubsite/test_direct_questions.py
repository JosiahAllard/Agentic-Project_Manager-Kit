"""kitlib.hubsite.api: a question comes from a session (`from`, a launcher session key or `pm`),
and that session reads and waits for its own answers (`for`) while the PM still sees every one.
"""
import json
import shutil
import tempfile
import threading
import time
import unittest

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import Store

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]


class DirectCase(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix='kit-hubdirect-')
        self.addCleanup(shutil.rmtree, root, True)
        self.store = Store.open(root, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.keys = {'kit', 'zeta'}
        self.reads = []
        self.api = api.Api(self.store, clock=CLOCK, session_keys=self.session_keys)

    def session_keys(self):
        self.reads.append(1)
        return set(self.keys)

    def call(self, method, path, role, body=None, query=None):
        status, obj = self.api.call(method, BASE + path, role, body, query)
        json.dumps(obj)
        return status, obj

    def ok(self, method, path, role, body=None, query=None):
        status, obj = self.call(method, path, role, body, query)
        self.assertEqual((status, obj.get('error')), (200, None))
        return obj

    def refused(self, field, method, path, role, body=None, query=None):
        before = self.store.snapshot()
        status, obj = self.call(method, path, role, body, query)
        self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                         (400, 'invalid', field), obj)
        self.assertEqual(self.store.snapshot(), before, 'a refused call wrote')
        return obj

    def ask(self, **kw):
        body = {'title': 't', 'repo': 'kit', 'options': OPTIONS}
        body.update(kw)
        return self.ok('POST', '/questions', AGENT, body)['id']

    def answer(self, qid):
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})

    def pending(self, **query):
        return self.ok('GET', '/pending', AGENT, query=query)['items']

    def ids(self, items):
        return [p['id'] for p in items]


class FromTest(DirectCase):
    def test_a_question_from_a_session_key_is_stored_from_it(self):
        qid = self.ask(**{'from': 'kit'})
        self.assertEqual(self.store.get('questions', qid)['from'], 'kit')

    def test_a_question_from_no_session_is_the_pms(self):
        qid = self.ask()
        self.assertEqual(self.store.get('questions', qid)['from'], 'pm')
        qid = self.ask(**{'from': '  '})
        self.assertEqual(self.store.get('questions', qid)['from'], 'pm')
        self.assertEqual(self.reads, [], 'the PM\'s own key needs no read of the sessions')

    def test_a_question_from_an_unknown_key_is_refused_and_named_without_a_path(self):
        for who in ('coordinator', 'Kit', 'you', 'kitx'):
            with self.subTest(who=who):
                obj = self.refused('from', 'POST', '/questions', AGENT,
                                   {'title': 't', 'options': OPTIONS, 'from': who})
                said = obj['error']['message']
                self.assertIn('not a session the hub knows', said)
                self.assertIn('kit, pm, zeta', said)
                self.assertNotIn('sessions.json', said)
                self.assertNotIn('\\', said)

    def test_the_session_keys_are_read_afresh_on_each_add(self):
        self.refused('from', 'POST', '/questions', AGENT,
                     {'title': 't', 'options': OPTIONS, 'from': 'newrepo'})
        self.keys.add('newrepo')
        qid = self.ask(**{'from': 'newrepo'})
        self.assertEqual(self.store.get('questions', qid)['from'], 'newrepo')

    def test_unreadable_session_keys_leave_only_the_pm(self):
        def broken():
            raise OSError(r'C:\secret\sessions.json is locked')
        self.api = api.Api(self.store, clock=CLOCK, session_keys=broken)
        obj = self.refused('from', 'POST', '/questions', AGENT,
                           {'title': 't', 'options': OPTIONS, 'from': 'kit'})
        self.assertNotIn('secret', obj['error']['message'])
        self.assertIn('known: pm)', obj['error']['message'])
        qid = self.ask(**{'from': 'pm'})
        self.assertEqual(self.store.get('questions', qid)['from'], 'pm')

    def test_the_default_reader_gives_nothing_when_the_launcher_cannot_be_read(self):
        # the launcher's settings refuse (a ConfigError, an OSError): only the PM is known
        from kitlib.launcher import settings as lsettings
        saved = lsettings.load

        def refusing(home=None):
            raise OSError('no config')
        lsettings.load = refusing
        try:
            self.assertEqual(api.launcher_keys(), set())
            self.assertEqual(api.Api(self.store, clock=CLOCK,
                                     session_keys=api.launcher_keys).session_keys(), {'pm'})
        finally:
            lsettings.load = saved

    def test_a_to_dos_free_text_from_is_kept_and_routed_to_the_pm(self):
        tid = self.ok('POST', '/todos', AGENT, {'title': 'x', 'commands': ['dir'],
                                                'from': 'the kit coordinator'})['id']
        self.assertEqual(self.store.get('todos', tid)['from'], 'the kit coordinator')
        self.ok('POST', '/todos/%s/done' % tid, OWNER, {})
        self.assertEqual([(p['id'], p['from']) for p in self.pending()], [(tid, 'pm')])
        self.assertEqual(self.pending(**{'for': 'kit'}), [])


class ForTest(DirectCase):
    def test_pending_items_carry_the_key_they_are_for(self):
        mine, theirs, pms = self.ask(**{'from': 'kit'}), self.ask(**{'from': 'zeta'}), \
            self.ask()
        for qid in (mine, theirs, pms):
            self.answer(qid)
        kit_todo = self.ok('POST', '/todos', AGENT, {'title': 'x', 'commands': ['dir'],
                                                     'from': 'kit'})['id']
        self.ok('POST', '/todos/%s/done' % kit_todo, OWNER, {})
        owners = self.ok('POST', '/todos', OWNER, {'title': 'mine'})['id']
        story = self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's'})
        got = {p['id']: p['from'] for p in self.pending()}
        self.assertEqual(got, {mine: 'kit', theirs: 'zeta', pms: 'pm', kit_todo: 'kit',
                               owners: 'pm', story['id']: 'pm'})
        self.assertNotIn('sender', self.pending()[0])

    def test_pending_for_a_key_lists_only_its_items_and_without_for_lists_all(self):
        mine, theirs = self.ask(**{'from': 'kit'}), self.ask(**{'from': 'zeta'})
        pms = self.ask()
        for qid in (mine, theirs, pms):
            self.answer(qid)
        self.assertEqual(self.ids(self.pending(**{'for': 'kit'})), [mine])
        self.assertEqual(self.ids(self.pending(**{'for': 'zeta'})), [theirs])
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [pms])
        self.assertEqual(sorted(self.ids(self.pending())), sorted([mine, theirs, pms]))

    def test_a_question_whose_session_went_away_is_the_pms(self):
        qid = self.ask(**{'from': 'zeta'})
        self.answer(qid)
        self.keys.discard('zeta')
        self.assertEqual([(p['id'], p['from']) for p in self.pending()], [(qid, 'pm')])
        self.assertEqual(self.ids(self.pending(**{'for': 'pm'})), [qid])

    def test_a_filtered_read_leaves_the_item_pending_for_every_reader_until_logged(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        version = self.store.get('questions', qid)['version']
        self.assertEqual(self.ids(self.pending(**{'for': 'kit'})), [qid])
        got = self.ok('GET', '/wait', AGENT, query={'pending': '1', 'for': 'kit', 'timeout': '0'})
        self.assertEqual(self.ids(got['pending']), [qid])
        # nothing was marked, consumed or hidden: the PM's unfiltered read still lists it
        self.assertEqual(self.ids(self.pending()), [qid])
        self.assertEqual(self.ids(self.pending(**{'for': 'kit'})), [qid])
        self.assertEqual(self.store.get('questions', qid)['version'], version)
        self.ok('POST', '/questions/%s/log' % qid, AGENT,
                {'ruling': 'logged', 'if_version': version})
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.pending(**{'for': 'kit'}), [])


class WaitForTest(DirectCase):
    def wait(self, timeout, **query):
        q = {'pending': '1', 'timeout': str(timeout)}
        q.update(query)
        return self.ok('GET', '/wait', AGENT, query=q)

    def test_for_needs_pending(self):
        self.refused('for', 'GET', '/wait', AGENT, query={'for': 'kit', 'timeout': '0'})

    def test_another_sessions_answer_does_not_end_a_wait_for_kit(self):
        theirs = self.ask(**{'from': 'zeta'})
        self.answer(theirs)
        start = time.monotonic()
        got = self.wait(0.3, **{'for': 'kit'})
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        self.assertEqual((got['pending'], got['timed_out']), ([], True))
        # without for the same item ends the wait at once
        self.assertEqual(self.ids(self.wait(5)['pending']), [theirs])

    def test_a_wait_for_kit_sleeps_through_other_writes_and_wakes_on_its_answer(self):
        mine, theirs = self.ask(**{'from': 'kit'}), self.ask(**{'from': 'zeta'})
        since = self.store.revision
        out = {}

        def waiter():
            out['got'] = self.wait(20, **{'for': 'kit', 'since': str(since)})
        t = threading.Thread(target=waiter)
        t.start()
        self.addCleanup(t.join, 30)
        time.sleep(0.2)
        self.answer(theirs)
        self.ok('POST', '/todos', AGENT, {'title': 'x', 'commands': ['dir']})
        time.sleep(0.3)
        self.assertTrue(t.is_alive(), 'another session\'s answer woke the wait for kit')
        self.answer(mine)
        t.join(10)
        self.assertFalse(t.is_alive())
        got = out['got']
        self.assertEqual((self.ids(got['pending']), got['timed_out'], got['reset']),
                         ([mine], False, False))
        # the changes are all of them since `since`, the other session's answer too
        self.assertIn(theirs, [c['id'] for c in got['changes']])

    def test_a_wait_for_kit_that_times_out_says_so_even_after_other_writes(self):
        since = self.store.revision
        theirs = self.ask(**{'from': 'zeta'})
        self.answer(theirs)
        got = self.wait(0.2, **{'for': 'kit', 'since': str(since)})
        self.assertEqual((got['pending'], got['timed_out']), ([], True))
        self.assertEqual(got['revision'], self.store.revision)

    def test_a_wait_without_for_is_unchanged(self):
        since = self.store.revision
        self.ask(**{'from': 'zeta'})
        got = self.ok('GET', '/wait', AGENT, query={'since': str(since), 'timeout': '5'})
        self.assertEqual((got['timed_out'], got['pending']), (False, []))
        self.assertEqual(len(got['changes']), 1)


if __name__ == '__main__':
    unittest.main()
