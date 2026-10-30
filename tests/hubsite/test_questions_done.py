"""kitlib.hubsite.api questions_done and `hub.py questions done`: the PM marks an answered,
unbound question handled with no ruling (a one-off go the ledger keeps out). The question is
then `done`: no longer pending for any reader, its push ends, and it takes no new answer.
"""
import json
import shutil
import tempfile
import unittest

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import Store
from tests.hubsite import test_hub_cli as cli
from tests.hubsite import test_page as page
from tests.hubsite import test_push as pushfix

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
OPTIONS = [{'label': 'go', 'description': 'land it'}, {'label': 'wait', 'description': 'hold'}]
# a ledger id, built so no file holds a ruling id (tests/scrub)
RULING = 'R-' + '0007'


class DoneCase(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix='kit-hubdone-')
        self.addCleanup(shutil.rmtree, root, True)
        self.store = Store.open(root, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, clock=CLOCK, session_keys=lambda: {'kit'})

    def call(self, method, path, role, body=None, query=None):
        status, obj = self.api.call(method, BASE + path, role, body, query)
        json.dumps(obj)
        return status, obj

    def ok(self, method, path, role, body=None, query=None):
        status, obj = self.call(method, path, role, body, query)
        self.assertEqual((status, obj.get('error')), (200, None), obj)
        return obj

    def refused(self, code, field, qid, body, role=AGENT):
        before = self.store.snapshot()
        status, obj = self.call('POST', '/questions/%s/done' % qid, role, body)
        self.assertNotEqual(status, 200, obj)
        self.assertEqual((obj['error']['code'], obj['error']['field']), (code, field), obj)
        self.assertEqual(self.store.snapshot(), before, 'a refused call wrote')
        return obj

    def answered(self, **kw):
        body = {'title': 'land kit/x?', 'repo': 'kit', 'options': OPTIONS}
        if 'record' not in kw:
            body['class'] = 'go'            # a question needs a record or a class
        body.update(kw)
        qid = self.ok('POST', '/questions', AGENT, body)['id']
        self.ok('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 0})
        return qid

    def version(self, qid):
        return self.store.get('questions', qid)['version']

    def done(self, qid, why='a one-off landing go'):
        return self.ok('POST', '/questions/%s/done' % qid, AGENT,
                       {'why': why, 'if_version': self.version(qid)})

    def pending_ids(self, **query):
        return [p['id'] for p in self.ok('GET', '/pending', AGENT, query=query)['items']]


class DoneTest(DoneCase):
    def test_an_answered_question_is_marked_done_with_its_why_and_time(self):
        qid = self.answered()
        before = self.version(qid)
        self.done(qid, '  a one-off landing go  ')
        d = self.store.get('questions', qid)
        self.assertEqual((d['status'], d['done_why'], d['done_at']),
                         ('done', 'a one-off landing go', NOW))
        self.assertEqual(d['version'], before + 1)
        self.assertNotIn('ruling', d)
        self.assertEqual(d['answer']['choice'], 0, 'the answer is kept')

    def test_a_done_question_leaves_every_pending_list_and_others_stay(self):
        qid, other = self.answered(), self.answered(**{'from': 'kit'})
        self.assertEqual(sorted(self.pending_ids()), sorted([qid, other]))
        self.done(qid)
        self.assertEqual(self.pending_ids(), [other])
        self.assertEqual(self.pending_ids(**{'for': 'pm'}), [other])
        self.assertEqual([p.id for p in self.store.pending()], [other])

    def test_done_is_a_status_the_listing_takes(self):
        qid, other = self.answered(), self.answered()
        self.done(qid)
        got = self.ok('GET', '/questions', AGENT, query={'status': 'done'})['items']
        self.assertEqual([d['id'] for d in got], [qid])
        got = self.ok('GET', '/questions', AGENT, query={'status': 'answered'})['items']
        self.assertEqual([d['id'] for d in got], [other])

    def test_a_done_question_takes_no_new_answer(self):
        qid = self.answered()
        self.done(qid)
        status, obj = self.call('POST', '/questions/%s/answer' % qid, OWNER, {'choice': 1})
        self.assertEqual((status, obj['error']['field']), (400, 'status'), obj)
        self.assertEqual(self.store.get('questions', qid)['status'], 'done')


    def test_the_asker_marks_its_own_question_done(self):
        qid = self.answered(**{'from': 'kit'})
        self.ok('POST', '/questions/%s/done' % qid, AGENT,
                {'why': 'w', 'from': 'kit', 'if_version': self.version(qid)})
        self.assertEqual(self.store.get('questions', qid)['status'], 'done')

    def test_the_pm_marks_any_question_done_named_or_not(self):
        first, second = self.answered(**{'from': 'kit'}), self.answered(**{'from': 'kit'})
        self.ok('POST', '/questions/%s/done' % first, AGENT,
                {'why': 'w', 'from': 'pm', 'if_version': self.version(first)})
        self.done(second)
        for qid in (first, second):
            self.assertEqual(self.store.get('questions', qid)['status'], 'done')


class RefusedTest(DoneCase):
    def test_an_open_question_is_refused(self):
        qid = self.ok('POST', '/questions', AGENT, {'title': 't', 'options': OPTIONS,
                                                    'class': 'go'})['id']
        self.refused('invalid', 'status', qid, {'why': 'w', 'if_version': self.version(qid)})

    def test_a_logged_question_is_refused(self):
        qid = self.answered()
        self.ok('POST', '/questions/%s/log' % qid, AGENT,
                {'if_version': self.version(qid)})
        self.refused('invalid', 'status', qid, {'why': 'w', 'if_version': self.version(qid)})

    def test_a_question_already_done_is_refused(self):
        qid = self.answered()
        self.done(qid)
        obj = self.refused('invalid', 'status', qid,
                           {'why': 'again', 'if_version': self.version(qid)})
        self.assertIn('is done', obj['error']['message'])

    def test_a_version_the_question_no_longer_has_is_a_conflict(self):
        qid = self.answered()
        self.refused('conflict', 'if_version', qid,
                     {'why': 'w', 'if_version': self.version(qid) - 1})

    def test_the_version_and_the_why_are_required_and_the_why_not_blank(self):
        qid = self.answered()
        self.refused('invalid', 'if_version', qid, {'why': 'w'})
        self.refused('invalid', 'why', qid, {'if_version': self.version(qid)})
        self.refused('invalid', 'why', qid, {'why': '  ', 'if_version': self.version(qid)})

    def test_another_session_key_is_refused_and_the_question_is_unchanged(self):
        qid = self.answered(**{'from': 'kit'})
        before = self.store.get('questions', qid)
        obj = self.refused('forbidden', 'from', qid,
                           {'why': 'w', 'from': 'other', 'if_version': self.version(qid)})
        self.assertIn('only its asker or pm', obj['error']['message'])
        self.assertEqual(self.store.get('questions', qid), before)

    def test_unfiled_is_refused_on_a_question_bound_to_no_record(self):
        qid = self.answered()
        obj = self.refused('invalid', 'unfiled', qid,
                           {'why': 'w', 'unfiled': 'no record', 'if_version': self.version(qid)})
        self.assertIn('only an answer whose filing failed', obj['error']['message'])

    def test_the_owner_cannot_mark_one_done(self):
        qid = self.answered()
        status, obj = self.call('POST', '/questions/%s/done' % qid, OWNER,
                                {'why': 'w', 'if_version': self.version(qid)})
        self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                         (403, 'forbidden', 'role'), obj)
        self.assertEqual(self.store.get('questions', qid)['status'], 'answered')


