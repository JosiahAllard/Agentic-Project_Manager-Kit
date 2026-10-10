"""kitlib.gate.reuse: a landing's merged gate counts a step that passed in the branch gate of the
exact tree, and runs it again on every doubt (docs/gate.md "Gate reuse"). A scratch repository
with a bare remote, the scratch lock store; the kit identity is pinned so the test does not
depend on the kit checkout being clean."""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, result, reuse, runner
from kitlib.records import gate_ok
from tests.gate import fixtures

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH = 'kit/feature'
OK_CMD = ['{python}', '-c', 'print("ok")']
IDENT = {'commit': 'a' * 40, 'python': os.path.normcase(os.path.abspath(sys.executable)),
         'clean': True}
UNIT = {'name': 'unit', 'always': True, 'cmd': fixtures.RED_AWARE, 'pass': '^ok$', 'reuse': True}


class ReuseCase(fixtures.GateRepoCase):
    GATE = {'steps': [UNIT,
                      {'name': 'merged', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$',
                       'merged_args': ['--merged'], 'reuse': True},
                      {'name': 'plain', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$'}]}
    KIT = {'landing': {}}

    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.ident = dict(IDENT)
        p = mock.patch.object(runner, 'kit_identity', side_effect=lambda: dict(self.ident))
        p.start()
        self.addCleanup(p.stop)
        self.said = []

    def gated_branch(self, name='feature', files=None):
        wt, tip = self.branch(name, files or {'a.py': 'x = 1\n'})
        out = runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNotNone(out.record, out.lines)
        return wt, tip

    def land(self, branch, tip, expect_main=None, **kw):
        kw.setdefault('git_env', self.env)
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        lander = land.Lander(self.repo.root, branch, expect_main or self.main, tip,
                             say=self.said.append, **kw)
        return lander, lander.run()

    def reuse_lines(self):
        return [l for l in self.said if l.startswith(reuse.PREFIX) or l.startswith('REUSED')]

    def assert_ran(self, lander, *names):
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(lander.gate_logs, n + '.log')),
                            '%s did not run: %s' % (n, self.said))

    def assert_none(self, out, lander, why):
        """Landed, every step ran, and the one reuse line says `why`."""
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.reuse_lines(), [reuse.PREFIX + 'none: ' + why], self.said)
        self.assert_ran(lander, 'unit', 'merged', 'plain')
        self.assertNotIn('reused', out.verdict)


