"""kitlib.hubsite.push: the hub tells the session that asked, and the PM, when the owner answers
a question or marks a to-do done, and keeps telling them until they pick it up
(docs/action-hub.md "Push notifications"). A fake delivery object stands in for the host's.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.hubsite import api, auth, push, serve, store as store_mod
from kitlib.hubsite import settings as hub_settings
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import HubError, Store

from tests.hubsite import httpfix

OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]
# ledger ids, built so no file holds a ruling id (tests/scrub)
R1 = 'R-' + '0001'
PLANTED = 'PUSHPROOF title text'
NOTE = 'PUSHPROOF note text'
REAL_HEARTBEAT = push.watcher_heartbeat      # the default reader, before no_watcher patches it


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


def no_watcher(case):
    """Patch the default heartbeat reader for `case`: a running worker reads it, and the real
    one reads the machine's config (the home fence)."""
    patcher = mock.patch.object(push, 'watcher_heartbeat', lambda: None)
    patcher.start()
    case.addCleanup(patcher.stop)


class PushCase(unittest.TestCase):
    def setUp(self):
        no_watcher(self)
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
        if 'record' not in kw:
            body['class'] = 'go'            # a question needs a record or a class
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

    def test_a_session_that_picked_up_a_first_answer_or_done_before_the_worker_saw_it_hears_nothing(self):
        # red proof: PushWorker._fire without its `held and first` skip sends both once
        qid = self.ask(**{'from': 'kit'})
        tid = self.todo(**{'from': 'kit'})
        self.worker.poll()
        self.answer(qid)
        self.done(tid)
        self.pick_up('kit')
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['pm'])
        self.assertIn('question %s answered' % qid, self.push.calls[0][1])
        self.assertIn('todo %s done' % tid, self.push.calls[0][1])
        self.later(3 * 3600)
        self.assertEqual(self.push.to('kit'), [])

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

    def test_a_filed_question_goes_on_pushing_its_asker_until_it_is_picked_up_or_logged(self):
        # red proof: the fingerprint's raw status (answered -> filed fires nothing and drops the
        # push) or a question acknowledged once it is no longer `answered`
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.store.update('questions', qid, {'status': 'filed'})
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('kit'), [1, 2], 'its asker reads it')
        self.assertEqual(self.attempts('pm'), [1], 'the asker, not the PM, handles it')
        self.store.update('questions', qid, {'status': 'logged'})
        self.later(3 * 3600)
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1, 2], [1]))

    def test_a_question_filed_before_the_worker_saw_it_answered_still_pushes_its_asker(self):
        qid = self.ask(**{'from': 'kit'})
        self.worker.poll()
        self.answer(qid)
        self.store.update('questions', qid, {'status': 'filed'})
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['kit'])
        self.assertIn('question %s filed repo:kit from:kit route:kit' % qid, self.push.calls[0][1])
        self.pick_up('kit')
        self.later(3 * 3600)
        self.assertEqual(self.attempts('kit'), [1], 'picked up: the pushes end')

    def test_the_pms_own_filed_question_goes_on_pushing_the_pm_until_done(self):
        qid = self.ask()
        self.answer(qid)
        self.store.update('questions', qid, {'status': 'filed'})
        self.worker.poll()
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('pm'), [1, 2])
        self.store.update('questions', qid, {'status': 'done'})
        self.later(3 * 3600)
        self.assertEqual(self.attempts('pm'), [1, 2])

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
            self.worker.poll()
            # red proof: PushWorker._send leaving the unlisted items due sends them in the next
            # round as a second message
            self.assertEqual(len(self.push.calls), 1, 'the items past MAX_ITEMS went out at once')
            self.later(push.SCHEDULE_S[0])
        self.assertTrue(self.push.calls[0][1].endswith('+1 more: hub pending --for pm'))
        self.assertEqual(len(self.push.calls), 2, 'one message per recipient per slot')
        self.assertEqual(self.attempts('pm'), [1, 1, 2, 2])


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

    def test_a_running_worker_saves_each_round_so_an_unclean_exit_loses_nothing(self):
        # red proof: PushWorker._run saving only in its `finally` leaves no outbox here, so the
        # restarted worker owes nothing (logoff or a closed console never reaches that finally)
        self.worker.start()
        self.addCleanup(self.worker.stop, push.STOP_S)
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        path = os.path.join(self.folder, push.OUTBOX)

        def owed():
            try:
                with open(path, encoding='utf-8') as f:
                    entry = json.load(f).get('question:%s' % qid, {})
            except (OSError, ValueError):
                return None
            return {r: st['attempt'] for r, st in entry.get('recipients', {}).items()}
        deadline = time.monotonic() + 10
        while owed() != {'kit': 1, 'pm': 1} and time.monotonic() < deadline:
            threading.Event().wait(0.05)
        self.assertEqual(owed(), {'kit': 1, 'pm': 1}, 'the running worker did not save its round')
        self.worker._save = lambda: None        # the process dies: no save on the way out
        self.assertTrue(self.worker.stop(push.STOP_S))
        self.assertEqual(len(self.push.calls), 2)
        self.worker = self.make()
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 2, 'the restart sent again at once')
        self.later(push.SCHEDULE_S[0])
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1, 2], [1, 2]))

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
        qid = call('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS, 'from': 'kit',
                                                 'class': 'go'})['id']
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

    def test_a_story_or_a_note_pushes_no_free_text(self):
        self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': PLANTED, 'story': NOTE})
        self.ok('POST', '/to_pm', OWNER, {'text': PLANTED})
        self.ok('POST', '/to_pm/send', OWNER)
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 1)
        self.assertNotIn('PUSHPROOF', self.push.calls[0][1])

    def test_header_fields_are_cut_to_a_safe_set(self):
        self.assertEqual(push.field('a b<c>\n' + 'x' * 80), ('a_b_c__' + 'x' * 80)[:64])
        self.assertEqual(push.field(None), '-')


