"""kitlib.hubsite.recordwrite: the owner's add, edit and delete from the record page, written
through the backlog library to a ref store on a temporary repository with a bare remote and an
empty home, each refusal put as the HubError the Api answers."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.hubsite import backlog, recordwrite
from kitlib.hubsite.store import HubError
from tests.backlog.helpers import kit_json_ref, rec, record_text
from tests.gitfixture import TempRepo, fixture_env, rmtree

SUMMARY = 'the hub page lists nothing when the store is empty'
# Built, so this file holds no ruling id or flag for tests/scrub to flag.
FIRST, ASKS = 'R-' + '9001', '--' + 'ruling'


class Case(unittest.TestCase):

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = mock.patch.dict(os.environ, fixture_env({'USERPROFILE': home, 'HOME': home}),
                              clear=True)
        env.start()
        self.addCleanup(env.stop)

    def named(self):
        kit_json_ref(self.repo)

    def seed(self, **records):
        """Publish records as they are, past every plan (a pulled one cannot be written by set)."""
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap, records, 'seed\n')

    def read(self, rid):
        return backlog.read_record('r', self.repo.root, rid)

    def refused(self, call, code):
        with self.assertRaises(HubError) as got:
            call()
        self.assertEqual(got.exception.code, code, got.exception.message)
        return got.exception

    def field(self, rid, key):
        return [v for k, v in self.read(rid)['fields'] if k == key]


class Add(Case):

    def test_add_writes_an_unpriced_record_marked_for_review(self):
        self.named()
        out = recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py'], 's')
        self.assertIn('ADDED FOO, found on no branch (or on a branch since landed)', out['lines'])
        self.assertEqual(self.field('FOO', 'priority'), ['-'])
        self.assertEqual(self.field('FOO', 'effort'), ['s'])
        self.assertEqual(len(self.field('FOO', 'review')), 1)
        self.assertRegex(self.field('FOO', 'review')[0], r'^owner-hub \d{4}-\d{2}-\d{2}$')
        self.assertEqual(self.field('FOO', 'summary'), [SUMMARY])     # the finding is in the body
        log = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertIn('Session: owner-hub', log)

    def test_a_store_refusal_is_invalid_with_the_stores_text(self):
        self.named()
        e = self.refused(lambda: recordwrite.add(self.repo.root, 'foo', SUMMARY, ['tools/x.py']),
                         'invalid')
        self.assertIn('cannot add', e.message)
        recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py'])
        e = self.refused(lambda: recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/y.py']),
                         'invalid')
        self.assertIn('FOO', e.message)

    def test_the_implicit_default_store_is_refused_before_any_write(self):
        before = self.repo.git('for-each-ref', smod.STORE_REF)
        e = self.refused(lambda: recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py']),
                         'store')
        self.assertIn('names no "backlog".store', e.message)
        self.assertEqual(self.repo.git('for-each-ref', smod.STORE_REF), before)


class Reread(Case):

    def test_a_reread_after_a_write_puts_it_in_the_held_view(self):
        self.named()
        views = backlog.Views([('r', self.repo.root)])
        views.get()
        recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py'])
        ids = lambda: [i['id'] for i in views.held()['repos'][0]['open']]
        self.assertEqual(ids(), [], 'held until something reads the repository again')
        views.reread('r', self.repo.root)
        self.assertEqual(ids(), ['FOO'])


class Edit(Case):

    def setUp(self):
        Case.setUp(self)
        self.named()
        recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py'])

    def test_an_edit_at_the_version_read_is_written_and_a_stale_one_is_a_conflict(self):
        version = self.read('FOO')['version']
        recordwrite.edit(self.repo.root, 'FOO', version, [('effort', ['m'])], body='New body.\n')
        got = self.read('FOO')
        self.assertEqual(self.field('FOO', 'effort'), ['m'])
        self.assertEqual(got['body'], 'New body.\n')
        e = self.refused(lambda: recordwrite.edit(self.repo.root, 'FOO', version,
                                                  [('effort', ['l'])]), 'conflict')
        self.assertEqual((e.message, e.field), (recordwrite.STALE, 'version'))
        self.assertEqual(self.field('FOO', 'effort'), ['m'])

    def test_a_field_the_store_writes_itself_is_invalid(self):
        version = self.read('FOO')['version']
        e = self.refused(lambda: recordwrite.edit(self.repo.root, 'FOO', version,
                                                  [('pulled_by', ['kit/x'])]), 'invalid')
        self.assertIn('`pulled_by` is written only by', e.message)

    def test_the_owner_prices_and_reprices_with_no_ruling_and_the_writes_take_none(self):
        # an OWNER route: the store's set-price-owner row is the owner's and the role check ran
        # (role_checked), so no ruling is asked for, and none of the writes takes one
        version = self.read('FOO')['version']
        for write in (lambda: recordwrite.edit(self.repo.root, 'FOO', version,
                                               [('priority', ['2'])], ruling=FIRST),
                      lambda: recordwrite.delete(self.repo.root, 'FOO', 'why', ruling=FIRST),
                      lambda: recordwrite.unblock(self.repo.root, 'FOO', ruling=FIRST)):
            with self.assertRaises(TypeError):
                write()
        self.assertEqual(self.field('FOO', 'priority'), ['-'])
        recordwrite.edit(self.repo.root, 'FOO', version,
                         [('priority', ['2']), ('priority_by', ['owner'])])
        self.assertEqual(self.field('FOO', 'priority_by'), ['owner'])
        self.assertEqual(self.field('FOO', 'priority_ruling'), [])
        recordwrite.edit(self.repo.root, 'FOO', self.read('FOO')['version'],
                         [('priority', ['3'])])
        self.assertEqual(self.field('FOO', 'priority'), ['3'])
        self.assertEqual(self.field('FOO', 'priority_ruling'), [])

    def test_a_reprice_drops_the_old_priority_ruling_and_says_so(self):
        self.seed(**rec('OLD', priority='2', priority_by='owner', priority_ruling=FIRST))
        out = recordwrite.edit(self.repo.root, 'OLD', self.read('OLD')['version'],
                               [('priority', ['3'])])
        self.assertEqual(self.field('OLD', 'priority'), ['3'])
        self.assertEqual(self.field('OLD', 'priority_ruling'), [])
        self.assertTrue(any(FIRST + ' dropped' in line for line in out['lines']), out['lines'])

    def test_a_pm_price_is_written_with_no_ruling(self):
        out = recordwrite.edit(self.repo.root, 'FOO', self.read('FOO')['version'],
                               [('priority', ['2']), ('priority_by', ['pm'])])
        self.assertEqual(self.field('FOO', 'priority_by'), ['pm'])
        self.assertEqual(self.field('FOO', 'priority_ruling'), [])
        self.assertFalse(any(ASKS in line for line in out['lines']), out['lines'])

    def test_a_priority_per_ruling_is_no_longer_written(self):
        recordwrite.edit(self.repo.root, 'FOO', self.read('FOO')['version'],
                         [('priority', ['2']), ('priority_by', ['owner'])])
        e = self.refused(lambda: recordwrite.edit(self.repo.root, 'FOO', self.read('FOO')['version'],
                                                  [('priority_per', [FIRST])]), 'invalid')
        self.assertIn('`priority_per` is no longer written', e.message)
        self.assertEqual(self.field('FOO', 'priority_per'), [])

    def test_a_record_not_there_is_not_found(self):
        self.refused(lambda: recordwrite.edit(self.repo.root, 'BAR', 'x:y', [('effort', ['m'])]),
                     'not_found')


class Delete(Case):

    def test_a_deleted_record_reads_as_deleted(self):
        self.named()
        recordwrite.add(self.repo.root, 'FOO', SUMMARY, ['tools/x.py'])
        out = recordwrite.delete(self.repo.root, 'FOO', 'the owner says it should not exist')
        self.assertIn('DELETED FOO', out['lines'])
        got = self.read('FOO')
        self.assertEqual((got['where'], got['removed']['how']), ('completed', 'deleted'))

    def test_a_live_pull_is_refused_and_the_record_stays(self):
        self.named()
        self.repo.git('branch', 'kit/work')
        self.seed(FOO=record_text(state='pulled', pulled_by='kit/work',
                                  pulled_at='2026-10-01'))
        e = self.refused(lambda: recordwrite.delete(self.repo.root, 'FOO',
                                                    'the owner says it should not exist'),
                         'invalid')
        self.assertIn('release it first', e.message)
        self.assertEqual(self.read('FOO')['where'], 'open')


AT, LATER, EARLIER = ('2026-10-02T09:00:%s+00:00' % s for s in ('10', '20', '05'))


class Answer(Case):
    """answer: a hub question's answer filed into the record it is bound to (plan_answer),
    idempotent on the question and its `at`."""

    def setUp(self):
        Case.setUp(self)
        self.named()
        self.seed(**rec('FOO', state='parked', q='Q-101', parked_at='2026-10-01'))

    def answer(self, at=AT, qid='Q-101', rid='FOO', **kw):
        return recordwrite.answer(self.repo.root, rid, qid, at, 'Which one?', 'first', **kw)

    def tip(self):
        return self.repo.git('rev-parse', smod.STORE_REF).strip()

    def test_the_last_answer_is_filed_and_unparks_the_record(self):
        out = self.answer(note='the first, please', via='web')
        self.assertTrue(any('UNPARKED' in line for line in out['lines']), out['lines'])
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        self.assertEqual(self.field('FOO', 'answer'), ['Q-101 ' + AT])
        body = self.read('FOO')['body']
        self.assertIn('## Owner answer (Action Hub, %s)' % AT, body)
        self.assertIn('Note: the first, please', body)
        self.assertIn('Via: web', body)
        log = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertIn('Session: owner-hub', log)

    def test_the_same_answer_again_and_an_older_one_change_nothing(self):
        self.answer()
        tip = self.tip()
        for at in (AT, EARLIER):
            with self.subTest(at=at):
                out = self.answer(at)
                self.assertTrue(any('no change' in line for line in out['lines']), out['lines'])
                self.assertEqual(self.tip(), tip)
        self.assertEqual(self.field('FOO', 'answer'), ['Q-101 ' + AT])

    def test_a_newer_answer_replaces_its_line(self):
        self.answer()
        self.answer(LATER)
        self.assertEqual(self.field('FOO', 'answer'), ['Q-101 ' + LATER])

    def test_an_open_question_on_the_record_keeps_it_parked(self):
        out = self.answer(waiting=['Q-102'])
        self.assertTrue(any('still waits on Q-102' in line for line in out['lines']), out['lines'])
        self.assertEqual(self.field('FOO', 'state'), ['parked'])

    def test_an_other_answer_marks_the_record_for_review(self):
        self.answer(other=True)
        self.assertRegex(self.field('FOO', 'review')[0], r'^owner-answer \d{4}-\d{2}-\d{2}$')

    def test_a_record_not_there_is_not_found_and_a_bad_answer_is_invalid(self):
        self.refused(lambda: self.answer(rid='NOPE'), 'not_found')
        self.refused(lambda: self.answer(qid='X-1'), 'invalid')
        self.refused(lambda: self.answer(at='2026-10-02 09:00'), 'invalid')
        self.assertEqual(self.field('FOO', 'answer'), [])

    def test_a_lost_race_plans_again_on_the_record_the_winner_wrote(self):
        # the other question's answer publishes between this write's plan and its publish: the
        # write plans again, sees both answers and so ends the wait
        real, attempts = recordwrite.opener.open_store, []

        def opened(*a, **kw):
            ctx = real(*a, **kw)
            if not attempts:
                def hook(attempt):
                    attempts.append(attempt)
                    if attempt == 0:
                        self.answer(qid='Q-102', at=LATER)
                ctx.store.before_publish = hook
            return ctx
        with mock.patch.object(recordwrite.opener, 'open_store', opened):
            out = self.answer(waiting=['Q-102'])
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(sorted(self.field('FOO', 'answer')), ['Q-101 ' + AT, 'Q-102 ' + LATER])
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        self.assertTrue(any('UNPARKED' in line for line in out['lines']), out['lines'])


class ConditionMet(Case):
    """condition_met: the unblock-conditions job's write, one commit under Session: hub-condition,
    and nothing on a second call."""

    def setUp(self):
        Case.setUp(self)
        self.named()
        self.seed(**rec('FOO', state='blocked', blocked_on='live-client',
                        until='live-client todo:T-5 done'))

    def tip(self):
        return self.repo.git('rev-parse', smod.STORE_REF).strip()

    def test_one_commit_under_its_own_session_then_nothing(self):
        before = self.tip()
        out = recordwrite.condition_met(self.repo.root, 'FOO', 'live-client', 'todo:T-5 done')
        self.assertIs(out['unblocked'], True)
        self.assertEqual(self.field('FOO', 'state'), ['open'])
        self.assertEqual(self.field('FOO', 'until'), [])
        self.assertEqual(self.repo.git('rev-parse', self.tip() + '~1').strip(), before)
        log = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertIn('Session: %s' % recordwrite.CONDITION_SESSION, log)
        self.assertEqual(recordwrite.CONDITION_SESSION, 'hub-condition')
        self.assertIn('condition met: todo:T-5 done', log)
        tip = self.tip()
        again = recordwrite.condition_met(self.repo.root, 'FOO', 'live-client', 'todo:T-5 done')
        self.assertIs(again['unblocked'], False)
        self.assertEqual(self.tip(), tip)

    def test_free_text_is_invalid_and_writes_nothing(self):
        tip = self.tip()
        self.refused(lambda: recordwrite.condition_met(self.repo.root, 'FOO', 'live-client',
                                                       'when it ships'), 'invalid')
        self.assertEqual(self.tip(), tip)


if __name__ == '__main__':
    unittest.main()