class PushTest(pushfix.PushCase):
    def test_a_done_question_stops_both_pushes_and_fires_none(self):
        qid = self.ask(**{'from': 'kit'})
        self.answer(qid)
        self.worker.poll()
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1], [1]))
        version = self.store.get('questions', qid)['version']
        self.ok('POST', '/questions/%s/done' % qid, AGENT,
                {'why': 'a one-off archive go', 'if_version': version})
        self.later(3 * 3600)
        self.assertEqual((self.attempts('kit'), self.attempts('pm')), ([1], [1]))
        self.assertEqual(self.worker._outbox, {}, 'the done question is still owed a push')


class CliTest(cli.VerbCase):
    def test_questions_done_marks_an_answered_question_done(self):
        self.assertEqual(self.ask(), ['ASKED Q-1000'])
        self.assertEqual(self.ok('answer', 'Q-1000', '--choice', '0'), ['ANSWERED Q-1000 left'])
        self.assertIn('REFUSED conflict: ', self.refused(
            'questions', 'done', 'Q-1000', '--why', 'a landing go', '--if-version', '1'))
        self.assertEqual(self.ok('questions', 'done', 'Q-1000', '--why', 'a landing go',
                                 '--if-version', '2'), ['DONE Q-1000'])
        self.assertEqual(self.ok('questions', '--status', 'done'),
                         ['Q-1000 v3 done [kit] which way (done: a landing go)'])
        self.assertEqual(self.direct('/pending')['items'], [])
        self.assertIn('REFUSED invalid: ', self.refused(
            'questions', 'done', 'Q-1000', '--why', 'again', '--if-version', '3'))

    def test_the_why_and_the_version_are_required(self):
        for argv, missing in ((('questions', 'done', 'Q-1000', '--why', 'w'), '--if-version'),
                              (('questions', 'done', 'Q-1000', '--if-version', '1'), '--why'),
                              (('questions', 'done', '--why', 'w', '--if-version', '1'), 'id')):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*(argv + ('--port', self.port)))
                self.assertEqual((code, out), (2, ''), err)
                self.assertIn('USAGE: the following arguments are required: %s' % missing, err)