class StoryAndNoteTest(PushCase):
    def story(self):
        return self.ok('POST', '/stories', OWNER, {'repo': 'kit', 'title': 't', 'story': 's'})['id']

    def sent_note(self):
        pid = self.ok('POST', '/to_pm', OWNER, {'text': 'one'})['id']
        self.ok('POST', '/to_pm/send', OWNER)
        return pid

    def test_a_submitted_story_pushes_to_the_pm_once_until_its_status_changes(self):
        sid = self.story()
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['pm'])
        self.assertIn('[hub push] story %s submitted repo:kit from:- route:pm attempt:1' % sid,
                      self.push.to('pm')[0])
        self.assertIn('hub stories set %s' % sid, self.push.to('pm')[0])
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 1, 'pushed again with nothing new')
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('pm'), [1, 2], 'not picked up: tried again on the schedule')
        self.ok('POST', '/stories/%s' % sid, AGENT, {'status': 'review', 'if_version': 1})
        self.later(push.SCHEDULE_S[1])
        self.assertEqual(self.attempts('pm'), [1, 2], 'its status changed: acknowledged')
        self.assertEqual(self.worker.status()['undelivered'], 0)

    def test_a_sent_note_pushes_to_the_pm_once_until_it_is_replied_to(self):
        self.ok('POST', '/to_pm', OWNER, {'text': 'queued only'})
        self.worker.poll()
        self.assertEqual(self.push.calls, [], 'a queued note is not sent yet')
        self.ok('POST', '/to_pm/send', OWNER)
        self.worker.poll()
        self.assertEqual([r for r, _ in self.push.calls], ['pm'])
        self.assertIn('[hub push] to_pm P-001 sent repo:- from:- route:pm attempt:1',
                      self.push.to('pm')[0])
        self.assertIn('hub to-pm reply P-001', self.push.to('pm')[0])
        self.worker.poll()
        self.assertEqual(len(self.push.calls), 1, 'pushed again with nothing new')
        self.ok('POST', '/to_pm/P-001/reply', AGENT, {'reply': 'r'})
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(len(self.push.calls), 1, 'replied: acknowledged')

    def test_the_outbox_keeps_a_story_push_across_a_restart(self):
        self.push.states['pm'] = push.NO_SESSION
        self.story()
        self.worker.poll()
        self.push.states['pm'] = push.WRITTEN
        self.worker = self.make()
        self.later(push.SCHEDULE_S[0])
        self.assertEqual(self.attempts('pm'), [1, 2])

    def test_a_written_push_to_a_session_is_told_to_the_api(self):
        told = []
        self.api.pushed = told.append
        self.push.states['zeta'] = push.NOT_WRITTEN
        self.answer(self.ask(**{'from': 'kit'}))
        self.answer(self.ask(**{'from': 'zeta'}))
        self.worker.poll()
        self.assertEqual(told, ['kit'], 'the PM and a try not written are not told')


