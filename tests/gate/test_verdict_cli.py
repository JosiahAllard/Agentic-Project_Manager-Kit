"""tools/verdict.py: arguments and exit codes (the queue's behaviour is kitlib.gate.verdicts',
tested in test_verdicts.py), run in this process on the scratch repository of
tests/gate/fixtures.py GateRepoCase."""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from kitlib.gate import steps
from kitlib.gate import verdicts as V
from tests.gate.fixtures import GateRepoCase

VERDICT_PY = os.path.join(steps.KIT_TOOLS, 'verdict.py')


def load_tool():
    spec = importlib.util.spec_from_file_location('kit_verdict_tool_for_tests', VERDICT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VerdictCli(GateRepoCase):
    def setUp(self):
        GateRepoCase.setUp(self)
        self.tool = load_tool()
        self.wt, self.tip = self.branch('a', {'a.txt': 'a\n'})

    def run_tool(self, *args):
        """`(code, stdout, stderr)` of verdict.py with `args`, -C the scratch repository."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.tool.main(list(args) + ['-C', self.repo.root])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def add_args(self, tip=None, **over):
        tip = tip or self.tip
        a = {'--branch': 'kit/a', '--tip': tip, '--verdict': 'LAND WITH FOLLOW-UP', '--risk': 'Low',
             '--reviewer': 'reviewer', '--reviewer-model': 'opus', '--reviewer-id': 'agent-r1',
             '--author': 'Kit Test / worker', '--range': '%s..%s' % (self.main[:10], tip[:10]),
             '--session': 'coord-1'}
        a.update(over)
        args = ['add']
        for k, v in a.items():
            if v is not None:
                args += [k, v]
        return args + ['--record', 'EX-1', '--record', 'EX-2']

    def assert_usage(self, got):
        code, out, err = got
        self.assertEqual(code, 2, (out, err))
        self.assertIn('USAGE:', err)
        self.assertNotIn('QUEUED', out)
        self.assertEqual(V.scan(self.repo.common_dir()), ([], []))

    def test_add_list_drop(self):
        code, out, err = self.run_tool(*self.add_args())
        self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % self.tip), err)
        code, out, _ = self.run_tool('list')
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r'^QUEUED kit/a %s Low LAND_WITH_FOLLOW-UP \S+Z '
                                      r'reviewer=reviewer/opus reviewer_id=agent-r1 '
                                      r'records=EX-1,EX-2$' % self.tip)
        code, out, _ = self.run_tool('drop', 'kit/a', '--tip', self.tip)
        self.assertEqual((code, out.strip()), (0, 'DROPPED kit/a'))
        code, out, _ = self.run_tool('drop', 'kit/a')
        self.assertEqual(code, 1)
        self.assertIn('NOT QUEUED', out)
        self.assertEqual(self.run_tool('list')[:2], (0, 'NOTHING QUEUED\n'))

    def test_a_tip_that_is_not_the_head_is_a_mismatch(self):
        code, out, _ = self.run_tool(*self.add_args(tip=self.main))
        self.assertEqual(code, 3)
        self.assertIn('TIP MISMATCH: kit/a is at %s' % self.tip, out)
        code, out, _ = self.run_tool(*self.add_args(**{'--branch': 'kit/nosuch'}))
        self.assertEqual(code, 3)
        self.assertIn('there is no branch kit/nosuch', out)
        self.assertEqual(V.scan(self.repo.common_dir()), ([], []))

    def test_usage_errors_exit_2_and_queue_nothing(self):
        self.assert_usage(self.run_tool())
        self.assert_usage(self.run_tool(*self.add_args(**{'--reviewer-id': None})))
        self.assert_usage(self.run_tool(*self.add_args(**{'--risk': 'high'})))
        self.assert_usage(self.run_tool(*self.add_args(**{'--range': 'main..kit/a'})))
        self.assert_usage(self.run_tool(*self.add_args(**{'--branch': 'refs/heads/kit/a'})))
        self.assert_usage(self.run_tool('drop', 'kit/a', '--tip', self.tip[:12]))

    def test_a_medium_or_high_risk_self_review_is_refused_with_an_instead_line(self):
        for risk in ('Medium', 'High'):
            for over in ({'--reviewer-id': 'coord-1'}, {'--reviewer': 'pm-kit:coordinator'}):
                with self.subTest(risk=risk, **over):
                    code, out, err = self.run_tool(*self.add_args(**dict(over, **{'--risk': risk})))
                    self.assertEqual(code, 2, (out, err))
                    lines = out.splitlines()
                    self.assertTrue(lines[0].startswith('REFUSED: a %s-risk verdict needs a reviewer '
                                                        'other than the coordinator' % risk), out)
                    self.assertEqual(lines[-1], V.SELF_REVIEW_INSTEAD)
                    self.assertNotIn('QUEUED', out)
                    self.assertEqual(V.scan(self.repo.common_dir()), ([], []))
        code, out, err = self.run_tool(*self.add_args(**{'--risk': 'Low', '--reviewer-id': 'coord-1'}))
        self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % self.tip), err)

    def test_list_drops_a_landed_entry_and_says_so(self):
        self.assertEqual(self.run_tool(*self.add_args())[0], 0)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % self.tip)
        self.repo.git('fetch', '-q', 'origin')
        code, out, _ = self.run_tool('list')
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            'EXPIRED kit/a %s: landed: %s is in main %s' % (self.tip, self.tip[:10], self.tip[:10]),
            'NOTHING QUEUED'])
        self.assertEqual(V.scan(self.repo.common_dir()), ([], []))

    def test_list_keeps_all_when_main_as_fetched_cannot_be_read(self):
        self.assertEqual(self.run_tool(*self.add_args())[0], 0)
        self.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        code, out, _ = self.run_tool('list')
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('KEPT ALL: main as fetched cannot be read (origin/main'),
                        out)
        self.assertRegex(lines[1], r'^QUEUED kit/a %s ' % self.tip)
        self.assertEqual(len(lines), 2, out)

    def test_list_names_a_file_that_is_not_an_entry_and_exits_1(self):
        folder = V.queue_dir(self.repo.common_dir())
        os.makedirs(folder)
        with open(os.path.join(folder, 'kit%2Fb.json'), 'w', encoding='utf-8') as f:
            f.write('{')
        code, out, _ = self.run_tool('list')
        self.assertEqual(code, 1)
        # the common dir comes back case-folded on Windows (kitlib.git.repo.common_dir)
        self.assertTrue(out.startswith('BAD '), out)
        self.assertIn(os.path.normcase(os.path.join(folder, 'kit%2Fb.json')) + ': ',
                      os.path.normcase(out))

    def test_drop_that_cannot_list_the_queue_is_not_done(self):
        folder = V.queue_dir(self.repo.common_dir())
        os.makedirs(folder)
        with open(os.path.join(folder, 'kit%2Fb.json'), 'w', encoding='utf-8') as f:
            f.write('{')
        real = os.listdir

        def listdir(p):
            if os.path.normcase(p) == os.path.normcase(folder):
                raise OSError('listing refused')
            return real(p)
        with mock.patch.object(V.os, 'listdir', side_effect=listdir):
            code, out, _ = self.run_tool('drop', 'kit/b')
        self.assertEqual(code, 1)
        self.assertIn('NOT DONE: ', out)
        self.assertIn('listing refused', out)

    def test_no_repository_is_not_done(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = self.tool.main(['list', '-C', self.tmp])
        self.assertEqual(code, 1)
        self.assertIn('NOT DONE: no git repository', out.getvalue())

    def test_add_with_a_kit_json_that_cannot_be_read_is_not_done(self):
        self.repo.write('.kit.json', '{')
        code, out, _ = self.run_tool(*self.add_args())
        self.assertEqual(code, 1)
        self.assertIn('NOT DONE: the kit configuration cannot be read: ', out)
        self.assertEqual(V.scan(self.repo.common_dir()), ([], []))


class VerdictCliSizeCap(GateRepoCase):
    """add reads "landing".self_review_max_lines from the -C checkout's .kit.json and passes it to
    verdicts.add: a coordinator self-read over it is REFUSED (exit 2, the Instead: line)."""
    KIT = {'landing': {'self_review_max_lines': 3}}

    setUp = VerdictCli.setUp
    run_tool = VerdictCli.run_tool
    add_args = VerdictCli.add_args

    def test_a_self_read_over_the_cap_is_refused_and_a_small_one_queued(self):
        code, out, err = self.run_tool(*self.add_args(**{'--reviewer-id': 'coord-1'}))
        self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % self.tip), err)  # 1 line
        self.repo.write('b.txt', 'b\nb\nb\n', root=self.wt)
        tip = self.repo.commit('more', 'b.txt', cwd=self.wt)
        code, out, err = self.run_tool(*self.add_args(tip=tip, **{'--reviewer-id': 'coord-1'}))
        self.assertEqual(code, 2, (out, err))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(
            'REFUSED: a verdict the coordinator queueing it reviewed itself covers at most 3 '
            'changed lines ("landing".self_review_max_lines), and %s..%s changes 4 (+4 -0 in 2 '
            'files; the larger of the range given and %s...%s from main as fetched)'
            % (self.main[:10], tip[:10], self.main[:10], tip[:10])), out)
        self.assertEqual(lines[-1], V.SELF_REVIEW_INSTEAD)
        code, out, err = self.run_tool(*self.add_args(
            tip=tip, **{'--reviewer': 'pm-kit:analyst', '--reviewer-id': 'agent-a7'}))
        self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % tip), err)

    def test_a_self_read_is_refused_when_main_as_fetched_cannot_be_read(self):
        self.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        code, out, err = self.run_tool(*self.add_args(**{'--reviewer-id': 'coord-1'}))
        self.assertEqual(code, 2, (out, err))
        self.assertTrue(out.startswith(
            'REFUSED: a verdict the coordinator queueing it reviewed itself covers at most 3 '
            'changed lines ("landing".self_review_max_lines), and main as fetched cannot be read '
            '(origin/main: '), out)
        self.assertIn('so the size of %s..%s cannot be told' % (self.main[:10], self.tip[:10]), out)
        code, out, err = self.run_tool(*self.add_args(
            **{'--reviewer': 'pm-kit:analyst', '--reviewer-id': 'agent-a7'}))
        self.assertEqual((code, out.strip()), (0, 'QUEUED kit/a %s' % self.tip), err)


if __name__ == '__main__':
    unittest.main()
