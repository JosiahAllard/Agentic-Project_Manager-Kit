"""tools/backlog.py's arguments and exit codes, run as a subprocess against a temp repository
with a bare remote and a temp home (no machine config unless a test writes one)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.backlog.helpers import ADD_FLAGS, TODAY, kit_json_ref, priced
from tests.gitfixture import TempRepo, fixture_env, rmtree
from kitlib.config import user as kuser

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')


class Cli(unittest.TestCase):

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        home = self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        self.env = fixture_env({'USERPROFILE': home, 'HOME': home})
        kit_json_ref(self.repo)              # the store these writes go to is named

    def run_tool(self, *args):
        p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=self.env,
                           timeout=120)
        return p.returncode, p.stdout + p.stderr

    def ok(self, *args):
        rc, out = self.run_tool(*args)
        self.assertEqual(rc, 0, out)
        return out

    def test_a_record_through_its_life(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        self.repo.git('checkout', '-b', 'tools/wk')
        out = self.ok('next')
        self.assertIn('FOO', out)
        self.assertTrue(out.startswith('BACKLOG '), out)
        self.assertIn('pulled_by:  tools/wk', self.ok('pull', 'FOO'))
        rc, out = self.run_tool('landable', 'tools/wk')
        self.assertEqual(rc, 0, out)         # its landing closes FOO in the ref store
        self.assertIn('  closes FOO, which it holds pulled (a slice branch needs a '
                      '`Part-of: FOO` line)', out)
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        rc, out = self.run_tool('landable', 'tools/wk')
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool('show', 'FOO')
        self.assertEqual(rc, 1, out)
        self.assertIn('NOT FOUND', out)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'closes FOO: the work is finished')

    def test_a_pull_in_the_main_checkout_is_refused_and_as_a_branch_pulls(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        rc, out = self.run_tool('pull', 'FOO')
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.startswith('REFUSED: '), out)
        self.assertIn('--as <branch>', out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), tip)
        self.assertIn('pulled_by:  tools/wk', self.ok('--as', 'tools/wk', 'pull', 'FOO'))

    def test_next_and_pull_agree_on_a_record_the_branch_already_holds(self):
        # The branch holds a/**. X (p1) overlaps it and docs/y.md; Y (p2) is docs/y.md.
        for rid, prio, scopes in (('HELD', '3', ('a/**',)), ('X', '1', ('a/x.py', 'docs/y.md')),
                                  ('Y', '2', ('docs/y.md',))):
            scope_args = [a for s in scopes for a in ('--scope', s)]
            self.ok('add', rid, '--summary', 'a thing to do', *(scope_args + list(ADD_FLAGS)))
            self.ok('set', rid, '--priority', prio, '--priority-by', 'agent', '--effort', 's')
        self.repo.git('branch', 'tools/wk')
        self.ok('--as', 'tools/wk', 'pull', 'HELD', '--reason', 'the branch starts with this one')
        plain = self.ok('next')                      # the main checkout: no branch bundles
        self.assertIn('passed over X', plain)
        self.assertNotIn('passed over Y', plain)
        mine = self.ok('--as', 'tools/wk', 'next')   # as the branch: X bundles onto it
        self.assertIn('passed over Y', mine)
        self.assertNotIn('passed over X', mine)
        self.assertIn('tools/wk', mine)
        # Each `next` offers what `pull` takes plainly: Y as the main checkout read it.
        out = self.ok('--as', 'tools/wk', 'pull', 'Y')
        self.assertNotIn('preempt', out)

    def test_a_bad_scope_on_another_branchs_pull_fails_next_and_pull_alike(self):
        # `add` refuses `a/*.py`, so the live pull is seeded through the library.
        self.repo.git('branch', 'tools/other')
        with mock.patch.dict(os.environ, self.env, clear=True):
            backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
            store = smod.Store(backend, rmod.Rules())
            backend.publish(store.load(), dict(
                priced('HOLD', state='pulled', pulled_at=TODAY, pulled_by='tools/other',
                       scope='a/*.py'), **priced('Y', scope='docs/y.md')), 'seed\n')
        for args in (('--as', 'tools/wk', 'next'), ('next',), ('--as', 'tools/wk', 'pull', 'Y')):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 1, (args, out))
            self.assertIn('ERROR: `next` cannot compute an offer: an in-flight record has '
                          '`scope: a/*.py`', out, args)

    def test_refusals_exit_2_and_write_nothing(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        for args in (('set', 'FOO', '--priority', '2', '--priority-by', 'pm'),
                     ('add', 'FOO', '--summary', 'again', '--scope', 'tools/y.py', *ADD_FLAGS),
                     ('add', 'BAR', '--summary', 'bad', '--scope', '*.py', *ADD_FLAGS),
                     ('pull', 'FOO'),
                     ('block', 'FOO', '--on', 'item:NOPE')):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 2, (args, out))
            self.assertRegex(out, r'^(REFUSED|USAGE): ', args)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), tip)

    def test_bad_arguments_exit_2(self):
        for args in (('add', 'FOO'), ('frobnicate',), ('release', 'FOO'), ('retire', 'FOO')):
            rc, out = self.run_tool(*args)
            self.assertEqual(rc, 2, (args, out))

    def test_retire_owner_said_is_refused_and_points_at_ruling(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        rc, out = self.run_tool('retire', 'FOO', '--why', 'no longer wanted at all',
                                '--owner-said', 'go ahead')
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, r'^USAGE: ')
        self.assertIn('--ruling R-nnnn', out)
        self.assertNotIn('go ahead', out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), tip)
        self.assertEqual(self.run_tool('show', 'FOO')[0], 0)

    def test_release_returns_the_callers_pull_to_open(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        self.repo.git('checkout', '-b', 'tools/wk')
        self.ok('pull', 'FOO')
        out = self.ok('release', 'FOO', '--reason', 'not this branch after all')
        self.assertIn('RELEASED FOO - state: pulled -> open', out)
        self.assertIn('was pulled_by: tools/wk\n', out)
        shown = self.ok('show', 'FOO')
        self.assertIn('state: open', shown)
        self.assertNotIn('pulled_by', shown)
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0],
                         'backlog: release FOO from tools/wk: not this branch after all')

    def test_retire_deletes_the_record_and_frees_its_dependents(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md', *ADD_FLAGS)
        self.ok('block', 'BAR', '--on', 'item:FOO')
        out = self.ok('retire', 'FOO', '--why', 'no longer wanted at all')
        self.assertIn('RETIRED FOO', out)
        rc, out = self.run_tool('show', 'FOO')
        self.assertEqual(rc, 1, out)
        self.assertIn('NOT FOUND', out)
        shown = self.ok('show', 'BAR')
        self.assertIn('blocked_on: -', shown)
        self.assertIn('state: open', shown)
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'retires FOO: no longer wanted at all')

    def test_set_flags_take_underscored_and_dashed_spellings(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('set', 'FOO', '--priority', '3', '--priority_by', 'agent')
        self.ok('set', 'FOO', '--priority-by', 'agent', '--priority', '4')
        self.assertIn('priority: 4', self.ok('show', 'FOO'))

    def test_check_and_the_list_filters(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md', *ADD_FLAGS)
        self.ok('block', 'BAR', '--on', 'item:FOO')
        self.assertIn('GREEN', self.ok('check'))
        self.assertIn('BAR', self.ok('list', '--blocked', '--on', 'item:FOO'))
        self.ok('add', 'BAZ', '--summary', 'a third thing', '--scope', 'tools/z.py', *ADD_FLAGS)
        out = self.ok('list', '--needs-priority')
        self.assertLess(out.index('BAZ'), out.index('FOO'))
        self.assertNotIn('BAR', out)
        out = self.ok('list', '--area', 'docs')
        self.assertIn('BAR', out)
        self.assertNotIn('FOO', out)

    def test_park_list_parked_and_unpark(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md', *ADD_FLAGS)
        rc, out = self.run_tool('set-status', 'FOO', 'parked')
        self.assertEqual(rc, 2, out)
        self.ok('set-status', 'FOO', 'parked', '--q', 'Q-001')
        out = self.ok('list', '--status', 'parked')
        self.assertRegex(out, r'FOO .*\[q Q-001, parked 0d\]')
        self.assertNotIn('BAR', out)
        self.assertIn('1 parked', out)
        rc, out = self.run_tool('unpark', 'FOO')
        self.assertEqual(rc, 2, out)
        self.ok('unpark', 'FOO', '--ruling', 'R-9001')
        self.assertIn('resume: R-9001', self.ok('show', 'FOO'))
        self.assertIn('GREEN', self.ok('check'))

    def test_an_invalid_record_on_the_ref_makes_check_red(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap,{'BAD': 'id: BAD\nstate: open\n'}, 'hand edit\n')
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 1, out)
        self.assertIn('RED', out)
        # a write is refused only for findings it adds, so the store can still be repaired
        rc, out = self.run_tool('add', 'BAZ', '--summary', 'more', '--scope', 'x/**', *ADD_FLAGS)
        self.assertEqual(rc, 0, out)

    def test_migrate_dry_run_then_copy_then_refuse(self):
        self.repo.write('docs/backlog/FOO.md', 'id: FOO\narea: xx\npriority: 2\npriority_by: agent\n'
                        'effort: s\nstate: open\nblocked_on: -\nscope: tools/x.py\n'
                        'summary: a thing to do\nmigrated: 12\n')
        self.repo.write('docs/backlog/OLD.md', 'id: OLD\nsummary: done\nclosed: 2026-09-24\n')
        src = self.repo.commit('records', 'docs/backlog/FOO.md', 'docs/backlog/OLD.md')
        self.repo.git('push', '-q', 'origin', 'main')
        out = self.ok('migrate', '--dry-run')
        self.assertIn(src, out)
        self.assertIn('DRY RUN', out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), '')
        out = self.ok('migrate')
        self.assertIn('MIGRATED', out)
        self.assertIn('1 tombstone', out)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF).split('\t')[0]
        self.assertTrue(tip)
        self.assertNotIn('migrated', self.ok('show', 'FOO'))
        rc, out = self.run_tool('show', 'OLD')
        self.assertEqual(rc, 1, out)
        rc, out = self.run_tool('migrate')
        self.assertEqual(rc, 2, out)
        self.assertIn('REFUSED', out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF).split('\t')[0], tip)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')


class RulingCitesUnchecked(unittest.TestCase):
    """A ledger the machine config names is never read by tools/backlog.py: `check` does not test
    a record's cited ruling against it, and a ruled write takes any well-shaped id on its word,
    whether the ledger holds it, retired it, or the machine config calls it the standing
    delegation."""
    run_tool, ok = Cli.run_tool, Cli.ok      # Cli's sandbox, without rerunning Cli's tests
    LEDGER = ('# Rulings ledger (test fixture)\n\n'
              '## R-9168 | 2026-09-25 08:08Z | kit | typed\n\nstand-in text\nItems: FOO\n\n'
              '## R-9007 | 2026-09-25 09:09Z | kit | typed\n\n'
              'Retired: superseded - replaced in the fixture (see -)\n')

    def setUp(self):
        Cli.setUp(self)
        self.ledger = os.path.join(self.home, 'rulings.md')
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write(self.LEDGER)
        kit_json_ref(self.repo, {'backlog': {'ledger_repo': 'kit'}})  # the ledger's repo column

    def configure(self, **extra):
        os.makedirs(kuser.config_dir(self.home), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(dict(ledger_path=self.ledger, **extra), f)

    def test_check_stays_green_over_cites_the_ledger_does_not_hold(self):
        # priced before the ledger is configured; R-9999 is absent from it, R-9007 is retired
        for rid, ruling in (('FOO', 'R-9999'), ('BAR', 'R-9007')):
            self.ok('add', rid, '--summary', 'a thing to do', '--scope', 'tools/%s.py' % rid.lower(),
                    *ADD_FLAGS)
            self.ok('set', rid, '--priority', '1', '--priority-by', 'owner', '--ruling', ruling)
        self.configure()
        rc, out = self.run_tool('--no-fetch', 'check')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.rstrip().endswith('GREEN'), out)
        self.assertNotIn('priority rulings', out)
        self.assertNotIn('hold rulings', out)

    def test_a_ruled_write_takes_an_id_the_ledger_does_not_hold(self):
        self.configure()
        self.ok('add', 'BAZ', '--summary', 'a thing to do', '--scope', 'tools/baz.py', *ADD_FLAGS)
        rc, out = self.run_tool('set', 'BAZ', '--priority', '2', '--priority-by', 'owner',
                                '--ruling', 'R-9999')
        self.assertEqual(rc, 0, out)
        self.assertIn('priority_ruling: R-9999', self.ok('show', 'BAZ'))

    def test_the_standing_delegation_id_is_not_refused(self):
        self.configure(standing_delegation='R-9168')
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        rc, out = self.run_tool('set', 'FOO', '--priority', '1', '--priority-by', 'owner',
                                '--ruling', 'R-9168')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('standing delegation', out)
        self.assertIn('priority_ruling: R-9168', self.ok('show', 'FOO'))


if __name__ == '__main__':
    unittest.main()
