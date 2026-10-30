"""kitlib.gate.submit.follow (land.py --wait-result --submit, docs/gate.md "Submitting a
landing"): after the SUBMITTED line, the landing followed to its end in the same call, the
landing's own outcome lines from its output, then each branch's RESULT line and record exactly
as gate.py wait-result prints them, the exit code the last record's. On the scratch repository of
tests/gate/test_land_submit.py with its fake detached start: the detached landing is run in this
process (submit.restore, then Lander.run), its progress and outcome lines appended to the
submission's output as the detached tool's redirected stdout would be."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import result, submit
from tests.gate.test_land import BRANCH, KIT_ROOT
from tests.gate.test_land_feed import FakeHub
from tests.gate.test_land_submit import SubmitCase

sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import gate as gatecli                                                  # noqa: E402
import land as landcli                                                  # noqa: E402


def gone(pid, ctime=None):
    """process.alive for a landing whose process has exited."""
    return False


class FollowCase(SubmitCase):
    def landed(self, branches, tips):
        """Submit, then run the detached landing here, its lines going to the submission's
        output: (the submission's Outcome, the landing's Outcome)."""
        out, detach = self.submit(branches, tips, expect_main=self.main)
        self.assertEqual(out.code, G.OK, out.lines)
        output = out.facts['output']
        sub = self.restored(out, detach, branches, tips, expect_main=self.main)

        def say(line):
            with open(output, 'a', encoding='utf-8', newline='\n') as f:
                f.write(line + '\n')
        lander = self.lander(branches if len(branches) > 1 else branches[0], self.main,
                             tips if len(tips) > 1 else tips[0], submitted=sub, say=say,
                             hub=FakeHub(self))
        got = lander.run()
        for line in got.lines:
            say(line)
        return out, got

    def follow(self, branches, tips, output, **kw):
        kw.setdefault('alive', gone)
        kw.setdefault('poll_s', 0.05)
        return submit.follow(self.repo.root, branches, tips, output, **kw)

    def wait_result(self, branch, tip):
        """What `gate.py wait-result BRANCH --tip TIP --kind land` prints, and its exit code."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = gatecli.do_wait([branch, '--tip', tip, '--kind', 'land', '-C', self.repo.root])
        return buf.getvalue().splitlines(), code


