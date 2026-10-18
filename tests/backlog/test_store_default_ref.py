"""The `ref` store reached by `.kit.json`'s default alone is read-only (docs/backlog.md "Stores"):
a write there is refused and names where to write instead, a named `ref` store is written as
before (records written to a stale default ref were once lost to the live PM data store)."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import config as kconfig
from kitlib import pmdata
from kitlib.backlog import at_landing
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal

from tests.backlog.helpers import ADD_FLAGS, priced, record_text
from tests.gitfixture import TempRepo, fixture_env
from tests.home import temp_home

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
HEAD = 'refs/backlog/main is the default backlog store'
OPT_IN = '"backlog": {"store": "ref"}'


class DefaultRef(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root

    def kit_json(self, data, root=None):
        with open(os.path.join(root or self.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def backend(self, store=None):
        return smod.open_backend(self.root, kconfig.load(self.root), fetch=False, store=store)

    def publish(self, backend, *records):
        writes = {}
        for r in records:
            writes.update(r)
        snap = smod.Store(backend, rmod.Rules()).load()
        return backend.publish(snap, writes, 'seed\n')

    def ref_tip(self):
        local = self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF, check=False)
        remote = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        return local, remote

    def test_a_write_to_the_default_store_is_refused_and_writes_nothing(self):
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        self.assertIn(HEAD, str(caught.exception))
        self.assertIn(OPT_IN, str(caught.exception))
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_a_kit_json_without_a_store_is_the_default(self):
        self.kit_json({'backlog': {'id_grammar': 'mixed'}})
        with self.assertRaises(Refusal):
            self.publish(self.backend(), priced('FOO'))
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_a_named_ref_store_is_written(self):
        self.kit_json({'backlog': {'store': 'ref'}})
        tip = self.publish(self.backend(), priced('FOO'))
        self.assertEqual(self.ref_tip(), (tip, '%s\t%s' % (tip, smod.STORE_REF)))

    def test_a_store_given_by_the_caller_is_written(self):
        """`migrate --to ref` names the store itself."""
        tip = self.publish(self.backend(store='ref'), priced('FOO'))
        self.assertEqual(self.ref_tip()[0], tip)

    def test_the_default_store_still_reads(self):
        named = smod.RefBackend(self.root, 'origin', smod.STORE_REF, smod.STORE_REF, fetch=False)
        self.publish(named, priced('FOO'))
        snap = smod.Store(self.backend(), rmod.Rules()).load()
        self.assertIsNotNone(snap.get('FOO'))

    def test_the_refusal_names_the_pm_data_worktree_and_its_store(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        self.kit_json({'backlog': {'store': 'kit/backlog'}}, root=wt)
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        text = str(caught.exception)
        self.assertIn('write to the live store in the PM data worktree (store kit/backlog) '
                      'with -C "%s"' % wt.replace('\\', '/'), text)
        self.assertIn('or set ' + OPT_IN, text)
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_a_pm_data_worktree_naming_no_store_is_not_advised_as_the_live_store(self):
        """It reads the same implicit ref, so a write there is refused too."""
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        text = str(caught.exception)
        self.assertNotIn('write to the live store', text)
        self.assertIn('the PM data worktree "%s" names no "backlog".store either, so a write there '
                      'is refused too; set %s' % (wt.replace('\\', '/'), OPT_IN), text)
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_a_pm_data_worktree_naming_the_ref_store_is_where_to_write_it(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        self.kit_json({'backlog': {'store': 'ref'}}, root=wt)
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        self.assertIn('write to the live store in the PM data worktree (store ref) with -C "%s"'
                      % wt.replace('\\', '/'), str(caught.exception))

    def test_an_unreadable_pm_data_kit_json_is_named_and_not_offered(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        with open(os.path.join(wt, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write('{not json')
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        text = str(caught.exception)
        self.assertNotIn('write to the live store', text)
        self.assertIn('the PM data worktree was not found (cannot read the .kit.json of the PM data '
                      'worktree %s: ' % wt.replace('\\', '/'), text)
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_the_refusal_in_the_pm_data_worktree_itself_never_points_at_itself(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        backend = smod.open_backend(wt, kconfig.load(wt), fetch=False)
        with self.assertRaises(Refusal) as caught:
            self.publish(backend, priced('FOO'))
        text = str(caught.exception)
        self.assertNotIn('-C "%s"' % wt.replace('\\', '/'), text)
        self.assertTrue(text.endswith('; set %s in .kit.json to write this store' % OPT_IN), text)
        self.assertEqual(self.ref_tip(), ('', ''))

    def test_a_landing_close_refusal_keeps_the_plain_command(self):
        """at_landing._close_in's WARNING for a refused close names the store's own command: a
        landing never writes the default store (it only reads it: tests/backlog/
        test_default_store_warn.py), so no refusal of its reaches the advice."""
        act = at_landing._Act('FOO', 'kit/x', 'abc')
        args = ['close', 'FOO', '--reason', '"landed abc"']
        named = self.backend(store='ref')
        said = at_landing._refused_advice(Refusal('busy'), named, self.root, act, args,
                                          lambda *a: 'PLAIN ' + ' '.join(a))
        self.assertEqual(said, 'close it with: PLAIN close FOO --reason "landed abc"')

    def test_a_missing_pm_data_worktree_is_named(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        with self.assertRaises(Refusal) as caught:
            self.publish(self.backend(), priced('FOO'))
        self.assertIn('the PM data worktree was not found (no worktree has the PM data branch '
                      'pmdata checked out)', str(caught.exception))

    def run_tool(self, *args):
        env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        p = subprocess.run([sys.executable, TOOL, '-C', self.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_the_tool_exits_2_refused_and_a_named_store_takes_the_write(self):
        add = ('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py') + ADD_FLAGS
        rc, out = self.run_tool(*add)
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.startswith('REFUSED: ' + HEAD), out)
        self.assertEqual(self.ref_tip(), ('', ''))
        self.kit_json({'backlog': {'store': 'ref'}})
        rc, out = self.run_tool(*add)
        self.assertEqual(rc, 0, out)
        self.assertIn('ADDED FOO', out)

    def test_a_bare_migrate_onto_the_default_store_is_refused_and_so_is_its_dry_run(self):
        """`migrate` without `--to` and no store named would build the whole store and be refused
        at publish, while its dry run passed: both are refused before anything is read."""
        self.repo.write('docs/backlog/FOO.md', record_text(id='FOO'))
        self.repo.commit('records', 'docs/backlog/FOO.md')
        self.repo.git('push', '-q', 'origin', 'main')
        for args in (('migrate', '--dry-run'), ('migrate',)):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 2, out)
            self.assertTrue(out.startswith('REFUSED: ' + HEAD), out)
            self.assertNotIn('DRY RUN', out)
        self.assertEqual(self.ref_tip(), ('', ''))


if __name__ == '__main__':
    unittest.main()