class WatcherTest(PushCase):
    def setUp(self):
        super().setUp()
        self.beat = [None]
        self.worker = push.PushWorker(self.store, self.api, self.push, self.folder,
                                      clock=lambda: self.t, watcher=self.read)
        self.worker.begin()

    def read(self):
        b = self.beat[0]
        return None if b is None else {'pid': 7, 'beat': b, 'stream': False, 'started': None}

    def watch(self, beat_age=None, after=0):
        self.t += after
        self.beat[0] = None if beat_age is None else self.t - beat_age
        self.worker.watch()

    def downs(self):
        return [t for t in self.push.to('pm') if t.startswith('[hub push] watcher down')]

    def test_a_watcher_never_seen_fresh_pushes_nothing(self):
        self.watch(None)
        self.watch(push.WATCHER_STALE_S + 1, after=60)
        self.assertEqual(self.push.calls, [])
        self.assertEqual(self.worker.watcher_status(),
                         {'state': 'down', 'age': push.WATCHER_STALE_S + 1})

    def test_a_stale_heartbeat_pushes_watcher_down_once_and_again_only_after_fresh(self):
        self.watch(10)
        self.assertEqual(self.worker.watcher_status(), {'state': 'up', 'age': 10})
        self.assertEqual(self.push.calls, [])
        self.watch(push.WATCHER_STALE_S + 5, after=60)
        self.assertEqual(len(self.downs()), 1)
        self.assertEqual(self.downs()[0].splitlines()[0],
                         '[hub push] watcher down age:%d route:pm attempt:1' % (push.WATCHER_STALE_S + 5))
        self.assertEqual([r for r, _ in self.push.calls], ['pm'], 'the PM alone')
        for _ in range(3):
            self.watch(push.WATCHER_STALE_S + 5, after=push.SCHEDULE_S[-1])
        self.assertEqual(len(self.downs()), 1, 'written once: not sent again while still down')
        self.watch(5, after=60)
        self.watch(push.WATCHER_STALE_S + 1, after=60)
        self.assertEqual(len(self.downs()), 2, 'fresh again, then stale: a second push')

    def test_a_covering_session_watch_job_counts_as_up(self):
        block = [None]
        self.worker.jobs = lambda: {'session-watch': block[0]} if block[0] else {}
        self.watch(10)
        block[0] = {'failures': 0, 'ok_at': self.t + 50}
        self.watch(push.WATCHER_STALE_S + 5, after=60)      # pm_watch stale, the job covers
        self.assertEqual(self.downs(), [])
        self.assertEqual(self.worker.watcher_status()['state'], 'up')
        self.watch(None, after=60)                          # no beat at all: still covered
        self.assertEqual(self.downs(), [])
        self.assertEqual(self.worker.watcher_status(), {'state': 'up', 'age': None})
        block[0] = {'failures': 3, 'ok_at': self.t}         # the job fails: nobody covers
        self.watch(None, after=60)
        self.assertEqual(len(self.downs()), 1)
        self.assertEqual(self.worker.watcher_status(), {'state': 'none', 'age': None})

    def test_a_skipping_or_stale_job_does_not_count(self):
        self.worker.jobs = lambda: {'session-watch': {'failures': 0, 'ok_at': None}}
        self.watch(10)
        self.watch(push.WATCHER_STALE_S + 5, after=60)
        self.assertEqual(len(self.downs()), 1, 'a job that only skipped covers nothing')

    def test_the_default_job_reader_is_the_api_s_health_jobs(self):
        self.api.jobs_status = lambda: {'session-watch': {'failures': 0, 'ok_at': self.t}}
        self.watch(10)
        self.watch(None, after=60)
        self.assertEqual(self.downs(), [])

    def test_a_job_reader_that_raises_covers_nothing(self):
        def broken():
            raise RuntimeError('boom')
        self.worker.jobs = broken
        self.watch(10)
        self.watch(None, after=60)
        self.assertEqual(len(self.downs()), 1)

    def test_a_heartbeat_that_goes_missing_is_down(self):
        self.watch(10)
        self.watch(None, after=60)
        self.assertEqual(self.worker.watcher_status(), {'state': 'none', 'age': None})
        self.assertEqual(self.downs()[0].splitlines()[0], '[hub push] watcher down age:none route:pm attempt:1')

    def test_a_push_not_written_is_tried_on_the_schedule_until_fresh(self):
        self.push.states['pm'] = push.NO_SESSION
        self.watch(10)
        self.watch(push.WATCHER_STALE_S + 1, after=60)
        self.watch(push.WATCHER_STALE_S + 1, after=1)
        self.assertEqual(len(self.downs()), 1, 'not due again at once')
        self.assertEqual(self.worker.status()['last'], push.NO_SESSION)
        self.watch(push.WATCHER_STALE_S + 1, after=push.SCHEDULE_S[0])
        self.assertEqual(len(self.downs()), 2)
        self.assertEqual([(u['kind'], u['id'], u['reason']) for u in self.undelivered()],
                         [('watcher', 'down', push.NO_SESSION)] * 2)
        self.watch(5, after=push.SCHEDULE_S[1])
        self.watch(5, after=push.SCHEDULE_S[2])
        self.assertEqual(len(self.downs()), 2, 'fresh again: acknowledged')

    def test_a_reader_that_raises_is_none(self):
        self.watch(10)

        def broken():
            raise OSError('locked')
        self.worker.watcher = broken
        self.t += 60
        self.worker.watch()
        self.assertEqual(self.worker.watcher_status(), {'state': 'none', 'age': None})
        self.assertEqual(len(self.downs()), 1)

    def test_poll_never_reads_the_heartbeat(self):
        self.worker.watcher = lambda: self.fail('poll read the heartbeat')
        self.worker.poll()

    def test_the_thread_reads_the_heartbeat(self):
        read = threading.Event()

        def reader():
            read.set()
            return None
        self.worker = push.PushWorker(self.store, self.api, self.push, self.folder, watcher=reader)
        self.worker.start()
        self.addCleanup(self.worker.stop, push.STOP_S)
        self.assertTrue(read.wait(10), 'the running worker never read the heartbeat')

    def test_the_default_reader_reads_pm_watchs_heartbeat_file(self):
        state = os.path.join(self.root, 'pmw', 'state.json')
        os.mkdir(os.path.dirname(state))
        with open(state + '.beat', 'w', encoding='utf-8') as f:
            json.dump({'pid': 3, 'beat': 123.0, 'stream': False, 'started': None}, f)
        with mock.patch.object(push.pmwatch, 'load_config', lambda: {'state_file': state}):
            got = REAL_HEARTBEAT()
        self.assertEqual(got['beat'], 123.0)