class Reuses(ReuseCase):
    def test_a_passed_step_of_the_exact_tree_is_reused(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        logs = lander.gate_logs
        self.assertFalse(os.path.exists(os.path.join(logs, 'unit.log')))     # never ran
        self.assert_ran(lander, 'merged', 'plain')
        lines = self.reuse_lines()
        self.assertIn(reuse.PREFIX + 'merged runs: its merged-gate command differs (merged_args)',
                      lines)
        self.assertTrue(any(l.startswith('REUSED unit: passed in the branch gate of %s'
                                         % tip[:10]) for l in lines), lines)
        with open(os.path.join(logs, 'reuse.log'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines(), lines)
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual({s['name']: s['result'] for s in rec['steps']},
                         {'unit': 'reused', 'merged': 'pass', 'plain': 'pass'})

    def test_fresh_reuses_nothing(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip, fresh=True)
        self.assert_none(out, lander, '--fresh')

    def test_full_reuses_nothing(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip, full=True)
        self.assert_none(out, lander, '--full')

    def test_a_batch_reuses_nothing(self):
        _, tip = self.gated_branch()
        _, tip2 = self.gated_branch('other', {'b.py': 'y = 1\n'})
        lander, out = self.land([BRANCH, 'kit/other'], [tip, tip2])
        self.assert_none(out, lander, 'a batch of 2 branches is gated whole')

    def test_no_branch_gate_reuses_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'no passing branch gate of %s at %s wrote its gate-OK record'
                         % (BRANCH, tip[:10]))

    def test_a_later_red_gate_of_the_same_commit_reuses_nothing(self):
        wt, tip = self.gated_branch()
        self.repo.write('RED', 'x\n', root=wt)          # untracked: the same commit gates red
        self.assertEqual(runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs).code,
                         G.FAILED)
        os.remove(os.path.join(wt, 'RED'))
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'a gate of %s on %s ended fail' % (tip[:10], BRANCH))

    def test_a_tip_that_failed_to_land_is_gated_again(self):
        _, tip = self.gated_branch()
        _, first = self.land(BRANCH, tip, merged_gate=lambda l: G.Outcome(
            G.FAILED, ['GATE FAILED: unit (unit: flaky)'], verdict='GATE FAILED'))
        self.assertEqual(first.code, G.FAILED, first.lines)
        self.said[:] = []
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, '%s at %s was landed before (fail) and did not land: its '
                         'tree is gated again' % (BRANCH, tip[:10]))

    def test_a_branch_gated_against_an_older_main_reuses_nothing(self):
        _, tip = self.gated_branch()
        other = self.repo.worktree('other')
        self.repo.write('o.py', 'o\n', root=other)
        moved = self.repo.commit('other', 'o.py', cwd=other)
        self.repo.git('merge', '-q', '--ff-only', moved)
        self.repo.git('push', '-q', 'origin', 'main')
        lander, out = self.land(BRANCH, tip, expect_main=moved)
        self.assert_none(out, lander, 'the branch gate compared against %s, not --expect-main %s'
                         % (self.main[:10], moved[:10]))

    def test_another_kit_reuses_nothing(self):
        _, tip = self.gated_branch()
        self.ident['commit'] = 'b' * 40
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(self.reuse_lines()[0].startswith(
            reuse.PREFIX + 'none: the branch gate ran under another kit'), self.said)
        self.assert_ran(lander, 'unit')

    def test_a_dirty_kit_reuses_nothing(self):
        _, tip = self.gated_branch()
        self.ident['clean'] = False
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the kit checkout is not a clean commit')

    def test_a_branch_gate_of_another_config_reuses_nothing(self):
        _, tip = self.gated_branch()
        cfg = runner.gate_config(runner.kconfig.load(self.repo.root))
        cfg['fail'] = ['^never$']       # main's gate config changes after the branch gate
        self.repo.write('.kit.json', json.dumps(dict(self.KIT, gate=cfg), indent=1) + '\n')
        moved = self.repo.commit('gate config', '.kit.json')
        self.repo.git('push', '-q', 'origin', 'main')
        lander, out = self.land(BRANCH, tip, expect_main=moved)
        self.assert_none(out, lander, 'the branch gate ran another gate config than '
                         '--expect-main holds')

    def test_an_error_while_planning_reuses_nothing(self):
        _, tip = self.gated_branch()
        with mock.patch.object(runner, 'verify_record', side_effect=RuntimeError('boom')):
            lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'RuntimeError: boom')

    def test_a_record_older_than_the_limit_is_not_reused(self):
        rec = {'ended_utc': '2026-01-01T00:00:00Z'}
        now = datetime.datetime(2026, 1, 2, 1, tzinfo=datetime.timezone.utc)
        self.assertIn('25.0 h ago', reuse._age_why(rec, 24, now))
        self.assertIsNone(reuse._age_why(rec, 26, now))
        self.assertIn('future', reuse._age_why(rec, 24, now - datetime.timedelta(days=2)))
        self.assertIn('no readable ended_utc', reuse._age_why({}, 24, now))


class NeverReused(ReuseCase):
    KIT = {'landing': {'reuse_never': ['docs/*']}}

    def test_a_reuse_never_path_reuses_nothing(self):
        _, tip = self.gated_branch(files={'docs/x.md': 'x\n'})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the merge changes docs/x.md')


class MaxAgeZero(ReuseCase):
    KIT = {'landing': {'reuse_max_age_h': 0}}

    def test_max_age_zero_reuses_nothing(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, '"landing".reuse_max_age_h is 0')


class Groups(ReuseCase):
    GATE = {'setup': OK_CMD,
            'steps': [UNIT,
                      {'name': 'build', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$',
                       'reuse': 'pair'},
                      {'name': 'test', 'always': True, 'cmd': OK_CMD, 'pass': '^ok$',
                       'reuse': 'pair', 'merged_args': ['--merged']}]}

    def test_a_group_is_reused_whole_or_not_at_all(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertIn(reuse.PREFIX + 'build runs: test of its group pair runs', self.reuse_lines())
        self.assert_ran(lander, 'setup', 'build', 'test')


class AllReused(ReuseCase):
    GATE = {'setup': OK_CMD, 'steps': [UNIT]}

    def test_nothing_to_run_books_no_slot_and_runs_no_setup(self):
        _, tip = self.gated_branch()
        with mock.patch.object(runner.lockrun, 'book', side_effect=AssertionError('booked')):
            lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)')
        self.assertEqual(sorted(os.listdir(lander.gate_logs)), ['reuse.log'])
        rec, why = gate_ok.verify(self.repo.root, out.merge)
        self.assertIsNone(why)
        self.assertEqual([(s['name'], s['result']) for s in rec['steps']], [('unit', 'reused')])


