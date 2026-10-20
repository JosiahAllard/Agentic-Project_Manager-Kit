"""kitlib.gate.budget: the branch gate budget (docs/gate.md "The branch gate budget"). A branch
gets budget.RUNS gate runs; before booking one more, its branch gate is REFUSED until `gate.py
allow` gives it more. A run counted is a machine-lock history line of the scratch store: written
here by Store.history_append, or by a real branch gate run (`--full`, so nothing is reused)."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import budget, runner
from kitlib.gate import steps as S
from kitlib.lock import store as lockstore
from tests.gate import fixtures

BRANCH = 'kit/feature'
SLUG = runner.slug_of(BRANCH)
AGENT = {'role': 'agent', 'host': [912345, 77], 'ancestors': [[912345, 77]], 'session': 'S1',
         'name': 't'}
OWNER = {'role': 'owner', 'host': None, 'ancestors': [], 'session': 'S1', 'name': 't'}


def load_gate_py():
    spec = importlib.util.spec_from_file_location('kit_gate_tool_budget',
                                                  os.path.join(S.KIT_TOOLS, 'gate.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BudgetCase(fixtures.GateRepoCase):
    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        self.name = runner.repo_name(kconfig.load(self.wt), self.wt)
        self.n = 0
        self.as_caller(AGENT)
        self.made(60)

    def made(self, ago_s):
        """Writes the branch's reflog file as git would when it made the branch ago_s seconds ago
        (the tests' fake git writes no reflog)."""
        path = os.path.join(self.repo.common_dir(), 'logs', 'refs', 'heads', 'kit', 'feature')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        t = int(lockstore.utc_now().timestamp()) - ago_s
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('%s %s Kit Test <kit@example.invalid> %d -0400\tbranch: Created from main\n'
                    % ('0' * 40, 'b' * 40, t))
        return path

    def as_caller(self, who):
        patcher = mock.patch.object(budget, 'actor', return_value=who)
        patcher.start()
        self.addCleanup(patcher.stop)

    def line(self, ago_s=0, **fields):
        """One history line, ended ago_s seconds ago: by default a branch gate run of BRANCH."""
        self.n += 1
        end = lockstore.utc_now() - datetime.timedelta(seconds=ago_s)
        rec = {'id': 'r-20261020T120000Z-%06x' % self.n, 'repo': self.name, 'slug': SLUG,
               'kind': 'engine-test', 'since': lockstore.iso(end), 'held_since': lockstore.iso(end),
               'ended': lockstore.iso(end), 'gate': True}
        rec.update(fields)
        rec = {k: v for k, v in rec.items() if v is not None}
        self.store.history_append(rec)
        return rec['id']

    def lines(self, n, **fields):
        return [self.line(**fields) for _ in range(n)]

    def gate(self, full=True):
        said = []
        out = runner.branch_gate(self.wt, full=full, book_kw=self.book_kw, log_dir=self.logs,
                                 say=said.append)
        return out, said

    def history(self):
        return len(self.store.history_records())


class Counting(BudgetCase):
    def test_only_branch_gate_runs_of_this_branch_since_it_was_made_count(self):
        after = budget.since(self.wt, BRANCH)
        self.assertIsNotNone(after)
        mine = self.lines(2)
        mine.append(self.line(repo=self.name.upper()))     # the same repo, in other case
        self.line(priority='land')                          # a landing of the branch
        self.line(slug='kit-other')                         # another branch
        self.line(repo='another-repo')                      # another repository
        self.line(gate=None)                                # a line written before `gate` was kept
        self.line(gate=None, kind='heavy')                  # not a gate run
        self.line(ago_s=3600)                               # before the branch was made
        got = [d['id'] for d in budget.runs(self.store, self.name, SLUG, after)]
        self.assertEqual(sorted(got), sorted(mine))


class Refused(BudgetCase):
    def test_at_the_budget_it_refuses_before_booking(self):
        ids = self.lines(budget.RUNS)
        before = self.history()
        out, _ = self.gate()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(self.book_calls, [])               # nothing booked, nothing waited for
        self.assertEqual(self.history(), before)
        text = '\n'.join(out.lines)
        self.assertIn('REFUSED: the branch gate budget: %s has had %d gate runs' % (
            BRANCH, budget.RUNS), text)
        for rid in ids:
            self.assertIn(rid, text)
        self.assertIn('Instead: stop and send your coordinator', text)
        self.assertIn('gate.py allow %s --runs %d --why' % (BRANCH, budget.RUNS + 1), text)
        self.assertEqual(out.facts['no_record'], 'the branch gate budget')

    def test_below_the_budget_it_books_and_its_own_run_then_counts(self):
        """Four lines: the gate runs as run 5 of 5, and its own history line (which keeps `gate`)
        makes the next gate the sixth: refused."""
        self.lines(budget.RUNS - 1)
        out, said = self.gate()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('gate budget: run %d of %d on %s' % (budget.RUNS, budget.RUNS, BRANCH), said)
        self.assertEqual(len(budget.runs(self.store, self.name, SLUG)), budget.RUNS)
        out, _ = self.gate()
        self.assertEqual(out.code, G.REFUSED, out.lines)

    def test_an_allowance_gives_the_new_total(self):
        self.lines(budget.RUNS)
        path, used = budget.allow(self.wt, BRANCH, budget.RUNS + 2, 'flake fixed, need a rerun',
                                  self.store, who=AGENT)
        self.assertEqual(used, budget.RUNS)
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        self.assertEqual((rec['branch'], rec['runs'], rec['by']), (
            BRANCH, budget.RUNS + 2, {'role': 'agent', 'session': 'S1'}))
        for _ in range(2):
            out, _ = self.gate()
            self.assertEqual(out.code, G.OK, out.lines)
        out, _ = self.gate()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('--runs %d' % (budget.RUNS + 3), '\n'.join(out.lines))


class NeverRefused(BudgetCase):
    def test_the_owner_is_never_refused(self):
        self.as_caller(OWNER)
        self.lines(budget.RUNS)
        out, _ = self.gate()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_runs_from_before_the_branch_was_made_do_not_count(self):
        self.lines(budget.RUNS, ago_s=3600)
        out, _ = self.gate()
        self.assertEqual(out.code, G.OK, out.lines)

    def test_an_allowance_from_before_the_branch_was_made_is_not_read(self):
        path = budget.allow_path(self.wt, BRANCH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = lockstore.iso(lockstore.utc_now() - datetime.timedelta(hours=1))
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'branch': BRANCH, 'runs': 50, 'why': 'x', 'at': old}, f)
        self.assertIsNone(budget.allowance(self.wt, BRANCH, budget.since(self.wt, BRANCH)))
        self.lines(budget.RUNS)
        out, _ = self.gate()
        self.assertEqual(out.code, G.REFUSED, out.lines)

    def test_an_input_it_cannot_read_warns_and_the_gate_runs(self):
        self.lines(budget.RUNS)
        path = budget.allow_path(self.wt, BRANCH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"branch": "kit/fea')                  # cut short: unreadable
        self.assert_warns_and_runs()
        os.remove(path)
        with open(self.made(60), 'w', encoding='utf-8') as f:
            f.write('not a reflog line\n')
        self.assert_warns_and_runs()

    def assert_warns_and_runs(self):
        out, said = self.gate()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue([t for t in said if t.startswith(
            'WARNING: the branch gate budget is not applied')], said)

    def test_no_reflog_is_no_lower_bound(self):
        os.remove(self.made(60))
        self.assertIsNone(budget.since(self.wt, BRANCH))
        self.lines(budget.RUNS, ago_s=3600)
        out, _ = self.gate()
        self.assertEqual(out.code, G.REFUSED, out.lines)


class FullyReused(BudgetCase):
    """A gate whose every step is reused books nothing: never counted, never refused."""
    GATE = {'steps': [{'name': 'unit', 'always': True, 'globs': ['*.py'], 'reuse': True,
                       'cmd': ['{python}', '-c', 'print("ok")'], 'pass': '^ok$'}]}

    def setUp(self):
        p = mock.patch.object(runner, 'kit_identity', return_value={
            'commit': 'a' * 40, 'python': os.path.normcase(os.path.abspath(sys.executable)),
            'clean': True})
        p.start()
        self.addCleanup(p.stop)
        BudgetCase.setUp(self)

    def test_a_fully_reused_gate_is_not_refused(self):
        out, _ = self.gate(full=False)                      # books, and stores its pass
        self.assertEqual(out.code, G.OK, out.lines)
        self.lines(budget.RUNS)
        calls = len(self.book_calls)
        out, said = self.gate(full=False)                   # every step reused: books nothing
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertEqual(len(self.book_calls), calls)
        self.assertFalse([t for t in said if 'gate budget' in t], said)


class AllowEntry(BudgetCase):
    """tools/gate.py allow: arguments and exit codes."""

    def setUp(self):
        BudgetCase.setUp(self)
        self.tool = load_gate_py()
        patcher = mock.patch.object(self.tool, 'Store', return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def allow(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.tool.main(['allow'] + list(args))
            except SystemExit as e:                     # argparse's errors exit (USAGE)
                code = e.code
        return code, buf.getvalue()

    def test_bad_arguments_are_usage_and_write_nothing(self):
        path = budget.allow_path(self.wt, BRANCH)
        for args in (['--runs', str(budget.RUNS), '--why', 'w'], ['--runs', 'x', '--why', 'w'],
                     ['--runs', '7'], ['--runs', '7', '--why', '  ']):
            code, said = self.allow(BRANCH, '-C', self.wt, *args)
            self.assertEqual(code, G.USAGE, (args, said))
            self.assertIn('USAGE:', said)
            self.assertFalse(os.path.exists(path), args)

    def test_it_writes_the_allowance(self):
        self.lines(2)
        code, said = self.allow(BRANCH, '-C', self.wt, '--runs', '8', '--why', 'one more')
        self.assertEqual(code, G.OK, said)
        self.assertIn('ALLOWED %s: 8 gate runs (2 used)' % BRANCH, said)
        rec = budget.allowance(self.wt, BRANCH)
        self.assertEqual((rec['runs'], rec['why']), (8, 'one more'))

    def test_a_branch_that_does_not_exist_is_refused(self):
        code, said = self.allow('kit/no-such', '-C', self.wt, '--runs', '8', '--why', 'w')
        self.assertEqual(code, G.REFUSED, said)
        self.assertIn('REFUSED:', said)


class OutsideARepo(unittest.TestCase):
    def test_allow_path_outside_a_repository_raises(self):
        d = tempfile.mkdtemp(prefix='budget-')
        self.addCleanup(shutil.rmtree, d, True)
        with self.assertRaises(budget.BudgetError):
            budget.allow_path(d, BRANCH)


if __name__ == '__main__':
    unittest.main()