class ThreadTest(unittest.TestCase):
    def setUp(self):
        no_watcher(self)
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
                                   {'title': 't', 'options': OPTIONS, 'class': 'go'})
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
        # red proof: Hub.close joining for min(timeout, STOP_S) cuts a deliver in flight short
        self.assertEqual(order, [('push.stop', 0), ('store.close',), ('push.stop', push.STOP_S)])
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


class RecordViews(object):
    """The record view and its write for a question bound to record EX-1 of repo kit: the
    record's state line is `before` until an answer is filed, then `after` (what plan_answer
    leaves). `fail`: the HubError the write raises; `during`: run once inside the next write,
    before it is done (the push worker polling while the answer is being filed)."""

    def __init__(self, before='open', after=None):
        self.before, self.after = before, before if after is None else after
        self.filed, self.fail, self.during = False, None, None

    def record(self, repo, id):
        if repo.lower() != 'kit' or id != 'EX-1':
            raise HubError('not_found', 'no such record', 'id')
        state = self.after if self.filed else self.before
        return {'repo': 'kit', 'id': id, 'where': 'open',
                'fields': [['id', id], ['state', state]]}

    def record_answer(self, repo, id, qid, at, title, label, note, via, other, waiting):
        during, self.during = self.during, None
        if during is not None:
            during()
        if self.fail is not None:
            raise self.fail
        self.filed = True
        return {'lines': []}


class ResolvingPush(FakePush):
    """FakePush with resolve(key): the live session of `key`, None when none (`live`)."""

    def __init__(self, live=()):
        FakePush.__init__(self)
        self.live, self.resolved = set(live), []

    def resolve(self, key):
        self.resolved.append(key)
        return ('sid-' + key) if key in self.live else None


class FiledPushCase(PushCase):
    """A question bound to a record, answered and filed through the Api: who hears it depends on
    what the filing did to the record (docs/action-hub.md "Push notifications", "Filed answers")."""

    def bind(self, before, after=None, live=None):
        self.views = RecordViews(before, after)
        self.api = api.Api(self.store, views=self.views, clock=self.ticks,
                           session_keys=lambda: {'kit', 'zeta'})
        if live is not None:
            self.push = ResolvingPush(live)
        self.worker = self.make()

    def ask_bound(self, asker='kit'):
        return self.ask(**{'from': asker, 'record': {'repo': 'kit', 'id': 'EX-1'}})

    def promote(self, qid):
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'standing': True})

    def log(self, qid):
        version = self.store.get('questions', qid)['version']
        self.ok('POST', '/questions/%s/log' % qid, AGENT, {'if_version': version})


