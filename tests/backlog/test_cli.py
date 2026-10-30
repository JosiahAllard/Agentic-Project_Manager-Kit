"""tools/backlog.py's arguments, output and exit codes, against a temp repository with a bare remote
and a temp home (no machine config unless a test writes one). The tool's main(argv) runs in this
process (tests/inproc.py) with the env a child would get; test_a_record_through_its_life and
test_bad_arguments_exit_2 run it as a real subprocess, so the process boundary stays covered."""
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
from tests import inproc
from tests.gitfixture import TempRepo, fixture_env, rmtree
from kitlib.config import user as kuser

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
BACKLOG = inproc.load_tool('backlog')


class Cli(unittest.TestCase):

    def setUp(self):
        # the store these writes go to is named: .kit.json committed once, in the template
        self.repo = TempRepo(bare_remote=True, prepare=kit_json_ref,
                             prepare_key='test_cli.Cli.kit_json_ref')
        self.addCleanup(self.repo.cleanup)
        home = self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        self.env = fixture_env({'USERPROFILE': home, 'HOME': home})

    def run_tool(self, *args):
        """backlog.py's main(argv) in this process: (exit code, stdout + stderr)."""
        rc, out, err = inproc.run_main(BACKLOG.main, ['-C', self.repo.root] + list(args),
                                       self.env, self.repo.root)
        return rc, out + err

    def run_process(self, *args):
        """backlog.py as a real child process: (exit code, stdout + stderr)."""
        p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=self.env,
                           timeout=120)
        return p.returncode, p.stdout + p.stderr

    def ok(self, *args):
        rc, out = self.run_tool(*args)
        self.assertEqual(rc, 0, out)
        return out

    def test_a_record_through_its_life(self):
        self.run_tool = self.run_process      # the process boundary: every call a real child
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
        self.assertEqual(rc, 0, out)
        self.assertIn('ENDED: FOO is no longer in', out)
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
        for args in (('set', 'FOO', '--priority-ruling', 'x'),
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
            rc, out = self.run_process(*args)
            self.assertEqual(rc, 2, (args, out))

    def test_retire_takes_no_owner_words_and_no_ruling(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py', *ADD_FLAGS)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        for flag in ('--owner-said', '--ruling'):
            rc, out = self.run_tool('retire', 'FOO', '--why', 'no longer wanted at all', flag, 'x')
            self.assertEqual(rc, 2, out)
            self.assertIn('USAGE: unrecognized arguments: %s' % flag, out)
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
        self.assertEqual(rc, 0, out)
        self.assertIn('ENDED: FOO is no longer in', out)
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
        self.ok('set-status', 'FOO', 'parked')                  # no question yet
        self.assertRegex(self.ok('list', '--status', 'parked'), r'FOO .*\[q \?, parked 0d\]')
        self.ok('set-status', 'FOO', 'parked', '--q', 'Q-001')  # re-pointed on one
        out = self.ok('list', '--status', 'parked')
        self.assertRegex(out, r'FOO .*\[q Q-001, parked 0d\]')
        self.assertNotIn('BAR', out)
        self.assertIn('1 parked', out)
        out = self.ok('unpark', 'FOO')
        self.assertIn('UNPARKED FOO - state: open (was parked on Q-001)', out)
        shown = self.ok('show', 'FOO')
        self.assertIn('state: open', shown)
        self.assertNotIn('resume', shown)
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

    def test_migrate_is_gone_and_exits_as_an_unknown_verb(self):
        """The one-shot store migration is removed: `migrate` is a usage error, and writes
        nothing."""
        for args in (('migrate',), ('migrate', '--dry-run')):
            with self.subTest(args):
                rc, out = self.run_process(*args)
                self.assertEqual(rc, 2, out)
                self.assertIn("invalid choice: 'migrate'", out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), '')
        self.assertEqual(self.repo.git('status', '--porcelain'), '')


class RulingCitesUnchecked(unittest.TestCase):
    """A ledger the machine config names is never read by tools/backlog.py: `check` does not test
    a record's cited ruling against it, and no write takes a ruling: the role check decides an
    owner's price."""
    run_tool, run_process, ok = Cli.run_tool, Cli.run_process, Cli.ok  # Cli's sandbox only
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
        os.makedirs(os.path.dirname(kuser.config_file(self.home)), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(dict(ledger_path=self.ledger, **extra), f)

    def test_check_stays_green_over_cites_the_ledger_does_not_hold(self):
        # records an older tool priced with a cite: R-9999 is absent from the ledger, R-9007 retired
        with mock.patch.dict(os.environ, self.env, clear=True):
            backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
            store = smod.Store(backend, rmod.Rules())
            backend.publish(store.load(), dict(
                priced('FOO', priority='1', priority_by='owner', priority_ruling='R-9999',
                       scope='tools/foo.py'),
                **priced('BAR', priority='1', priority_by='owner', priority_ruling='R-9007',
                         scope='tools/bar.py')), 'seed\n')
        self.configure()
        # a real child: the machine config reaches backlog.py only through the env it inherits
        rc, out = self.run_process('--no-fetch', 'check')
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.rstrip().endswith('GREEN'), out)
        self.assertNotIn('priority rulings', out)
        self.assertNotIn('hold rulings', out)

    def test_an_owner_price_takes_no_ruling(self):
        self.configure()
        self.ok('add', 'BAZ', '--summary', 'a thing to do', '--scope', 'tools/baz.py', *ADD_FLAGS)
        rc, out = self.run_tool('set', 'BAZ', '--priority', '2', '--priority-by', 'owner',
                                '--ruling', 'R-9999')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: unrecognized arguments: --ruling', out)
        self.ok('set', 'BAZ', '--priority', '2', '--priority-by', 'owner')
        shown = self.ok('show', 'BAZ')
        self.assertIn('priority_by: owner', shown)
        self.assertNotIn('priority_ruling', shown)


if __name__ == '__main__':
    unittest.main()