class NoneAsked(ReuseCase):
    GATE = fixtures.GATE

    def test_no_step_asking_for_reuse_says_nothing(self):
        _, tip = self.gated_branch()
        lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.reuse_lines(), [])
        self.assertFalse(os.path.exists(os.path.join(lander.gate_logs, 'reuse.log')))
        self.assert_ran(lander, 'unit')


class PriorRecords(unittest.TestCase):
    def test_an_unreadable_folder_is_none(self):
        with mock.patch.object(result, 'result_dir', side_effect=result.ResultError('x')):
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))

    def test_one_unreadable_record_is_none(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        mine = {'kind': 'gate', 'branch': BRANCH, 'tip': 'a' * 40, 'verdict': 'pass'}
        other = dict(mine, branch='kit/other', tip='b' * 40)
        for name, rec in (('mine.gate.json', mine), ('other.gate.json', other)):
            with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                json.dump(rec, f)
        with mock.patch.object(result, 'result_dir', return_value=folder):
            self.assertEqual(reuse.prior_records('.', ['a' * 40]), [mine])
            with open(os.path.join(folder, 'red.gate.json'), 'w', encoding='utf-8') as f:
                f.write('{"kind": "gate", "verdict": "fa')          # cut short: unreadable
            self.assertIsNone(reuse.prior_records('.', ['a' * 40]))


class RecordEdits(ReuseCase):
    """The branch gate's gate-OK record, edited after it was written, still verifies (it carries
    no signature); reuse's own checks refuse it."""

    def edit_record(self, tip, **changes):
        path = gate_ok.record_path(self.repo.root, tip)
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        rec.update(changes)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)

    def test_a_record_whose_config_rev_is_not_the_tip_is_not_reused(self):
        _, tip = self.gated_branch()
        self.edit_record(tip, config_rev=self.main)     # in its history, with the same config
        self.assertIsNone(runner.verify_record(self.repo.root, tip)[1])
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the gate-OK record of %s was not written by its branch '
                         'gate' % tip[:10])

    def test_a_branch_gate_older_than_the_limit_is_not_reused(self):
        _, tip = self.gated_branch()
        ended = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=30)
        self.edit_record(tip, ended_utc=ended.strftime('%Y-%m-%dT%H:%M:%SZ'))
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the branch gate passed 30.0 h ago, more than '
                         '"landing".reuse_max_age_h (24)')


class NeverList(ReuseCase):
    """The config, a section file and the hook module are never reused across, whatever
    "landing".reuse_never says. Owner-only paths are narrowed so the branch may change them."""
    KIT = {'landing': {}, 'owner': {'paths': ['CLAUDE.md']}, 'hook': {'module': 'gate_hook'}}

    def setUp(self):
        ReuseCase.setUp(self)
        kit = dict(self.KIT, gate=self.GATE, forbidden='cfg/forbidden.json')
        self.repo.write('.kit.json', json.dumps(kit, indent=1) + '\n')
        self.repo.write('cfg/forbidden.json', '{}\n')
        self.main = self.repo.commit('forbidden in a section file', '.kit.json',
                                     'cfg/forbidden.json')
        self.repo.git('push', '-q', 'origin', 'main')

    def test_the_kit_json_is_never_reused_across(self):
        kit = dict(self.KIT, gate=self.GATE, forbidden='cfg/forbidden.json',
                   repo={'name': 'renamed'})                   # the gate config is unchanged
        _, tip = self.gated_branch(files={'.kit.json': json.dumps(kit, indent=1) + '\n'})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the merge changes .kit.json')

    def test_a_section_file_is_never_reused_across(self):
        _, tip = self.gated_branch(files={'cfg/forbidden.json': '{"x": 1}\n'})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the merge changes cfg/forbidden.json')

    def test_the_hook_module_is_never_reused_across(self):
        _, tip = self.gated_branch(files={'tools/gate_hook.py': 'GATE_CHECKS = {}\n'})
        lander, out = self.land(BRANCH, tip)
        self.assert_none(out, lander, 'the merge changes tools/gate_hook.py')