class FiledRecipientsTest(FiledPushCase):

    def test_an_answer_filed_into_a_pulled_record_goes_to_its_asker_alone(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assertEqual(self.store.get('questions', qid)['filed_as'], 'pulled')
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        self.assertIn('filed into record kit:EX-1, which is being worked on', self.push.to('kit')[0])
        self.assertIn('hub pending --for kit', self.push.to('kit')[0])
        self.later(130)
        self.assertEqual(self.attempts('kit'), [1, 2], 'the asker is told until it picks it up')
        self.pick_up('kit')
        self.later(400)
        self.assertEqual(self.attempts('kit'), [1, 2])
        self.assertEqual(self.push.to(push.PM_KEY), [])

    def test_a_pulled_answer_the_pm_asked_tells_the_pm_once(self):
        self.bind('pulled')
        qid = self.ask_bound(asker='pm')
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.assertEqual(self.attempts(push.PM_KEY), [1])
        self.assertIn('its worker reads it', self.push.to(push.PM_KEY)[0])
        self.assertIn('no ack needed', self.push.to(push.PM_KEY)[0])

    def test_a_held_answer_goes_to_its_live_asker_saying_the_record_stays_as_it_was(self):
        # red proof: before this change a held filing pushed to nobody
        self.bind('blocked', live={'zeta'})
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.assertEqual(self.store.get('questions', qid)['filed_as'], 'held')
        self.assertEqual([r for r, _t in self.push.calls], ['zeta'],
                         'one push, to the asker: no separate answered push besides it')
        text = self.push.to('zeta')[0]
        self.assertIn('[hub push] question %s filed' % qid, text)
        self.assertIn('does not free record kit:EX-1', text)
        self.assertIn('stays as it was', text)
        self.assertIn('hub pending --for zeta', text)
        self.later(130)
        self.assertEqual(self.attempts('zeta'), [1, 2], 'the asker is told until it picks it up')
        self.pick_up('zeta')
        self.later(400)
        self.assertEqual(self.attempts('zeta'), [1, 2])
        self.assertEqual(self.push.to(push.PM_KEY), [])

    def test_a_held_answer_whose_asker_is_gone_goes_to_the_records_coordinator_once(self):
        self.bind('blocked', live={'kit'})
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.later(400)
        self.assertEqual(self.store.get('questions', qid)['filed_as'], 'held')
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        text = self.push.to('kit')[0]
        self.assertIn('does not free record kit:EX-1', text)
        self.assertIn('asked by zeta', text)
        self.assertIn('hub view %s' % qid, text)

    def test_a_held_answer_with_nobody_live_stays_owed_to_its_asker(self):
        for push_obj in ('no resolve', 'none live'):
            with self.subTest(push=push_obj):
                self.bind('blocked', live=None if push_obj == 'no resolve' else ())
                qid = self.ask_bound()
                self.answer(qid)
                self.worker.poll()
                self.assertEqual(self.store.get('questions', qid)['filed_as'], 'held')
                self.assertEqual([r for r, _t in self.push.calls], ['kit'],
                                 'tried at its asker, which reads it at its next start anyway')
                self.assertIn(qid, [p['id'] for p in self.ok('GET', '/pending', AGENT,
                                                              query={'for': 'kit'})['items']])

    def test_a_held_answer_the_pm_asked_tells_the_pm_once(self):
        self.bind('blocked', live=())
        qid = self.ask_bound(asker='pm')
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.assertEqual(self.attempts(push.PM_KEY), [1])
        self.assertIn('does not free record kit:EX-1', self.push.to(push.PM_KEY)[0])

    def test_a_freed_record_with_no_live_coordinator_pings_the_pm_once(self):
        self.bind('parked', 'open', live=())
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.later(400)
        self.assertEqual(self.store.get('questions', qid)['filed_as'], 'freed')
        self.assertEqual([r for r, _t in self.push.calls], [push.PM_KEY])
        text = self.push.to(push.PM_KEY)[0]
        self.assertIn('frees it, and no coordinator of repo kit is running', text)
        self.assertIn('no ack needed', text)
        self.assertEqual(self.push.resolved, ['kit'], 'the asker and the repo are one key here')

    def test_a_freed_answer_goes_to_its_live_asker(self):
        # red proof: before this change a freed filing with a live session pushed to nobody
        for live in ({'zeta'}, {'zeta', 'kit'}):    # the asker is preferred to the coordinator
            with self.subTest(live=live):
                self.bind('blocked', 'open', live=live)
                qid = self.ask_bound(asker='zeta')
                self.answer(qid)
                self.worker.poll()
                self.assertEqual(self.store.get('questions', qid)['filed_as'], 'freed')
                self.assertEqual([r for r, _t in self.push.calls], ['zeta'])
                text = self.push.to('zeta')[0]
                self.assertIn('frees record kit:EX-1', text)
                self.assertIn('hub pending --for zeta', text)
                self.later(130)
                self.assertEqual(self.attempts('zeta'), [1, 2], 'until it picks it up')
                self.pick_up('zeta')
                self.later(400)
                self.assertEqual(self.attempts('zeta'), [1, 2])
                self.assertEqual(self.push.to(push.PM_KEY), [])

    def test_a_freed_answer_whose_asker_is_gone_goes_to_the_records_coordinator_once(self):
        self.bind('blocked', 'open', live={'kit'})
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.later(400)
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        text = self.push.to('kit')[0]
        self.assertIn('frees record kit:EX-1', text)
        self.assertIn('asked by zeta', text)
        self.assertEqual(self.push.to(push.PM_KEY), [])

    def test_the_records_coordinator_is_found_through_the_repo_key_map(self):
        self.bind('blocked', 'open', live={'kitcoord'})
        self.api.repo_name = lambda repo: 'kitcoord' if repo == 'kit' else None
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], ['kitcoord'])

    def test_a_freed_record_without_resolve_pings_the_pm(self):
        self.bind('blocked', 'open')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], [push.PM_KEY])

    def test_nothing_fires_while_an_answer_is_being_filed(self):
        self.bind('pulled')
        qid = self.ask_bound()
        seen = []

        def poll_mid_filing():
            self.worker.poll()
            seen.append((self.store.get('questions', qid)['status'], list(self.push.calls)))
        self.views.during = poll_mid_filing
        self.answer(qid)
        self.assertEqual(seen, [('answered', [])])
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])

    def test_an_answer_whose_filing_failed_goes_to_its_asker_and_the_pm(self):
        self.bind('pulled')
        self.views.fail = HubError('store', 'disk full', 'views')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assertIsNotNone(self.store.get('questions', qid)['filing'])
        self.assertEqual(sorted(r for r, _t in self.push.calls), sorted(['kit', push.PM_KEY]))

    def test_a_re_answer_seen_while_it_is_filed_reaches_an_asker_that_held_the_first(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        self.later(130)
        self.assertEqual(self.attempts('kit'), [1])
        self.views.during = self.worker.poll
        self.answer(qid, choice=1)
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 1], 'the re-answer is sent to the asker')


