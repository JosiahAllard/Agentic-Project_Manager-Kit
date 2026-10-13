"""kitlib.gate.result: the result records every branch gate and landing writes (running, then
final), and wait-result, the read-only wait a second session arms on them (docs/gate.md "Result
record")."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, result, runner, steps
from kitlib.proc import process
from kitlib.records import landing
from tests import gitfixture, testutil
from tests.gate import fixtures

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')
FIELDS = ('schema', 'kind', 'branch', 'tip', 'base', 'run_id', 'started_at', 'finished_at',
          'verdict', 'code', 'log', 'summary', 'gate_ok_record', 'cleanup', 'source', 'pid',
          'ctime')
UTC = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$'


def block_dir(root):
    """A file where the result folder goes: every record write then fails."""
    with open(result.result_dir(root), 'w') as f:
        f.write('not a folder\n')


class Names(unittest.TestCase):
    def test_a_branch_name_becomes_one_safe_file_name(self):
        for b in ('kit/gate-result-file', 'a\\b', 'x:y*z?"<>|', 'team/a1/deep', 'K/X'):
            name = result.file_stem(b)
            for bad in '/\\:*?"<>|':
                self.assertNotIn(bad, name, b)
        self.assertNotEqual(result.file_stem('kit/a-b'), result.file_stem('kit-a/b'))
        self.assertNotEqual(result.file_stem('kit/a'), result.file_stem('kit-a'))
        self.assertEqual(result.file_stem('plain-name_1.2'), 'plain-name_1.2')

    def test_no_two_branches_share_a_stem_even_ignoring_case(self):
        names = ['kit/Foo', 'kit/foo', 'kit/FOO', 'kit/%46oo', 'kit/%2546oo', 'Kit/foo',
                 'kit%2Ffoo', 'kit/fo~o']
        folded = [result.file_stem(n).lower() for n in names]
        self.assertEqual(len(set(folded)), len(names), dict(zip(names, folded)))

    def test_windows_device_names_are_encoded(self):
        for b in ('con', 'NUL', 'aux', 'com1', 'lpt9', 'prn', 'con.x', 'nul.gate'):
            stem = result.file_stem(b)
            self.assertNotIn(stem.split('.')[0].lower(), result.DEVICES, (b, stem))
        self.assertNotEqual(result.file_stem('con').lower(), result.file_stem('%63on').lower())

    def test_a_long_branch_is_cut_with_a_hash(self):
        a, b = 'kit/' + 'x' * 300, 'kit/' + 'x' * 299 + 'y'
        self.assertLessEqual(len(result.file_stem(a)), result.MAX_STEM)
        self.assertNotEqual(result.file_stem(a), result.file_stem(b))

    def test_verdict_words(self):
        self.assertEqual(result.verdict_of(G.OK), 'pass')
        self.assertEqual(result.verdict_of(G.FAILED), 'fail')
        self.assertEqual(result.verdict_of(G.REFUSED), 'refused')
        self.assertEqual(result.verdict_of(G.STUCK), 'stuck')
        self.assertEqual(result.verdict_of(G.BUSY), 'busy')
        self.assertEqual(result.verdict_of(G.USAGE), 'usage')
        self.assertEqual(result.verdict_of(6), 'not-run')


class Records(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('a.txt', 'a\n')
        self.tip = self.repo.commit('a', 'a.txt')

    def rec(self, tip=None, verdict='pass', kind='gate', branch='kit/x'):
        return result.build(branch, tip or self.tip, None, kind,
                            G.OK if verdict == 'pass' else G.FAILED, log='L', summary='S',
                            verdict=verdict if verdict == 'running' else None)

    def test_write_is_atomic_and_read_back(self):
        path = result.write(self.repo.root, self.rec())
        self.assertEqual(os.path.dirname(path), result.result_dir(self.repo.root))
        self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])  # no temp
        got, why = result.read(self.repo.root, 'kit/x')
        self.assertIsNone(why)
        self.assertEqual(sorted(got), sorted(FIELDS))
        self.assertEqual(got['tip'], self.tip)
        self.assertEqual(got['verdict'], 'pass')
        self.assertRegex(got['finished_at'], UTC)
        self.assertRegex(got['started_at'], UTC)
        self.assertEqual(result.read(self.repo.root, 'kit/none'), (None, 'none'))

    def test_branches_differing_only_in_case_keep_their_own_records(self):
        result.write(self.repo.root, self.rec(branch='kit/Foo'))
        result.write(self.repo.root, self.rec(branch='kit/foo', verdict='fail'))
        self.assertEqual(result.read(self.repo.root, 'kit/Foo')[0]['verdict'], 'pass')
        self.assertEqual(result.read(self.repo.root, 'kit/foo')[0]['verdict'], 'fail')

    def test_a_device_named_branch_is_written(self):
        result.write(self.repo.root, self.rec(branch='con'))
        self.assertEqual(result.read(self.repo.root, 'con')[0]['branch'], 'con')

    def test_a_replace_held_open_is_retried(self):
        real, calls = os.replace, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('held open')
            return real(a, b)
        with mock.patch.object(result, 'RETRY_SLEEP', 0), mock.patch('os.replace', flaky):
            result.write(self.repo.root, self.rec())
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'pass')

    def test_a_replace_held_open_too_long_is_a_result_error_and_leaves_no_temp(self):
        def held(a, b):
            raise PermissionError('held open')
        with mock.patch.object(result, 'RETRY_SLEEP', 0), mock.patch('os.replace', held):
            with self.assertRaises(result.ResultError):
                result.write(self.repo.root, self.rec())
        self.assertEqual(os.listdir(result.result_dir(self.repo.root)), [])

    def test_run_writes_running_then_final_with_one_run_id(self):
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        path, line = run.start()
        self.assertEqual(line, 'result record: %s' % path)
        got = result.read(self.repo.root, 'kit/x')[0]
        self.assertEqual((got['verdict'], got['code'], got['finished_at']), ('running', None, None))
        run.finish(G.Outcome(G.FAILED, ['GATE FAILED: unit']))
        final = result.read(self.repo.root, 'kit/x')[0]
        self.assertEqual((final['verdict'], final['code']), ('fail', 1))
        self.assertEqual((final['run_id'], final['started_at']), (got['run_id'], got['started_at']))
        run.error(KeyboardInterrupt())
        err = result.read(self.repo.root, 'kit/x')[0]
        self.assertEqual((err['verdict'], err['code'], err['run_id']), ('error', None, got['run_id']))

    def test_the_final_record_carries_the_gate_source(self):
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        run.start()
        self.assertIsNone(result.read(self.repo.root, 'kit/x')[0]['source'])   # not known yet
        run.finish(G.Outcome(G.OK, ['GATE OK'], source='auto'))
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['source'], 'auto')
        run.finish(G.Outcome(G.OK, ['LANDED x']), source='gate.json')     # a landing passes it
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['source'], 'gate.json')

    def test_a_record_from_before_the_source_key_reads_as_none(self):
        old = self.rec()
        del old['source']
        self.assertIsNone(result.source_of(old))
        self.assertTrue(result.answers(old, 'kit/x', 'gate', self.tip))

    def test_run_never_raises_when_it_cannot_write(self):
        block_dir(self.repo.root)
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        for path, line in (run.start(), run.finish(G.Outcome(G.OK, ['GATE OK'])),
                           run.error(ValueError('x'))):
            self.assertIsNone(path)
            self.assertTrue(line.startswith('WARNING: the result record could not be written'))
        with mock.patch.object(result, 'build', side_effect=RuntimeError('a bug')):
            self.assertTrue(run.start()[1].startswith('WARNING: '))

    def test_gate_and_land_records_are_separate_files(self):
        result.write(self.repo.root, self.rec(verdict='fail', kind='land'))
        result.write(self.repo.root, self.rec())
        self.assertEqual(result.read(self.repo.root, 'kit/x', 'gate')[0]['verdict'], 'pass')
        self.assertEqual(result.read(self.repo.root, 'kit/x', 'land')[0]['verdict'], 'fail')

    def test_wait_returns_the_record_at_that_tip(self):
        result.write(self.repo.root, self.rec())
        got, why = result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=0)
        self.assertIsNone(why)
        self.assertEqual(got['tip'], self.tip)
        got, _ = result.wait(self.repo.root, 'kit/x', self.tip[:8].upper(), timeout_s=0)
        self.assertEqual(got['tip'], self.tip)              # an abbreviated tip, any case

    def fake_wait(self, *a, **kw):
        now, slept = [0.0], []

        def sleep(s):
            slept.append(s)
            now[0] += s
        got, why = result.wait(self.repo.root, *a, timeout_s=5, poll_s=1, clock=lambda: now[0],
                               sleep=sleep, **kw)
        return got, why, len(slept)

    def test_wait_ignores_a_record_at_another_tip_until_the_timeout(self):
        result.write(self.repo.root, self.rec(tip='b' * 40))
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))

    def test_wait_ignores_a_running_record(self):
        result.write(self.repo.root, self.rec(verdict='running'))
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))

    def test_wait_ignores_a_record_of_another_branch_or_kind_in_the_file(self):
        path = result.result_path(self.repo.root, 'kit/x')
        result.write(self.repo.root, self.rec(branch='kit/y'))
        os.replace(result.result_path(self.repo.root, 'kit/y'), path)   # kit/y's record, kit/x's file
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))
        result.write(self.repo.root, self.rec(kind='land'))
        os.replace(result.result_path(self.repo.root, 'kit/x', 'land'), path)
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))

    def test_wait_resolves_the_record_path_once(self):
        with mock.patch.object(result, 'result_path', wraps=result.result_path) as rp:
            self.fake_wait('kit/x', self.tip)
        self.assertEqual(rp.call_count, 1)

    def test_wait_refuses_a_nan_or_negative_timeout(self):
        for t in (float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=t)

    def dead_pid(self):
        p = testutil.dead_process()
        self.addCleanup(p.wait)             # holds the Popen, so Windows cannot reuse the pid
        return p.pid

    def test_a_run_names_its_process_in_its_records(self):
        run = result.Run(self.repo.root, 'land', 'kit/x', self.tip)
        run.start()
        got = result.read(self.repo.root, 'kit/x', 'land')[0]
        self.assertEqual((got['pid'], got['ctime']), (os.getpid(), process.own_ctime()))
        self.assertEqual(result.running_state(got), 'live')

    def test_wait_finishes_a_running_record_whose_process_died_as_error(self):
        for kind, what in (('land', 'landing'), ('gate', 'gate')):
            run = result.Run(self.repo.root, kind, 'kit/x', self.tip, base='c' * 40)
            run.pid, run.ctime = self.dead_pid(), None
            run.start(log='L')
            running = result.read(self.repo.root, 'kit/x', kind)[0]
            self.assertEqual(result.running_state(running), 'dead')
            got, why = result.wait(self.repo.root, 'kit/x', self.tip[:8], kind=kind, timeout_s=0)
            self.assertIsNone(why)
            self.assertEqual((got['verdict'], got['code'], got['run_id'], got['started_at'],
                              got['base'], got['log'], got['pid']),
                             ('error', None, run.run_id, running['started_at'], 'c' * 40, 'L',
                              run.pid))
            self.assertEqual(got['summary'], 'the %s process died: pid %d ended without '
                                             'finishing its record' % (what, run.pid))
            self.assertRegex(got['finished_at'], UTC)
            self.assertEqual(result.read(self.repo.root, 'kit/x', kind)[0], got)   # on disk

    def test_wait_leaves_a_running_record_whose_process_lives_or_is_unknown(self):
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)        # this process: live
        run.start()
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))
        old = self.rec(verdict='running')                   # no pid: a record from before the key
        old['started_at'] = '2020-01-01T00:00:00Z'
        result.write(self.repo.root, old)
        self.assertEqual(result.running_state(old), 'unknown')
        self.assertEqual(self.fake_wait('kit/x', self.tip), (None, 'timeout', 5))
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'running')
        self.assertEqual(result.running_state(self.rec(verdict='running')), 'live')  # young
        self.assertIsNone(result.running_state(self.rec()))

    def test_finish_dead_leaves_a_record_another_run_replaced(self):
        dead = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        dead.pid = self.dead_pid()
        dead.start()
        rec = result.read(self.repo.root, 'kit/x')[0]
        newer = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        newer.start()                                       # a new run took the file meanwhile
        got = result.finish_dead(self.repo.root, rec)
        self.assertEqual((got['run_id'], got['verdict']), (newer.run_id, 'running'))
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0], got)

    def test_wait_sees_the_final_record_after_the_running_one(self):
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        result.write(self.repo.root, self.rec())            # an earlier run's pass at this tip
        run.start()                                         # a re-run at the same tip begins
        calls = []

        def sleep(_s):
            calls.append(1)
            if len(calls) == 2:
                run.finish(G.Outcome(G.FAILED, ['GATE FAILED: unit']))
        got, why = result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=60, poll_s=0.01,
                               sleep=sleep)
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['run_id']), ('fail', run.run_id))


class BranchGateWrites(fixtures.GateRepoCase):
    def gate(self, wt, **kw):
        kw.setdefault('log_dir', self.logs)
        return runner.branch_gate(wt, book_kw=self.book_kw, **kw)

    def test_a_pass_writes_a_pass_record_every_checkout_sees(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = []
        out = self.gate(wt, say=said.append)
        self.assertEqual(out.code, G.OK, out.lines)
        rec, why = result.read(self.repo.root, 'kit/feature')      # read from the main checkout
        self.assertIsNone(why)
        self.assertEqual(rec['kind'], 'gate')
        self.assertEqual(rec['branch'], 'kit/feature')
        self.assertEqual(rec['tip'], tip)
        self.assertEqual(rec['base'], self.main)
        self.assertEqual(rec['verdict'], 'pass')
        self.assertEqual(rec['code'], 0)
        self.assertEqual(rec['log'], out.log_dir)
        self.assertEqual(rec['gate_ok_record'], out.record)
        self.assertTrue(rec['summary'].startswith('GATE OK'), rec['summary'])
        self.assertEqual(os.path.normcase(out.result_record),
                         os.path.normcase(result.result_path(wt, 'kit/feature')))
        self.assertFalse(any('result record' in l for l in out.lines), out.lines)  # progress only
        self.assertEqual(len([l for l in said if l.startswith('result record: ')]), 2)  # run + end

    def test_the_running_record_is_there_while_the_gate_runs_and_a_rerun_replaces_it(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        first = self.gate(wt)
        old = result.read(wt, 'kit/feature')[0]
        self.assertEqual((first.code, old['verdict']), (G.OK, 'pass'))
        seen = []
        real = runner._branch_gate

        def spy(*a, **kw):
            seen.append(result.read(wt, 'kit/feature')[0])
            return G.Outcome(G.FAILED, ['GATE FAILED: spy'], head=tip)
        with mock.patch.object(runner, '_branch_gate', spy):
            self.gate(wt)
        self.assertIs(runner._branch_gate, real)
        self.assertEqual(seen[0]['verdict'], 'running')
        self.assertEqual(seen[0]['tip'], tip)
        self.assertNotEqual(seen[0]['run_id'], old['run_id'])
        new = result.read(wt, 'kit/feature')[0]
        self.assertEqual((new['verdict'], new['run_id'], new['started_at']),
                         ('fail', seen[0]['run_id'], seen[0]['started_at']))

    def test_a_red_gate_writes_a_fail_record(self):
        wt, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        rec, _ = result.read(wt, 'kit/feature')
        self.assertEqual((rec['tip'], rec['verdict'], rec['code']), (tip, 'fail', 1))
        self.assertTrue(rec['summary'].startswith('GATE FAILED'), rec['summary'])

    def test_a_refused_gate_writes_a_refused_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.repo.write('.kit.json', '{"gate": {"steps": "nope"}}\n', root=wt)
        self.repo.commit('break config', '.kit.json', cwd=wt)
        tip2 = self.repo.git('rev-parse', 'HEAD', cwd=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        rec, _ = result.read(wt, 'kit/feature')
        self.assertEqual((rec['tip'], rec['verdict'], rec['code']), (tip2, 'refused', 4))
        self.assertNotEqual(tip, tip2)

    def test_an_exception_that_escapes_writes_error_and_is_raised_unchanged(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        for exc in (SystemExit(3), RuntimeError('boom'), KeyboardInterrupt()):
            with mock.patch.object(runner, '_branch_gate', side_effect=exc):
                with self.assertRaises(type(exc)) as cm:
                    self.gate(wt)
            self.assertIs(cm.exception, exc)
            rec = result.read(wt, 'kit/feature')[0]
            self.assertEqual((rec['verdict'], rec['tip'], rec['code']), ('error', tip, None))
            self.assertIn(type(exc).__name__, rec['summary'])

    def test_a_failing_record_write_never_replaces_the_exception(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(result.Run, 'error', side_effect=RuntimeError('write bug')), \
                mock.patch.object(runner, '_branch_gate', side_effect=SystemExit(5)):
            with self.assertRaises(SystemExit) as cm:
                self.gate(wt)
        self.assertEqual(cm.exception.code, 5)

    def test_a_record_that_cannot_be_written_leaves_the_outcome_alone(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        block_dir(wt)
        said = []
        out = self.gate(wt, say=said.append)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[0].startswith('GATE OK'), out.lines)
        self.assertEqual(out.record is not None, True)          # the gate-OK record still written
        self.assertIsNone(out.result_record)
        self.assertTrue(any(l.startswith('WARNING: the result record') for l in said), said)

    def test_plan_writes_no_record(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt, plan=True)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(result.read(wt, 'kit/feature'), (None, 'none'))

    def test_a_detached_head_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.repo.git('switch', '-q', '--detach', tip, cwd=wt)
        self.gate(wt)
        self.assertFalse(os.path.exists(result.result_dir(wt)))


class LandingWrites(fixtures.GateRepoCase):
    def lander(self, branch, expect_main, expect_tip, **kw):
        kw.setdefault('git_env', self.env)
        kw.setdefault('book_kw', self.book_kw)
        kw.setdefault('log_dir', self.logs)
        return land.Lander(self.repo.root, branch, expect_main, expect_tip, **kw)

    def test_a_landing_writes_running_then_a_land_record(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        seen = []

        def merged_gate(lander):
            seen.append(result.read(self.repo.root, 'kit/feature', 'land')[0])
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      slug=lander.branch, log_dir=lander.log_dir,
                                      book_kw=lander.book_kw)
        out = self.lander('kit/feature', self.main, tip, merged_gate=merged_gate).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual((seen[0]['verdict'], seen[0]['tip']), ('running', tip))
        rec, why = result.read(self.repo.root, 'kit/feature', 'land')
        self.assertIsNone(why)
        self.assertEqual((rec['kind'], rec['tip'], rec['base'], rec['verdict']),
                         ('land', tip, self.main, 'pass'))
        self.assertEqual(rec['run_id'], seen[0]['run_id'])
        self.assertTrue(rec['summary'].startswith('LANDED '), rec['summary'])
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)    # still the last line
        self.assertTrue(os.path.isfile(out.result_record))

    def test_the_landing_log_folder_is_recorded(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.lander('kit/feature', self.main, tip).run()
        rec = result.read(self.repo.root, 'kit/feature', 'land')[0]
        self.assertTrue(rec['log'] and rec['log'].startswith(self.logs), rec['log'])

    def test_a_red_landing_writes_a_fail_land_record(self):
        _, tip = self.branch('feature', {'RED': 'x\n'})
        out = self.lander('kit/feature', self.main, tip).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        rec, _ = result.read(self.repo.root, 'kit/feature', 'land')
        self.assertEqual((rec['tip'], rec['verdict']), (tip, 'fail'))
        self.assertTrue(rec['summary'].startswith('NOT LANDED'), rec['summary'])

    def test_a_landing_refused_or_busy_before_work_writes_nothing(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        prior = result.build('kit/feature', tip, self.main, 'land', G.OK, summary='LANDED earlier')
        result.write(self.repo.root, prior)
        out = self.lander('kit/feature', self.main, 'c' * 40).run()    # wrong --expect-tip
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIsNone(out.result_record)
        lock = landing.LandingLock(self.repo.root)
        held = lock.take('full', ['kit/someone'], pid=os.getpid())
        self.addCleanup(lock.release, held['token'])
        out = self.lander('kit/feature', self.main, tip).run()
        self.assertEqual(out.code, G.BUSY, out.lines)
        self.assertEqual(result.read(self.repo.root, 'kit/feature', 'land')[0], prior)

    def test_a_record_that_cannot_be_written_leaves_the_landing_alone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        block_dir(self.repo.root)
        said = []
        out = self.lander('kit/feature', self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)
        self.assertIsNone(out.result_record)
        self.assertTrue(any(l.startswith('WARNING: the result record') for l in said), said)

    def test_an_exception_after_the_checks_writes_error(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def merged_gate(_lander):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.lander('kit/feature', self.main, tip, merged_gate=merged_gate).run()
        rec = result.read(self.repo.root, 'kit/feature', 'land')[0]
        self.assertEqual((rec['verdict'], rec['tip']), ('error', tip))


def gate_py(*args, cwd=None):
    r = subprocess.run([sys.executable, GATE_PY] + list(args), cwd=cwd, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', env=fixtures.test_env(),
                       timeout=120)
    return r.returncode, r.stdout, r.stderr


class WaitResultEntry(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('a.txt', 'a\n')
        self.tip = self.repo.commit('a', 'a.txt')

    def put(self, code, kind='gate', tip=None, verdict=None):
        result.write(self.repo.root, result.build('kit/x', tip or self.tip, None, kind, code,
                                                  summary='S', verdict=verdict))

    def wait(self, *extra, tip=None):
        return gate_py('wait-result', 'kit/x', '--tip', tip or self.tip, '--timeout', '0',
                       '-C', self.repo.root, *extra)

    def test_pass_exits_zero_and_prints_the_record(self):
        self.put(G.OK)
        rc, out, err = self.wait()
        self.assertEqual(rc, 0, (out, err))
        line = [l for l in out.splitlines() if l.startswith('RESULT ')][0]
        self.assertIn('pass', line)
        rec = json.loads(out.splitlines()[-1])
        self.assertEqual(rec['tip'], self.tip)

    def test_any_other_verdict_exits_one(self):
        self.put(G.STUCK)
        rc, out, _ = self.wait()
        self.assertEqual(rc, 1, out)
        self.assertIn('stuck', out)

    def test_no_record_times_out(self):
        rc, out, _ = self.wait()
        self.assertEqual(rc, result.TIMEOUT, out)
        self.assertTrue(out.startswith('TIMEOUT: '), out)

    def test_a_record_at_another_tip_times_out(self):
        self.put(G.OK, tip='d' * 40)
        rc, out, _ = self.wait()
        self.assertEqual(rc, result.TIMEOUT, out)

    def test_a_running_record_times_out(self):
        self.put(None, verdict='running')
        rc, out, _ = self.wait()
        self.assertEqual(rc, result.TIMEOUT, out)

    def test_the_kind_must_match(self):
        self.put(G.OK, kind='land')                     # a land record does not answer a gate wait
        rc, out, _ = self.wait()
        self.assertEqual(rc, result.TIMEOUT, out)
        rc, out, _ = self.wait('--kind', 'land')
        self.assertEqual(rc, 0, out)

    def test_bad_arguments_are_usage(self):
        for args in (('--tip', 'xyz'), ('--tip', 'abc12'), ('--tip', self.tip, '--timeout', 'nan'),
                     ('--tip', self.tip, '--timeout', '-1'), ('--tip', self.tip, '--timeout', 'inf')):
            rc, out, err = gate_py('wait-result', 'kit/x', *args, '-C', self.repo.root)
            self.assertEqual(rc, 2, (args, out, err))
            self.assertIn('USAGE:', out + err)


if __name__ == '__main__':
    unittest.main()
