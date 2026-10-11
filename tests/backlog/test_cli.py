"""tools/backlog.py's arguments and exit codes, run as a subprocess against a temp repository
with a bare remote and a temp home (no machine config, so the ledger is unconfigured)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('set', 'FOO', '--priority', '2', '--priority-by', 'agent', '--effort', 's')
        self.repo.git('checkout', '-b', 'tools/wk')
        out = self.ok('next')
        self.assertIn('FOO', out)
        self.assertTrue(out.startswith('BACKLOG '), out)
        self.assertIn('pulled_by:  tools/wk', self.ok('pull', 'FOO'))
        rc, out = self.run_tool('landable', 'tools/wk')
        self.assertEqual(rc, 2, out)
        self.assertIn('NOT LANDABLE', out)
        self.ok('close', 'FOO', '--reason', 'the work is finished')
        rc, out = self.run_tool('landable', 'tools/wk')
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool('show', 'FOO')
        self.assertEqual(rc, 1, out)
        self.assertIn('NOT FOUND', out)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'closes FOO: the work is finished')

    def test_refusals_exit_2_and_write_nothing(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        for args in (('set', 'FOO', '--priority', '2', '--priority-by', 'pm'),
                     ('add', 'FOO', '--summary', 'again', '--scope', 'tools/y.py'),
                     ('add', 'BAR', '--summary', 'bad', '--scope', '*.py'),
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md')
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('set', 'FOO', '--priority', '3', '--priority_by', 'agent')
        self.ok('set', 'FOO', '--priority-by', 'agent', '--priority', '4')
        self.assertIn('priority: 4', self.ok('show', 'FOO'))

    def test_standing_delegation_comes_from_the_machine_config(self):
        price = ('set', 'FOO', '--priority', '1', '--priority-by', 'owner', '--ruling', 'R-9008')
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        cfg = kuser.config_file(self.home)
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, 'w', encoding='utf-8') as f:
            json.dump({'standing_delegation': 'R-9008'}, f)
        rc, out = self.run_tool(*price)
        self.assertEqual(rc, 2, out)
        self.assertIn('standing delegation', out)
        os.remove(cfg)
        self.ok(*price)                             # unset: no id is refused for it (no ledger here)
        self.assertIn('priority_ruling: R-9008', self.ok('show', 'FOO'))

    def test_check_and_the_list_filters(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md')
        self.ok('block', 'BAR', '--on', 'item:FOO')
        self.assertIn('GREEN', self.ok('check'))
        self.assertIn('BAR', self.ok('list', '--blocked', '--on', 'item:FOO'))
        self.ok('add', 'BAZ', '--summary', 'a third thing', '--scope', 'tools/z.py')
        out = self.ok('list', '--needs-priority')
        self.assertLess(out.index('BAZ'), out.index('FOO'))
        self.assertNotIn('BAR', out)
        out = self.ok('list', '--area', 'docs')
        self.assertIn('BAR', out)
        self.assertNotIn('FOO', out)

    def test_park_list_parked_and_unpark(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        self.ok('add', 'BAR', '--summary', 'another thing', '--scope', 'docs/y.md')
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
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap,{'BAD': 'id: BAD\nstate: open\n'}, 'hand edit\n')
        rc, out = self.run_tool('check')
        self.assertEqual(rc, 1, out)
        self.assertIn('RED', out)
        # a write is refused only for findings it adds, so the store can still be repaired
        rc, out = self.run_tool('add', 'BAZ', '--summary', 'more', '--scope', 'x/**')
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


class StandingDelegation(unittest.TestCase):
    """The machine config's `standing_delegation` reaches the ledger tools/backlog.py builds: with
    the key set, a change citing that id is refused; unset, the same change is priced."""
    run_tool, ok = Cli.run_tool, Cli.ok      # Cli's sandbox, without rerunning Cli's tests
    LEDGER = ('# Rulings ledger (test fixture)\n\n'
              '## R-9168 | 2026-09-25 08:08Z | kit | typed\n\nstand-in text\nItems: FOO\n')

    def setUp(self):
        Cli.setUp(self)
        self.home = self.env['USERPROFILE']
        self.ledger = os.path.join(self.home, 'rulings.md')
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write(self.LEDGER)
        with open(os.path.join(self.repo.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'backlog': {'ledger_repo': 'kit'}}, f)    # the ledger's repo column here
        self.repo.git('add', '.kit.json')
        self.repo.git('commit', '-q', '-m', 'kit config')

    def configure(self, **extra):
        os.makedirs(kuser.config_dir(self.home), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(dict(ledger_path=self.ledger, **extra), f)

    def price(self):
        self.ok('add', 'FOO', '--summary', 'a thing to do', '--scope', 'tools/x.py')
        return self.run_tool('set', 'FOO', '--priority', '1', '--priority-by', 'owner',
                             '--ruling', 'R-9168')

    def test_the_configured_standing_delegation_is_refused_as_a_ruling(self):
        self.configure(standing_delegation='R-9168')
        rc, out = self.price()
        self.assertEqual(rc, 2, out)
        self.assertIn('`R-9168` is the standing delegation', out)

    def test_without_the_key_the_same_ruling_prices_the_record(self):
        self.configure()
        rc, out = self.price()
        self.assertEqual(rc, 0, out)
        self.assertNotIn('standing delegation', out)


class EmptyLedgerName(unittest.TestCase):
    """A configured ledger and a `.kit.json` naming neither `backlog.ledger_repo` nor `repo.name`:
    the ruling checks report one config error each (never a FAIL per record naming an empty repo
    column), stay red, and a ruled write is refused with the same keys named."""
    run_tool, ok = Cli.run_tool, Cli.ok
    LEDGER = StandingDelegation.LEDGER

    def setUp(self):
        Cli.setUp(self)
        self.home = self.env['USERPROFILE']
        self.ledger = os.path.join(self.home, 'rulings.md')
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write(self.LEDGER)
        for rid in ('FOO', 'BAR'):       # priced with no ledger configured: the shape alone is read
            self.ok('add', rid, '--summary', 'a thing to do', '--scope', 'tools/%s.py' % rid.lower())
            self.ok('set', rid, '--priority', '1', '--priority-by', 'owner', '--ruling', 'R-9168')
        os.makedirs(kuser.config_dir(self.home), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': self.ledger}, f)

    def kit_json(self, data):
        with open(os.path.join(self.repo.root, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)
        self.repo.git('add', '.kit.json')
        self.repo.git('commit', '-q', '-m', 'kit config')

    def test_check_reports_one_config_error_and_stays_red(self):
        rc, out = self.run_tool('--no-fetch', 'check')
        self.assertEqual(rc, 1, out)
        self.assertTrue(out.rstrip().endswith('RED'), out)
        self.assertNotIn('is not a ruling header', out)
        self.assertNotIn('FAIL', out.replace('FAILED', ''))
        line = [l for l in out.splitlines() if l.startswith('priority rulings')]
        self.assertEqual(len(line), 1, out)
        self.assertIn('CONFIG ERROR', line[0])
        for key in ('`backlog.ledger_repo`', '`repo.name`', '.kit.json', '2 ruled records'):
            self.assertIn(key, line[0])

    def test_a_ruled_write_is_refused_naming_the_keys(self):
        self.ok('add', 'BAZ', '--summary', 'a thing to do', '--scope', 'tools/baz.py')
        rc, out = self.run_tool('set', 'BAZ', '--priority', '2', '--priority-by', 'owner',
                                '--ruling', 'R-9168')
        self.assertEqual(rc, 2, out)
        self.assertNotIn('is not a ruling header', out)
        self.assertIn('`backlog.ledger_repo`', out)
        self.assertIn('`repo.name`', out)

    def test_repo_name_names_the_ledger_column_when_ledger_repo_is_unset(self):
        self.kit_json({'repo': {'name': 'kit'}})
        out = self.ok('--no-fetch', 'check')
        self.assertIn('priority rulings 2 ok, 0 FAILED', out)


if __name__ == '__main__':
    unittest.main()
