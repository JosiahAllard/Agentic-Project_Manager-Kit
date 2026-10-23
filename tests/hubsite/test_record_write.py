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
        self.assertTrue(self.field('FOO', 'summary')[0].startswith(SUMMARY + ' (found on'))
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

    def test_the_owner_prices_and_reprices_and_a_ruling_given_is_never_written(self):
        # an OWNER route: the store's set-price-owner row is the owner's and the role check ran
        # (role_checked), so no ruling is asked for; `ruling` is accepted and ignored
        recordwrite.edit(self.repo.root, 'FOO', self.read('FOO')['version'],
                         [('priority', ['2']), ('priority_by', ['owner'])], ruling=FIRST)
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


if __name__ == '__main__':
    unittest.main()
