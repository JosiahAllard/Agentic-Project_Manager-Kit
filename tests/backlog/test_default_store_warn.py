"""The implicit default store (refs/backlog/main while .kit.json names no "backlog".store;
docs/backlog-store.md "Closing at landing") at a landing and in `check`: a landing only reads it,
the local ref too when the remote has none, and a record that only it holds is a WARNING that the
record is stranded there, never the typo NOTE; `check` warns each of its records and stays green.
Scratch repository and bare remote in temp only."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import config as kconfig
from kitlib import pmdata
from kitlib.backlog import at_landing
from kitlib.backlog import check as checkmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure

from tests.backlog.helpers import priced
from tests.gitfixture import TempRepo, fixture_env
from tests.home import temp_home

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
BRANCH = 'kit/x'
MERGE = 'f' * 40
TODAY = '2026-10-07'
STRANDED = 'the record is stranded there'


def pulled(rid, by=BRANCH, **over):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, **over)


class DefaultStoreWarn(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.main = self.repo.head()

    def kit_json(self, data, root=None):
        with open(os.path.join(root or self.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def seed_default(self, *records, remote=True):
        """Write `records` to refs/backlog/main with a backend that names it (the default one is
        read-only); `remote=False` leaves the local ref only, as a stale clone holds it."""
        named = smod.RefBackend(self.root, 'origin', smod.STORE_REF, smod.STORE_REF, fetch=False)
        writes = {}
        for r in records:
            writes.update(r)
        named.publish(smod.Store(named, rmod.Rules()).load(), writes, 'seed\n')
        if not remote:
            self.repo.git('push', '-q', 'origin', '--delete', smod.STORE_REF)
        return self.tips()

    def tips(self):
        local = self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF, check=False)
        return local, self.repo.git('ls-remote', 'origin', smod.STORE_REF)

    def tip_with(self, *trailers):
        """A commit on main whose message carries `trailers`: the landed branch's tip."""
        args = ['commit-tree', self.main + '^{tree}', '-p', self.main, '-m', 'work']
        if trailers:
            args += ['-m', '\n'.join(trailers)]
        return self.repo.git(*args)

    def land(self, *trailers):
        tip = self.tip_with(*trailers)
        return at_landing.close_landed(self.root, kconfig.load(self.root), BRANCH, MERGE, tip=tip,
                                       base=self.main)

    def pm_worktree(self, *records):
        """The PM data worktree, its folder store kit/backlog holding `records`, committed."""
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.main)
        wt = self.repo.worktree('pm', branch='pmdata')
        self.kit_json({'backlog': {'store': 'kit/backlog'}}, root=wt)
        paths = ['.kit.json']
        for r in records:
            for stem, text in r.items():
                self.repo.write('kit/backlog/%s.md' % stem, text, root=wt)
                paths.append('kit/backlog/%s.md' % stem)
        self.repo.commit('seed the PM data store', *paths, cwd=wt)
        return wt

    def warnings(self, lines):
        return [l for l in lines if l.startswith('WARNING')]

    def test_a_closes_id_only_the_remote_default_ref_holds_warns_and_writes_nothing(self):
        before = self.seed_default(pulled('FOO', by='kit/other'))
        lines = self.land('Closes: FOO')
        warn = self.warnings(lines)
        self.assertEqual(len(warn), 1, lines)
        self.assertIn('a Closes line of its commits names FOO, which only refs/backlog/main holds '
                      '(pulled by kit/other): the default backlog store', warn[0])
        self.assertIn(STRANDED, warn[0])
        self.assertIn('name it in .kit.json ("backlog": {"store": "ref"}) and close it with: '
                      'python', warn[0])
        self.assertIn('close FOO --reason "landed %s"' % MERGE, warn[0])
        self.assertFalse([l for l in lines if l.startswith('NOTE')], lines)
        self.assertEqual(self.tips(), before)

    def test_a_closes_id_only_a_local_default_ref_holds_warns_not_notes(self):
        """The record's case: the remote has no store, the local ref does; the id was a typo
        NOTE ("no backlog store this landing reads holds") although a store held it."""
        before = self.seed_default(pulled('FOO', by='kit/other'), remote=False)
        self.assertEqual(before[1], '')
        lines = self.land('Closes: FOO')
        warn = self.warnings(lines)
        self.assertEqual(len(warn), 1, lines)
        self.assertIn('names FOO, which only the local refs/backlog/main (origin has none) holds',
                      warn[0])
        self.assertIn(STRANDED, warn[0])
        self.assertFalse([l for l in lines if l.startswith('NOTE')], lines)
        self.assertEqual(self.tips(), before)

    def test_an_id_the_pm_data_store_also_holds_closes_there_with_no_default_warning(self):
        self.seed_default(pulled('FOO', by='kit/other'))
        wt = self.pm_worktree(pulled('FOO', by='kit/other'))
        lines = self.land('Closes: FOO')
        self.assertTrue([l for l in lines if l.startswith('closed FOO in the backlog store')],
                        lines)
        self.assertFalse([l for l in lines if STRANDED in l], lines)
        self.assertFalse(os.path.exists(os.path.join(wt, 'kit', 'backlog', 'FOO.md')))

    def test_a_branch_pull_only_in_the_default_ref_warns(self):
        before = self.seed_default(pulled('BAR'))
        lines = self.land()
        warn = self.warnings(lines)
        self.assertEqual(len(warn), 1, lines)
        self.assertIn('it holds BAR pulled only in refs/backlog/main: the default backlog store',
                      warn[0])
        self.assertIn('so BAR was not closed', warn[0])
        self.assertIn(STRANDED, warn[0])
        self.assertEqual(self.tips(), before)

    def test_a_part_of_landing_words_its_default_ref_pull_as_a_release(self):
        before = self.seed_default(pulled('BAR'))
        lines = self.land('Part-of: BAR')
        warn = self.warnings(lines)
        self.assertEqual(len(warn), 1, lines)
        self.assertIn('so BAR was not released', warn[0])
        self.assertIn('release it with: ', warn[0])
        self.assertIn('--as %s release BAR' % BRANCH, warn[0])
        self.assertIn(STRANDED, warn[0])
        self.assertFalse([l for l in lines if l.startswith('NOTE')], lines)
        self.assertEqual(self.tips(), before)

    def test_an_owner_held_default_ref_record_names_the_ruling_in_its_close_command(self):
        before = self.seed_default(pulled('FOO', by='kit/other', priority='-1', priority_by='-',
                                          owner_ask='may this ship?'))
        lines = self.land('Closes: FOO')
        warn = self.warnings(lines)
        self.assertEqual(len(warn), 1, lines)
        self.assertIn('close FOO --reason "landed %s" --ruling R-nnnn' % MERGE, warn[0])
        self.assertEqual(self.tips(), before)

    def test_a_named_ref_store_closes_as_before(self):
        self.kit_json({'backlog': {'store': 'ref'}})
        before = self.seed_default(pulled('FOO', by='kit/other'))
        lines = self.land('Closes: FOO')
        self.assertTrue([l for l in lines if l.startswith('closed FOO in the backlog store')],
                        lines)
        self.assertFalse([l for l in lines if STRANDED in l], lines)
        self.assertNotEqual(self.tips(), before)

    def test_a_named_folder_store_never_reads_a_stranded_default_ref(self):
        self.kit_json({'backlog': {'store': 'docs/backlog'}})
        self.seed_default(pulled('FOO', by='kit/other'), remote=False)
        lines = self.land('Closes: FOO')
        self.assertFalse([l for l in lines if 'refs/backlog/main' in l], lines)

    def test_an_unread_store_suppresses_the_default_warning(self):
        """A PM data record whose worktree is missing: that store was not read, so whether it
        holds the id is unknown, and no stranded WARNING is claimed."""
        self.seed_default(pulled('FOO', by='kit/other'))
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.main)
        lines = self.land('Closes: FOO')
        self.assertTrue([l for l in lines if 'PM data branch\'s backlog store was not read' in l],
                        lines)
        self.assertFalse([l for l in lines if STRANDED in l], lines)

    def default_snap(self):
        backend = smod.open_backend(self.root, kconfig.load(self.root), fetch=False)
        return backend, smod.Store(backend, rmod.Rules()).load()

    def test_check_warns_each_record_of_a_non_empty_default_store_and_stays_green(self):
        self.seed_default(priced('FOO'), pulled('BAR'))
        backend, snap = self.default_snap()
        self.assertTrue(backend.implicit)
        results = checkmod.check_store(snap, implicit=True)
        found = [r for r in results if r.name == checkmod.DEFAULT_STORE]
        self.assertEqual(len(found), 1)
        self.assertEqual(sorted(w.message.split()[0] for w in found[0].warnings), ['BAR', 'FOO'])
        self.assertFalse(found[0].red)
        self.assertIn('2 WARN', found[0].line())
        self.assertFalse([r for r in results if r.red])

    def test_check_adds_no_line_for_an_empty_default_or_a_named_store(self):
        _backend, empty = self.default_snap()
        self.assertFalse([r for r in checkmod.check_store(empty, implicit=True)
                          if r.name == checkmod.DEFAULT_STORE])
        self.seed_default(priced('FOO'))
        _backend, snap = self.default_snap()
        self.assertFalse([r for r in checkmod.check_store(snap)
                          if r.name == checkmod.DEFAULT_STORE])

    def fetching_store(self):
        backend = smod.open_backend(self.root, kconfig.load(self.root), fetch=True)
        self.assertTrue(backend.implicit)
        return backend, smod.Store(backend, rmod.Rules())

    def test_load_for_check_reraises_when_the_remote_cannot_be_asked(self):
        """A local-only default ref, but ls_remote cannot tell whether the remote has one: the
        fetching read's Failure stands and the store is not switched to the local ref."""
        self.seed_default(priced('FOO'), remote=False)
        backend, store = self.fetching_store()
        with mock.patch.object(checkmod.refs, 'ls_remote', return_value=(None, 'offline')):
            with self.assertRaises(Failure) as raised:
                checkmod.load_for_check(store)
        self.assertIn('holds a store', str(raised.exception))
        self.assertTrue(backend.fetch)

    def test_load_for_check_reraises_when_the_remote_holds_the_ref(self):
        """The remote holds refs/backlog/main and the load fails anyway: that Failure stands."""
        self.seed_default(priced('FOO'))
        backend, store = self.fetching_store()
        with mock.patch.object(smod.RefBackend, 'load', side_effect=Failure('broken store')):
            with self.assertRaises(Failure) as raised:
                checkmod.load_for_check(store)
        self.assertEqual(str(raised.exception), 'broken store')
        self.assertTrue(backend.fetch)

    def run_tool(self, *args):
        env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        p = subprocess.run([sys.executable, TOOL, '-C', self.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_the_tool_check_prints_the_warn_exit_0(self):
        self.seed_default(priced('FOO'))
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 0, out)
        self.assertIn('default store', out)
        self.assertIn('1 WARN', out)
        self.assertIn('WARN backlog/FOO.md:1 FOO is held by the default backlog store', out)
        self.assertTrue(out.rstrip().endswith('GREEN'), out)
        self.kit_json({'backlog': {'store': 'ref'}})
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('default store', out)

    def test_the_tool_check_reads_a_local_only_default_ref_and_warns(self):
        """The remote has no store, the local ref does: the fetching read refuses, so `check`
        reads the local ref (check.load_for_check), says so, and warns its records."""
        self.seed_default(priced('FOO'), remote=False)
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 0, out)
        self.assertIn('NOTE: origin has no refs/backlog/main; read the local refs/backlog/main',
                      out)
        self.assertIn('WARN backlog/FOO.md:1 FOO is held by the default backlog store', out)
        self.assertTrue(out.rstrip().endswith('GREEN'), out)

    def test_the_tool_check_of_a_named_ref_store_still_refuses_a_local_only_ref(self):
        self.kit_json({'backlog': {'store': 'ref'}})
        self.seed_default(priced('FOO'), remote=False)
        rc, out = self.run_tool('check')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('origin has no refs/backlog/main, but refs/backlog/main holds a store', out)


if __name__ == '__main__':
    unittest.main()