REAL_IDENTITY = runner.kit_identity


class RealInterpreter(ReuseCase):
    """runner.kit_identity computed for real; only the kit checkout's commit and cleanliness are
    pinned (the kit checkout running the tests may be dirty)."""

    def setUp(self):
        ReuseCase.setUp(self)

        def real():
            return dict(REAL_IDENTITY(), commit='a' * 40, clean=True)
        p =mock.patch.object(runner, 'kit_identity', side_effect=real)
        p.start()
        self.addCleanup(p.stop)

    def test_kit_identity_names_the_running_interpreter(self):
        here = REAL_IDENTITY()
        self.assertEqual(here['python'], os.path.normcase(os.path.abspath(sys.executable)))
        other = os.path.join(os.path.dirname(sys.executable), 'other-python.exe')
        with mock.patch.object(sys, 'executable', other):
            there = REAL_IDENTITY()
        self.assertEqual(there['python'], os.path.normcase(os.path.abspath(other)))
        self.assertEqual(there['commit'], here['commit'])
        self.assertNotEqual(there, here)

    def test_the_same_interpreter_reuses(self):
        _, tip = self.gated_branch()
        _, out = self.land(BRANCH, tip)
        self.assertEqual(out.verdict, 'GATE OK (reused: unit)', self.said)

    def test_a_branch_gate_under_another_interpreter_is_not_reused(self):
        _, tip = self.gated_branch()
        other = os.path.join(os.path.dirname(sys.executable), 'other-python.exe')
        planned = reuse.plan

        def plan_elsewhere(*a, **kw):       # the steps still run under the real interpreter
            with mock.patch.object(sys, 'executable', other):
                return planned(*a, **kw)
        with mock.patch.object(reuse, 'plan', plan_elsewhere):
            lander, out = self.land(BRANCH, tip)
        self.assertEqual(out.code, G.OK, out.lines)
        line = self.reuse_lines()[0]
        self.assertTrue(line.startswith(reuse.PREFIX + 'none: the branch gate ran under another '
                                        'kit'), self.said)
        self.assertIn('other-python.exe', line)
        self.assert_ran(lander, 'unit', 'merged', 'plain')


def load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Entry(unittest.TestCase):
    """--fresh reaches the landing from tools/land.py and from gate.py land."""

    def landed(self, tool, argv):
        seen = {}

        def fake(root, branch, expect_main, expect_tip, **kw):
            seen.update(kw)
            return G.Outcome(G.OK, ['LANDED fake'])
        with mock.patch.object(land, 'land', fake), mock.patch('sys.stdout'):
            self.assertEqual(tool.main(argv), G.OK)
        return seen

    def test_fresh_reaches_the_landing(self):
        tip = [BRANCH, '--expect-main', 'a' * 40, '--expect-tip', 'b' * 40]
        land_py = load_tool(os.path.join(KIT_ROOT, 'tools', 'land.py'), 'kit_land_tool_reuse')
        self.assertIs(self.landed(land_py, tip + ['--fresh'])['fresh'], True)
        self.assertIs(self.landed(land_py, tip)['fresh'], False)
        gate_py = load_tool(os.path.join(KIT_ROOT, 'tools', 'gate.py'), 'kit_gate_tool_reuse')
        self.assertIs(self.landed(gate_py, ['land'] + tip + ['--fresh'])['fresh'], True)
        self.assertIs(self.landed(gate_py, ['land'] + tip)['fresh'], False)

    def test_fresh_reaches_the_lander(self):
        with mock.patch.object(land.Lander, 'run', lambda self: self.fresh):
            self.assertIs(land.land('.', BRANCH, 'a' * 40, 'b' * 40, fresh=True), True)
            self.assertIs(land.land('.', BRANCH, 'a' * 40, 'b' * 40), False)

    def test_sweep_landed_refuses_fresh(self):
        r = subprocess.run([sys.executable, os.path.join(KIT_ROOT, 'tools', 'land.py'),
                            '--sweep-landed', '--fresh'], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL, env=fixtures.test_env())
        self.assertEqual(r.returncode, G.USAGE, (r.stdout, r.stderr))


if __name__ == '__main__':
    unittest.main()