@unittest.skipUnless(page.NODE, "node is needed to run the page's logic")
class PageTest(unittest.TestCase):
    """kitlib/hubsite/web/index.html shows a done question as handled, with logged, labelled done."""

    def test_a_done_question_is_drawn_as_handled_and_labelled_done(self):
        ans = {"choice": 0, "label": "one", "note": "", "at": page.NOW}
        q = page.LISTS["questions"]["Q-1"]
        lists = {"stories": {"S-1": {"status": "questions", "title": "mine", "story": "x", "repo": "alpha",
                                     "version": 1}},
                 "questions": {"Q-1": dict(q, status="done", version=3, answer=ans, story="S-1", urgent=True,
                                           done_why="a one-off go", done_at=page.NOW),
                               "Q-2": dict(q, status="logged", version=3, answer=ans, ruling="D-7")}}
        got = page.dom("""
            await start();
            const counts = {};
            for (const n of ["open", "answered", "logged", "other"]) counts[n] = text($("#n-" + n));
            await click($("#tabs [data-f=logged]"));
            const card = $("#q-Q-1");
            const out = { counts, card: card ? text(card) : null, cls: card ? card.className : null,
                          ok: $$("#row-Q-1 .chip.ok").map(text) };
            await click(button(card, "story: "));
            out.link = $$("#story-S-1 button").map(text).filter(t => t.startsWith("Q-1"));
            return out;
        """, page.snapshot(collections=lists, views={"repos": [{"name": "alpha"}], "backlog": page.BACKLOG}))
        self.assertEqual(got["counts"], {"open": "0", "answered": "0", "logged": "2", "other": "0"})
        self.assertIn("done, no ruling needed", got["card"])
        self.assertNotIn("waiting for the PM", got["card"])
        self.assertNotIn("status: done", got["card"])        # not the unknown-status chip
        self.assertEqual(got["ok"], ["done"])                # the row's chip, as logged has
        self.assertEqual(got["cls"], "q done urgent")
        self.assertEqual(got["link"], ["Q-1 (done)"])

    def test_an_urgent_done_question_loses_the_urgent_border_as_a_logged_one_does(self):
        self.assertIn(".q.urgent:not(.logged):not(.done)", page.page())


if __name__ == '__main__':
    unittest.main()