class Follows(FollowCase):
    def test_a_landing_that_lands_gives_its_outcome_lines_then_the_wait_result_lines(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, got = self.landed([BRANCH], [tip])
        self.assertEqual(got.code, G.OK, got.lines)
        fol = self.follow([BRANCH], [tip], out.facts['output'])
        self.assertEqual(fol.code, G.OK, fol.lines)
        waited, code = self.wait_result(BRANCH, tip)
        self.assertEqual(code, G.OK)
        self.assertEqual(fol.lines[-2:], waited)        # the same bytes as gate.py wait-result
        self.assertTrue(waited[0].startswith('RESULT pass land kit/feature at %s: LANDED '
                                             % tip[:10]), waited)
        self.assertEqual(json.loads(fol.lines[-1]), self.record(BRANCH))
        block = fol.lines[:-2]
        self.assertEqual(block, got.lines)              # the landing's outcome lines, as printed
        self.assertTrue([l for l in block if l.startswith('LANDED kit/feature on main at ')],
                        block)
        self.assertFalse([l for l in block if l.startswith(submit.RECORD_LINE)], block)

    def test_a_refusal_exits_with_the_records_code(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, got = self.landed([BRANCH], [self.repo.initial])   # not the branch's tip
        self.assertEqual(got.code, G.REFUSED, got.lines)
        fol = self.follow([BRANCH], [self.repo.initial], out.facts['output'])
        self.assertEqual(fol.code, G.REFUSED, fol.lines)
        self.assertTrue(fol.lines[-2].startswith('RESULT refused land kit/feature at '),
                        fol.lines)
        self.assertIn(got.lines[-1], fol.lines[:-2])

    def test_a_batch_gives_every_branchs_result_in_batch_order(self):
        branches = ['kit/one', 'kit/two']
        _, t1 = self.branch('one', {'one.py': '1\n'})
        _, t2 = self.branch('two', {'two.py': '2\n'})
        out, got = self.landed(branches, [t1, t2])
        self.assertEqual(got.code, G.OK, got.lines)
        fol = self.follow(branches, [t1, t2], out.facts['output'])
        self.assertEqual(fol.code, G.OK, fol.lines)
        results = [l.split(' at ')[0] for l in fol.lines if l.startswith('RESULT ')]
        self.assertEqual(results, ['RESULT pass land kit/one', 'RESULT pass land kit/two'])
        self.assertEqual(fol.lines[-4:-2], self.wait_result('kit/one', t1)[0])
        self.assertEqual(fol.lines[-2:], self.wait_result('kit/two', t2)[0])

    def test_a_landing_whose_process_died_ends_error_and_exits_failed(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, detach = self.submit([BRANCH], [tip], expect_main=self.main)
        sub = self.restored(out, detach, [BRANCH], [tip], expect_main=self.main)
        rr = sub['runs'][0]
        rr.pid, rr.ctime = self.dead_pid(), None    # the landing was killed mid-run
        rr.start(log=out.facts['output'])
        fol = self.follow([BRANCH], [tip], out.facts['output'], alive=None)
        self.assertEqual(fol.code, G.FAILED, fol.lines)
        self.assertTrue(fol.lines[-2].startswith('RESULT error land kit/feature at '), fol.lines)
        self.assertEqual(self.record(BRANCH)['verdict'], 'error')

    def test_a_landing_still_running_after_its_record_is_a_warning_never_a_hang(self):
        _, tip = self.branch('feature', {'a.py': 'a\n'})
        out, got = self.landed([BRANCH], [tip])
        polls = []
        fol = self.follow([BRANCH], [tip], out.facts['output'], exit_wait_s=0,
                          alive=lambda pid, ctime=None: polls.append(pid) or True)
        self.assertEqual(fol.code, G.OK, fol.lines)
        self.assertEqual(polls, [self.record(BRANCH)['pid']])
        self.assertTrue(fol.lines[0].startswith('WARNING: the landing process (pid %d) had not '
                                                'exited 0 s after its result record' % polls[0]),
                        fol.lines)
        self.assertEqual(fol.lines[1:-2], got.lines)    # what the output held, all the same
        self.assertTrue(fol.lines[-2].startswith('RESULT pass land kit/feature'), fol.lines)


class OutcomeBlock(unittest.TestCase):
    def output(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        path = os.path.join(d, 'x.land.log')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(''.join(l + '\n' for l in lines))
        return path

    def test_the_lines_after_the_last_record_line(self):
        path = self.output(['=== landing', 'result record: a', 'merged', 'result record: a',
                            'GATE OK', 'LANDED x'])
        self.assertEqual(submit.outcome_block(path), ['GATE OK', 'LANDED x'])

    def test_at_most_the_last_block_max_lines_saying_how_many_were_left_out(self):
        path = self.output(['result record: a'] + ['l%d' % i for i in range(70)])
        got = submit.outcome_block(path, most=60)
        self.assertEqual(len(got), 61)
        self.assertTrue(got[0].startswith('... 10 earlier line(s) of the landing\'s output: '))
        self.assertEqual(got[1:], ['l%d' % i for i in range(10, 70)])

    def test_no_record_line_gives_every_line_and_no_file_one_warning(self):
        self.assertEqual(submit.outcome_block(self.output(['a', 'b'])), ['a', 'b'])
        got = submit.outcome_block(os.path.join(tempfile.gettempdir(), 'no', 'such.land.log'))
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith('WARNING: the landing\'s output '), got)


SUBMITTED = ('SUBMITTED kit/a tip abc1234 wait: python C:/kit/tools/gate.py wait-result kit/a '
             '--tip abc1234 --kind land -C Y:/repo')


class TheTool(unittest.TestCase):
    """land.py's arguments, exit codes and the order of its output; kitlib.gate.submit mocked."""

    def run_tool(self, argv, submitted, follow=None):
        seen = {}
        out = io.StringIO()

        def fake_follow(root, branches, tips, output):
            seen['before'] = out.getvalue().splitlines()
            seen['args'] = (branches, tips, output)
            if isinstance(follow, BaseException):
                raise follow
            return follow
        with mock.patch.object(landcli.submitmod, 'submit', lambda *a, **k: submitted), \
                mock.patch.object(landcli.submitmod, 'follow', fake_follow), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = landcli.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue().splitlines(), seen

    ARGV = ['-C', 'Y:/repo', 'kit/a', '--expect-tip', 'abc1234', '--reviewed-by', 'r1']

    def ok(self):
        return G.Outcome(G.OK, ['REVIEW kit/a abc1234: LAND by r/opus (r1)', SUBMITTED],
                         output='Z:/x.land.log')

    def test_the_submitted_line_is_printed_before_the_wait_and_the_code_is_the_results(self):
        code, lines, seen = self.run_tool(self.ARGV + ['--wait-result', '--submit'], self.ok(),
                                          G.Outcome(G.STUCK, ['STUCK x', 'RESULT stuck ...']))
        self.assertEqual(seen['before'], ['REVIEW kit/a abc1234: LAND by r/opus (r1)', SUBMITTED])
        self.assertEqual(seen['args'], (['kit/a'], ['abc1234'], 'Z:/x.land.log'))
        self.assertEqual(lines[2:], ['STUCK x', 'RESULT stuck ...'])
        self.assertEqual(code, G.STUCK)

    def test_a_refused_submission_waits_for_nothing(self):
        code, lines, seen = self.run_tool(self.ARGV + ['--wait-result', '--submit'],
                                          G.Outcome(G.REFUSED, ['REFUSED: no; nothing was changed']))
        self.assertEqual((code, lines, seen), (G.REFUSED, ['REFUSED: no; nothing was changed'], {}))

    def test_a_plain_submit_is_unchanged(self):
        code, lines, seen = self.run_tool(self.ARGV + ['--submit'], self.ok())
        self.assertEqual((code, lines[-1], seen), (G.OK, SUBMITTED, {}))

    def test_ctrl_c_says_the_landing_goes_on_with_its_wait_command(self):
        code, lines, _ = self.run_tool(self.ARGV + ['--wait-result', '--submit'], self.ok(),
                                       KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertEqual(lines[-1], 'Ctrl+C: the wait stopped; the landing goes on: %s'
                         % SUBMITTED.split(' wait: ', 1)[1])

    def test_wait_result_without_submit_is_usage(self):
        code, _, seen = self.run_tool(['-C', 'Y:/repo', 'kit/a', '--expect-tip', 'abc1234',
                                       '--wait-result'], self.ok())
        self.assertEqual((code, seen), (G.USAGE, {}))

    def test_the_instead_command_keeps_wait_result(self):
        a = landcli.parser().parse_args(['-C', 'Y:/repo', 'kit/a', '--expect-tip', 'abc1234',
                                         '--wait-result', '--submit'])
        self.assertEqual(landcli.again_args(a, 'Y:/repo')[-1], '--wait-result')
        self.assertNotIn('--wait-result', landcli.child_args(a, 'Y:/repo'))


if __name__ == '__main__':
    unittest.main()
