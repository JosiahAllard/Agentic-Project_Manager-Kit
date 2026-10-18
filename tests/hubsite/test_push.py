"""kitlib.hubsite.push: the hub tells the session that asked, and the PM, when the owner answers
a question or marks a to-do done, and keeps telling them until they pick it up
(docs/action-hub.md "Push notifications"). A fake delivery object stands in for the host's.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.hubsite import api, auth, push, serve, store as store_mod
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import Store

from tests.hubsite import httpfix

OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]
PLANTED = 'PUSHPROOF title text'
NOTE = 'PUSHPROOF note text'


class Ticks(object):
    """The store's and Api's clock: a new whole second on every call, as the real one has."""

    def __init__(self):
        self.n = 0
        self.frozen = False

    def __call__(self):
        self.n += 0 if self.frozen else 1
        return '2026-01-02T%02d:%02d:%02d+00:00' % (self.n // 3600 % 24, self.n // 60 % 60, self.n % 60)


class FakePush(object):
    """deliver() records (recipient, text) and answers the state set for the recipient."""

    def __init__(self, pm_names=()):
        self.calls = []
        self.states = {}
        self.ambiguous = set()
        self.pm_names = set(pm_names)
        self.got = threading.Event()

    def deliver(self, recipient, text):
        self.calls.append((recipient, text))
        self.got.set()
        return push.Sent(self.states.get(recipient, push.WRITTEN), 'name-' + recipient, 'sid',
                         recipient in self.ambiguous)

    def is_pm(self, recipient):
        return recipient in self.pm_names

    def to(self, recipient):
        return [text for r, text in self.calls if r == recipient]


class PushCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hubpush-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.folder = os.path.join(self.root, 'state')
        os.mkdir(self.folder)
        self.ticks = Ticks()
        self.store = Store.open(os.path.join(self.root, 'store'), clock=self.ticks)
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, clock=self.ticks, session_keys=lambda: {'kit', 'zeta'})
        self.push = FakePush()
        self.t = 1000000.0
        self.worker = self.make()

    def make(self):
        w = push.PushWorker(self.store, self.api, self.push, self.folder, clock=lambda: self.t)
        w.begin()
        return w

    def ok(self, method, path, role, body=None, query=None):
        status, obj = self.api.call(method, BASE + path, role, body, query)
        self.assertEqual((status, obj.get('error')), (200, None), obj)
        return obj

    def ask(self, **kw):
        body = {'title': PLANTED, 'repo': 'kit', 'options': OPTIONS}
        body.update(kw)
        return self.ok('POST', '/questions', AGENT, body)['id']

    def answer(self, qid, choice=0):
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': choice, 'note': NOTE})

    def todo(self, **kw):
        body = {'title': PLANTED, 'detail': NOTE, 'repo': 'kit', 'commands': ['dir']}
        body.update(kw)
        return self.ok('POST', '/todos', AGENT, body)['id']

    def done(self, tid, done=True):
        self.ok('POST', '/todos/%s/done' % tid, OWNER, {'done': done})

    def ack(self, tid):
        self.ok('POST', '/todos/%s/ack' % tid, AGENT, {})

    def pick_up(self, key):
        self.ok('GET', '/pending', AGENT, query={'for': key})

    def later(self, seconds):
        self.t += seconds
        self.worker.poll()

    def attempts(self, recipient):
        return [int(line.rsplit('attempt:', 1)[1].split()[0])
                for text in self.push.to(recipient) for line in text.splitlines()
                if line.startswith('[hub push]')]

    def undelivered(self):
        path = os.path.join(self.folder, push.UNDELIVERED)
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f]


class FiresTest(PushCase):
    def test_an_answer_pushes_once_to_the_asking_session_and_once_to_the_pm(self):
        qid = self.ask(**{'from': 'kit'})
        self.worker.poll()
        self.assertEqual(self.push.calls, [], 'an open question is nothing to push')
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['kit', 'pm'])
        self.assertIn('[hub push] question %s answered repo:kit from:kit route:kit attempt:1' % qid,
                      self.push.to('kit')[0])
        self.assertIn('route:pm attempt:1', self.push.to('pm')[0])
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2, 'pushed again with nothing new')

    def test_a_done_todo_pushes_once_to_its_session_and_once_to_the_pm(self):
        tid = self.todo(**{'from': 'kit'})
        self.worker.poll()
        self.done(tid)
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['kit', 'pm'])
        text = self.push.to('kit')[0]
        self.assertIn('[hub push] todo %s done repo:kit from:kit route:kit attempt:1' % tid, text)
        self.assertIn('A done mark is not proof: verify each step on disk, then: hub todo ack %s'
                      % tid, text)

    def test_the_pms_own_items_and_a_session_that_is_the_pm_push_to_the_pm_once(self):
        self.push.pm_names.add('zeta')
        q1, q2 = self.ask(), self.ask(**{'from': 'zeta'})
        self.answer(q1)
        self.answer(q2)
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['pm'])
        self.assertIn(q1, self.push.calls[0][1])
        self.assertIn(q2, self.push.calls[0][1])

    def test_a_change_that_leaves_the_fingerprint_alone_pushes_nothing_new(self):
        # red proof: PushWorker._changed firing on every event, not only on `fp != old`, sends
        # the title change and the ack below at once, before the next slot
        tid = self.todo(**{'from': 'kit'})
        self.done(tid)
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2)
        self.store.update('todos', tid, {'title': 'changed'})
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2, 'a title change pushed')
        self.ack(tid)
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2, 'a pm_ack pushed')

    def test_a_reanswer_pushes_again(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        self.answer(qid, 1)
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 1])
        self.assertEqual(self.attempts('pm'), [1, 1])
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('kit'), [1, 1],
                         'a session that held it before is acknowledged by the written push')
        self.assertEqual(self.attempts('pm'), [1, 1, 2])

    def test_a_reanswer_in_the_same_second_pushes_again(self):
        # red proof: fingerprint() without the answer's digest sees the same status and time
        qid = self.ask(**{'from': 'kit'})
        self.ticks.frozen = True
        self.answer(qid)
        self.worker.poll()
        self.answer(qid, 1)
        self.worker.poll()
        self.assertEqual(self.attempts('pm'), [1, 1])

    def test_a_reopen_pushes_once_and_is_not_retried_after_written(self):
        tid = self.todo(**{'from': 'kit'})
        self.done(tid)
        self.worker.poll()
        self.done(tid, False)
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 4)
        self.assertIn('[hub push] todo %s open' % tid, self.push.to('kit')[1])
        self.assertIn('Reopened by the owner', self.push.to('kit')[1])
        self.later(3 * 3600)
        self.assertEqual(len(self.push.calls), 4, 'a written reopen was sent again')

    def test_a_todo_the_owner_adds_goes_to_the_pm_only_until_acked(self):
        tid = self.ok('POST', '/todos', OWNER, {'title': PLANTED})['id']
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['pm'])
        self.assertIn('The owner added it: hub view %s, then hub todo ack %s' % (tid, tid),
                      self.push.calls[0][1])
        self.ack(tid)
        self.later(3 * 3600)
        self.assertEqual(len(self.push.calls), 1)


class RetryTest(PushCase):
    def test_written_and_not_acked_is_sent_again_on_the_schedule(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.later(push.SCHEDULE_S[0] - 1)
        self.assertEqual(self.attempts('kit'), [1])
        self.later(1)
        self.assertEqual(self.attempts('kit'), [1, 2])
        self.later(push.SCHEDULE_S[1])
        self.later(push.SCHEDULE_S[2])
        self.assertEqual(self.attempts('kit'), [1, 2, 3, 4])
        self.assertEqual(self.attempts('pm'), [1, 2, 3, 4])

    def test_an_uncertain_write_waits_for_the_next_slot(self):
        self.push.states['kit'] = push.UNCERTAIN
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.worker.poll()
        self.later(push.SCHEDULE_S[0] - 1)
        self.assertEqual(self.attempts('kit'), [1], 'an uncertain write was sent again at once')
        self.later(1)
        self.assertEqual(self.attempts('kit'), [1, 2])

    def test_a_deliver_that_raises_counts_as_uncertain(self):
        def boom(recipient, text):
            raise OSError('host fault')
        self.push.deliver = boom
        qid = self.ask()
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([(l['recipient'], l['reason']) for l in self.undelivered()],
                         [('pm', push.UNCERTAIN)])


class AckTest(PushCase):
    def test_the_session_picking_it_up_stops_its_pushes_and_the_pm_goes_on(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('kit'), [1])
        self.assertEqual(self.attempts('pm'), [1, 2])

    def test_pm_ack_stops_the_pm(self):
        tid = self.todo()
        self.done(tid)
        self.worker.poll()
        self.ack(tid)
        self.later(3 * 3600)
        self.assertEqual(self.attempts('pm'), [1])

    def test_a_logged_question_stops_both(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.store.update('questions', qid, {'status': 'logged'})
        self.later(3 * 3600)
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1], [1]))

    def test_a_newer_answer_supersedes_the_push_in_flight(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('kit'), [1, 2])
        self.answer(qid, 1)
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 2, 1], 'the new answer starts afresh')

    def test_a_withdrawn_item_is_dropped(self):
        tid = self.todo(**{'from': 'kit'})
        self.done(tid)
        self.worker.poll()
        self.store.delete('todos', tid)
        self.later(3 * 3600)
        self.assertEqual(len(self.push.calls), 2)
        with open(os.path.join(self.folder, push.OUTBOX), encoding='utf-8') as f:
            self.assertEqual(json.load(f), {})


class CombineTest(PushCase):
    def test_items_due_together_go_to_a_recipient_in_one_message(self):
        ids = [self.todo() for _ in range(3)]
        for tid in ids:
            self.done(tid)
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 1)
        for tid in ids:
            self.assertIn('todo %s done' % tid, self.push.calls[0][1])

    def test_past_max_items_the_message_says_how_many_more(self):
        with mock.patch.object(push, 'MAX_ITEMS', 2):
            for _ in range(3):
                self.done(self.todo())
            self.worker.poll()
        self.assertTrue(self.push.calls[0][1].endswith('+1 more: hub pending --for pm'))
        self.assertEqual(self.attempts('pm'), [1, 1])


class UndeliveredTest(PushCase):
    def test_no_live_session_writes_one_line_without_the_items_text(self):
        # red proof: PushWorker._send without its `self._note(...)` for a state that is not
        # written leaves the log empty
        self.push.states['kit'] = push.NO_SESSION
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        lines = self.undelivered()
        self.assertEqual(len(lines), 1)
        self.assertEqual(set(lines[0]), {'ts', 'kind', 'id', 'status', 'recipient', 'name',
                                         'attempt', 'reason'})
        self.assertEqual((lines[0]['kind'], lines[0]['id'], lines[0]['recipient'],
                          lines[0]['attempt'], lines[0]['reason']),
                         ('question', qid, 'kit', 1, push.NO_SESSION))
        with open(os.path.join(self.folder, push.UNDELIVERED), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('PUSHPROOF', raw)

    def test_an_ambiguous_name_adds_a_line_even_when_written(self):
        self.push.ambiguous.add('kit')
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([l['reason'] for l in self.undelivered()], [push.AMBIGUOUS])

    def test_after_a_day_it_gives_up_with_one_line(self):
        self.push.states['pm'] = push.NO_SESSION
        qid = self.ask()
        self.answer(qid)
        self.worker.poll()
        while self.t < 1000000.0 + push.GIVE_UP_S + push.HOURLY_S:
            self.later(push.HOURLY_S)
        reasons = [l['reason'] for l in self.undelivered()]
        self.assertEqual(reasons.count(push.GAVE_UP), 1)
        self.assertEqual(reasons[-1], push.GAVE_UP)
        sent = len(self.push.calls)
        self.later(push.HOURLY_S)
        self.assertEqual(len(self.push.calls), sent, 'sent after giving up')

    def test_the_log_rotates_past_its_size(self):
        self.push.states['pm'] = push.NO_SESSION
        path = os.path.join(self.folder, push.UNDELIVERED)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('x' * 64)
        with mock.patch.object(push, 'LOG_MAX', 32):
            self.answer(self.ask())
            self.worker.poll()
        self.assertTrue(os.path.exists(path + '.1'))
        self.assertEqual(len(self.undelivered()), 1)


class RestartTest(PushCase):
    def test_the_outbox_outlives_the_worker_and_a_due_retry_still_goes(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.worker = self.make()
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2, 'a written push was sent again at the start')
        self.later(push.SCHEDULE_S[0])
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1, 2], [1, 2]))

    def test_an_item_answered_again_while_down_fires_afresh_at_the_start(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.answer(qid, 1)
        self.worker = self.make()
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 1])

    def test_a_broken_outbox_is_read_as_empty(self):
        with open(os.path.join(self.folder, push.OUTBOX), 'w', encoding='utf-8') as f:
            f.write('{"question:Q-001": {"kind": "question"}, "x": 1')
        self.worker = self.make()
        self.worker.poll()
        self.assertEqual(self.push.calls, [])


class ResetTest(unittest.TestCase):
    def test_a_reset_rebaselines_without_pushing_again_and_fires_only_real_changes(self):
        root = tempfile.mkdtemp(prefix='kit-hubpush-')
        self.addCleanup(shutil.rmtree, root, True)
        ticks = Ticks()
        with mock.patch.object(store_mod, 'RING', 8):
            st = Store.open(os.path.join(root, 'store'), clock=ticks)
        self.addCleanup(st.close)
        a = api.Api(st, clock=ticks, session_keys=lambda: {'kit'})
        fake = FakePush()
        w = push.PushWorker(st, a, fake, root, clock=lambda: 5.0)
        w.begin()

        def call(method, path, role, body):
            status, obj = a.call(method, BASE + path, role, body)
            self.assertEqual(status, 200, obj)
            return obj
        qid = call('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS, 'from': 'kit'})['id']
        call('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        w.poll()
        self.assertEqual(len(fake.calls), 2)
        tid = call('POST', '/todos', AGENT, {'title': 't', 'commands': ['dir']})['id']
        for i in range(20):
            st.update('todos', tid, {'title': 't%d' % i})
        self.assertTrue(st.changes_since(w._rev).reset)
        w.poll()
        self.assertEqual(len(fake.calls), 2, 'a reset pushed what was pushed already')
        call('POST', '/todos/%s/done' % tid, OWNER, {'done': True})
        for i in range(20):
            st.update('todos', tid, {'title': 'u%d' % i})
        w.poll()
        self.assertEqual([r for r, _ in fake.calls[2:]], ['pm'], 'a done hidden by a reset')
        self.assertIn('todo %s done' % tid, fake.calls[2][1])


class TextTest(PushCase):
    def test_the_text_never_carries_a_title_a_note_or_an_answer(self):
        qid = self.ask(**{'from': 'kit'})
        tid = self.todo(**{'from': 'kit'})
        self.answer(qid)
        self.done(tid)
        self.worker.poll()
        self.assertTrue(self.push.calls)
        for _r, text in self.push.calls:
            self.assertNotIn('PUSHPROOF', text)
            self.assertNotIn('first', text)
            self.assertNotIn('<', text)
            self.assertNotIn('>', text)
        with open(os.path.join(self.folder, push.OUTBOX), encoding='utf-8') as f:
            self.assertNotIn('PUSHPROOF', f.read())

    def test_header_fields_are_cut_to_a_safe_set(self):
        self.assertEqual(push.field('a b<c>\n' + 'x' * 80), ('a_b_c__' + 'x' * 80)[:64])
        self.assertEqual(push.field(None), '-')


class ThreadTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: self.assertEqual(httpfix.http_threads(), [],
                                                 'a listener thread outlived its test'))
        self.root = tempfile.mkdtemp(prefix='kit-hubpush-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tokens = os.path.join(self.root, 'tokens')
        self.folder = os.path.join(self.root, 'state')
        os.mkdir(self.folder)
        self.settings = hub_settings.settings(None)._replace(
            port=0, store=os.path.join(self.root, 'store'))
        self.lines = []

    def test_the_hub_runs_the_worker_and_closes_it_around_the_store(self):
        fake = FakePush()
        hub = serve.build(self.settings, serve.Sources(tokens=self.tokens, push=fake,
                                                       push_dir=self.folder,
                                                       static_dir=os.path.join(self.root, 'web')),
                          log=lambda level, text: self.lines.append((level, text)))
        self.addCleanup(hub.close)
        self.assertIsInstance(hub.push, push.PushWorker)
        self.assertTrue(any('push notifications on' in n for n in hub.notes))
        hub.start()
        status, obj = hub.api.call('POST', BASE + '/questions', AGENT,
                                   {'title': 't', 'options': OPTIONS})
        self.assertEqual(status, 200, obj)
        status, obj = hub.api.call('POST', BASE + '/questions/%s/answer' % obj['id'], OWNER,
                                   {'choice': 0})
        self.assertEqual(status, 200, obj)
        self.assertTrue(fake.got.wait(10), 'the running worker pushed nothing')
        order = []
        real_stop, real_close = hub.push.stop, hub.store.close

        def stop(timeout=push.STOP_S):
            order.append(('push.stop', timeout))
            return real_stop(timeout)

        def close():
            order.append(('store.close',))
            return real_close()
        hub.push.stop, hub.store.close = stop, close
        thread = hub.push._thread
        self.assertTrue(hub.close(timeout=3))
        self.assertEqual(order, [('push.stop', 0), ('store.close',), ('push.stop', 3)])
        self.assertFalse(thread.is_alive())
        with open(os.path.join(self.folder, push.OUTBOX), encoding='utf-8') as f:
            self.assertIn('question:', f.read())

    def test_no_push_without_a_folder(self):
        hub = serve.build(self.settings, serve.Sources(tokens=self.tokens, push=FakePush(),
                                                       static_dir=os.path.join(self.root, 'web')))
        self.addCleanup(hub.close)
        self.assertIsNone(hub.push)

    def test_a_worker_that_will_not_start_leaves_the_hub_serving(self):
        hub = serve.build(self.settings, serve.Sources(tokens=self.tokens, push=FakePush(),
                                                       push_dir=self.folder,
                                                       static_dir=os.path.join(self.root, 'web')),
                          log=lambda level, text: self.lines.append((level, text)))
        self.addCleanup(hub.close)
        hub.push.begin = mock.Mock(side_effect=OSError('nope'))
        self.assertTrue(hub.start())
        self.assertIsNone(hub.push)
        self.assertIn('push notifications did not start', ' '.join(t for _l, t in self.lines))
        self.assertTrue(hub.close(timeout=3))


if __name__ == '__main__':
    unittest.main()