class StandingPushTest(FiledPushCase):

    def test_a_standing_answer_goes_to_the_pm_until_it_is_logged(self):
        self.bind('blocked')
        qid = self.ask_bound()
        self.ok('POST', '/questions/%s/answer' % qid, OWNER,
                {'choice': 0, 'note': NOTE, 'standing': True})
        self.worker.poll()
        self.assertEqual(sorted(r for r, _t in self.push.calls), sorted(['kit', push.PM_KEY]),
                         'held: its asker hears it, and the PM logs the standing rule')
        self.assertIn('a standing rule', self.push.to(push.PM_KEY)[0])
        self.assertIn('hub questions log %s' % qid, self.push.to(push.PM_KEY)[0])
        self.pick_up('pm')
        self.later(130)
        self.assertEqual(self.attempts(push.PM_KEY), [1, 2], 'picking it up is not logging it')
        self.log(qid)
        self.later(400)
        self.assertEqual(self.attempts(push.PM_KEY), [1, 2])

    def test_a_promote_adds_the_pm_to_the_push_its_asker_is_owed(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        self.promote(qid)
        self.later(130)
        self.assertEqual(self.attempts('kit'), [1, 2], 'the asker is still owed the answer')
        self.assertEqual(self.attempts(push.PM_KEY), [1])
        self.assertIn('a standing rule', self.push.to(push.PM_KEY)[0])
        self.pick_up('kit')
        self.later(400)
        self.assertEqual(self.attempts('kit'), [1, 2])
        self.assertEqual(self.attempts(push.PM_KEY), [1, 2], 'the PM until it logs it')

    def test_a_promote_of_an_answer_no_longer_owed_goes_to_the_pm_alone(self):
        self.bind('blocked')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        self.pick_up('kit')
        self.later(130)
        self.assertEqual(self.attempts('kit'), [1], 'picked up: nothing more owed')
        self.promote(qid)
        self.worker.poll()
        self.assertEqual([r for r, _t in self.push.calls], ['kit', push.PM_KEY])
        self.assertIn('standing rule', self.push.to(push.PM_KEY)[0])


class AutoDoneTest(FiledPushCase):
    """A filed answer is marked done by the hub once its asker has it, pinned to the version the
    push fired at (kitlib.hubsite.push "Done by the hub", docs/action-hub.md "Filing the
    answer")."""

    def q(self, qid):
        return self.store.get('questions', qid)

    def assert_hub_done(self, qid):
        d = self.q(qid)
        self.assertEqual((d['status'], d.get('done_by'), d.get('done_why'), d.get('via')),
                         ('done', 'hub', 'filed into kit:EX-1', 'hub-push'), d)
        self.assertTrue(d.get('done_at'))
        self.assertIsNotNone(d.get('filed_as'), 'the filing is kept')

    def assert_not_done(self, qid, status='filed'):
        d = self.q(qid)
        self.assertEqual((d['status'], d.get('done_by')), (status, None), d)

    def pending_for(self, key):
        return [p['id'] for p in self.ok('GET', '/pending', AGENT, query={'for': key})['items']]

    def test_a_pulled_answer_is_marked_done_once_its_asker_picks_it_up(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.assert_not_done(qid)       # never in the filing's own write, nor at the push
        self.later(130)
        self.assertEqual(self.attempts('kit'), [1, 2], 'the filed push still reaches the asker')
        self.assert_not_done(qid)
        self.pick_up('kit')
        self.later(400)
        self.assert_hub_done(qid)
        self.assertEqual(self.attempts('kit'), [1, 2])
        self.assertEqual(self.pending_for('kit'), [])
        self.later(3600)
        self.assertEqual(self.attempts('kit'), [1, 2], 'the done fires no push')
        self.assertEqual(self.push.to(push.PM_KEY), [])

    def test_an_answer_picked_up_before_the_push_fired_is_marked_done_at_once(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.pick_up('kit')
        self.worker.poll()
        self.assertEqual(self.push.calls, [])
        self.assert_hub_done(qid)

    def test_one_the_pm_asked_is_done_at_its_written_try_and_not_before(self):
        self.bind('pulled')
        self.push.states[push.PM_KEY] = push.NOT_WRITTEN
        qid = self.ask_bound(asker='pm')
        self.answer(qid)
        self.worker.poll()
        self.assert_not_done(qid)
        self.push.states[push.PM_KEY] = push.WRITTEN
        self.later(130)
        self.assertEqual(self.attempts(push.PM_KEY), [1, 2])
        self.assert_hub_done(qid)

    def test_a_freed_record_still_pings_the_pm_and_stays_its_askers_until_it_reads_it(self):
        self.bind('parked', 'open', live=())
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.later(400)
        self.assertEqual([r for r, _t in self.push.calls], [push.PM_KEY],
                         'the freed ping still fires; the asker is not sent it')
        self.assertIn('no coordinator of repo kit is running', self.push.to(push.PM_KEY)[0])
        self.assert_not_done(qid)
        self.assertEqual(self.pending_for('kit'), [qid], 'pending for its asker (the wake)')
        self.later(3600)
        self.assert_hub_done(qid)

    def test_an_asker_never_sent_it_counts_as_no_undelivered_try_and_no_give_up(self):
        self.bind('parked', 'open', live=())
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.later(push.GIVE_UP_S + 10)
        self.assertEqual(self.undelivered(), [])
        self.assert_not_done(qid)
        self.assertEqual(self.pending_for('kit'), [qid], 'its asker still reads it at its start')

    def test_a_held_answer_told_to_the_records_coordinator_waits_for_its_asker(self):
        self.bind('blocked', live={'kit'})
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.later(130)
        self.assertEqual([r for r, _t in self.push.calls], ['kit'])
        self.assert_not_done(qid)
        self.pick_up('kit')
        self.later(400)
        self.assert_not_done(qid)       # the coordinator's read is not the asker's
        self.pick_up('zeta')
        self.later(900)
        self.assert_hub_done(qid)

    def test_a_held_answer_to_its_live_asker_is_done_when_it_picks_it_up(self):
        self.bind('blocked', live={'zeta'})
        qid = self.ask_bound(asker='zeta')
        self.answer(qid)
        self.worker.poll()
        self.pick_up('zeta')
        self.later(130)
        self.assert_hub_done(qid)

    def test_a_standing_a_failed_an_unbound_and_a_run_line_answer_are_never_marked_done(self):
        for case in ('standing', 'failed', 'class', 'unbound pm', 'run line'):
            with self.subTest(case=case):
                self.bind('pulled')
                if case == 'failed':
                    self.views.fail = HubError('store', 'disk full', 'views')
                if case == 'class':
                    qid = self.ask(**{'from': 'kit'})
                elif case == 'unbound pm':
                    qid = self.ask()
                elif case == 'run line':
                    qid = self.ask(**{'from': 'kit', 'record': {'repo': 'kit', 'id': 'EX-1'},
                                      'options': [{'label': 'run D-001 cap=2',
                                                   'description': 'go'},
                                                  {'label': 'hold', 'description': 'no'}]})
                else:
                    qid = self.ask_bound()
                body = {'choice': 0, 'note': NOTE}
                if case == 'standing':
                    body['standing'] = True
                self.ok('POST', '/questions/%s/answer' % qid, OWNER, body)
                self.worker.poll()
                self.pick_up('kit')
                self.pick_up('pm')
                self.later(130)
                self.later(400)
                d = self.q(qid)
                self.assertNotEqual(d['status'], 'done', d)
                self.assertIsNone(d.get('done_by'))

    def test_a_re_answer_reopens_one_the_hub_marked_done_and_reaches_its_asker_again(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        self.later(130)
        self.assert_hub_done(qid)
        self.answer(qid, choice=1)      # "Change answer" on the page
        d = self.q(qid)
        self.assertEqual((d['status'], d['answer']['choice'], d.get('done_by'),
                          d.get('done_why'), d.get('done_at')),
                         ('filed', 1, None, None, None))
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 1],
                         'the new answer is pushed: the old pick-up does not count for it')
        self.assert_not_done(qid)
        self.pick_up('kit')
        self.later(400)
        self.assert_hub_done(qid)
        self.assertEqual(self.q(qid)['answer']['choice'], 1)

    def test_a_promote_of_one_the_hub_marked_done_files_it_again_for_the_pm(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        self.later(130)
        self.assert_hub_done(qid)
        self.promote(qid)
        d = self.q(qid)
        self.assertEqual((d['status'], d.get('done_by'), d.get('done_why')), ('filed', None, None))
        self.assertIsInstance(d['standing'], dict)
        self.worker.poll()
        self.assertIn('standing rule', self.push.to(push.PM_KEY)[0])
        self.pick_up('pm')
        self.pick_up('kit')
        self.later(400)
        self.assert_not_done(qid)       # a standing rule is the PM's to log
        self.log(qid)
        self.assertEqual(self.q(qid)['status'], 'logged')

    def test_a_write_after_the_push_fired_wins_and_the_done_is_skipped(self):
        # mutation proof: a done written with no version pin overwrites this write's version
        logged = []
        self.bind('pulled')
        self.worker.log = logged.append
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.store.update('questions', qid, {'urgent': True})   # the answer is the same
        self.pick_up('kit')
        self.later(130)
        self.assert_not_done(qid)
        self.assertTrue(self.q(qid)['urgent'])
        self.assertEqual(logged, ['push: %s not marked done: conflict' % qid])
        self.assertEqual(self.attempts('kit'), [1], 'picked up: nothing more owed')

    def test_a_re_answer_landing_as_the_done_is_written_wins(self):
        self.bind('pulled')
        logged = []
        self.worker.log = logged.append
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.pick_up('kit')
        real = self.store.update
        raced = []

        def update(coll, doc_id, changes, **kw):
            if changes.get('done_by') == 'hub' and not raced:
                raced.append(True)
                self.answer(qid, choice=1)  # the owner's "Change answer" gets in first
            return real(coll, doc_id, changes, **kw)
        with mock.patch.object(self.store, 'update', update):
            self.later(130)
        self.assertEqual(raced, [True], 'the done write was tried')
        d = self.q(qid)
        self.assertEqual((d['status'], d['answer']['choice'], d.get('done_by')), ('filed', 1, None))
        self.assertEqual(logged, ['push: %s not marked done: conflict' % qid])
        self.worker.poll()
        self.assertEqual(self.attempts('kit'), [1, 1], 'the new answer reaches its asker')

    def test_the_done_survives_a_restart_of_the_worker(self):
        self.bind('parked', 'open', live=())
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        self.worker = self.make()
        self.pick_up('kit')
        self.later(130)
        self.assert_hub_done(qid)

    def test_the_askers_own_done_of_one_the_hub_marked_done_is_no_conflict(self):
        self.bind('pulled')
        qid = self.ask_bound()
        self.answer(qid)
        self.worker.poll()
        stale = self.q(qid)['version']      # what the asker read with the answer
        self.pick_up('kit')
        self.later(130)
        self.assert_hub_done(qid)
        before = self.q(qid)
        got = self.ok('POST', '/questions/%s/done' % qid, AGENT,
                      {'why': 'read it', 'from': 'kit', 'if_version': stale})
        self.assertEqual((got['status'], got['done_by'], got['version']),
                         ('done', 'hub', before['version']))
        self.assertEqual(self.q(qid), before, 'nothing written')
        status, obj = self.api.call('POST', BASE + '/questions/%s/done' % qid, AGENT,
                                    {'why': 'w', 'from': 'other', 'if_version': stale})
        self.assertEqual((status, obj['error']['code']), (403, 'forbidden'), obj)


if __name__ == '__main__':
    unittest.main()
