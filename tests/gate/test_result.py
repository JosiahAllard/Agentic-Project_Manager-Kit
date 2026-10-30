"""kitlib.gate.result: the result records every branch gate and landing writes (running, then
final), and wait-result, the wait a second session arms on them (its one write: a running
record whose process died, finished error) (docs/gate.md "Result
record")."""
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.fs import treelock
from kitlib.gate import land, result, runner, steps, verdicts
from kitlib.proc import process
from kitlib.records import landing
from tests import gitfixture, testutil
from tests.gate import fixtures
from tests.home import machine_config, result_drive, temp_home

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')
FIELDS = ('schema', 'kind', 'branch', 'tip', 'base', 'run_id', 'started_at', 'finished_at',
          'verdict', 'code', 'log', 'summary', 'gate_ok_record', 'cleanup', 'source', 'pid',
          'ctime', 'gate_once', 'escalated')
UTC = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$'


class FakeClock(object):
    """time.monotonic and time.sleep for the `with` of patched(): a sleep is recorded and moves
    the clock on at once, so a retry loop's tries and pauses are counted exactly."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(time, 'monotonic', self.monotonic))
        stack.enter_context(mock.patch.object(time, 'sleep', self.sleep))
        return stack


def ledger_lines(root):
    """The land ledger's lines, each parsed; [] when there is no ledger."""
    try:
        with open(result.ledger_path(root), encoding='utf-8', newline='') as f:
            text = f.read()
    except FileNotFoundError:
        return []
    assert text.endswith('\n') and '\r' not in text, repr(text)
    return [json.loads(l) for l in text.splitlines()]


def block_dir(root):
    """A file where the result folder goes: every record write then fails."""
    folder = result.result_dir(root)
    os.makedirs(os.path.dirname(folder), exist_ok=True)
    with open(folder, 'w') as f:
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
        self.drive = result_drive(self)

    def rec(self, tip=None, verdict='pass', kind='gate', branch='kit/x'):
        return result.build(branch, tip or self.tip, None, kind,
                            G.OK if verdict == 'pass' else G.FAILED, log='L', summary='S',
                            verdict=verdict if verdict == 'running' else None)

    def test_write_is_atomic_and_read_back(self):
        path = result.write(self.repo.root, self.rec())
        self.assertEqual(os.path.dirname(path), result.result_dir(self.repo.root))
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))),        # no temp, its lock
                         ['.%s.lock' % os.path.basename(path), os.path.basename(path)])
        got, why = result.read(self.repo.root, 'kit/x')
        self.assertIsNone(why)
        self.assertEqual(sorted(got), sorted(FIELDS))
        self.assertEqual(got['tip'], self.tip)
        self.assertEqual(got['verdict'], 'pass')
        self.assertRegex(got['finished_at'], UTC)
        self.assertRegex(got['started_at'], UTC)
        self.assertEqual(result.read(self.repo.root, 'kit/none'), (None, 'none'))

    def test_the_ledger_appends_one_line_per_record_and_never_rewrites_one(self):
        a = result.build('kit/x', self.tip, None, 'land', G.FAILED, gate_once=True,
                         escalated=True)
        b = result.build('kit/x', self.tip, None, 'land', G.OK, gate_once=True, escalated=False)
        self.assertEqual((a['gate_once'], a['escalated']), (True, True))
        path, warning = result.append_ledger(self.repo.root, a)
        self.assertIsNone(warning)
        self.assertEqual(path, result.ledger_path(self.repo.root))
        self.assertEqual(os.path.dirname(path), result.result_dir(self.repo.root))
        with open(path, 'rb') as f:
            first = f.read()
        self.assertEqual(result.append_ledger(self.repo.root, b), (path, None))
        with open(path, 'rb') as f:
            self.assertTrue(f.read().startswith(first))        # appended, the first line kept
        got = ledger_lines(self.repo.root)
        self.assertEqual([list(l) for l in got], [list(result.LEDGER_FIELDS)] * 2)
        self.assertEqual([(l['verdict'], l['code'], l['gate_once'], l['escalated']) for l in got],
                         [('fail', G.FAILED, True, True), ('pass', G.OK, True, False)])
        self.assertEqual([l['run_id'] for l in got], [a['run_id'], b['run_id']])
        self.assertEqual(got[0]['tip'], self.tip)
        self.assertEqual(result.read(self.repo.root, 'kit/x', 'land'), (None, 'none'))

    def test_a_ledger_that_cannot_be_appended_is_a_warning(self):
        block_dir(self.repo.root)
        path, warning = result.append_ledger(
            self.repo.root, result.build('kit/x', self.tip, None, 'land', G.OK))
        self.assertIsNone(path)
        self.assertTrue(warning.startswith('WARNING: the land ledger line could not be appended: '),
                        warning)
        self.assertTrue(result.append_ledger(self.repo.root, None)[1].startswith('WARNING: '))

    def test_a_branch_gate_record_carries_null_gate_once_and_escalated(self):
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        run.start()
        self.assertEqual((run.last['gate_once'], run.last['escalated']), (None, None))
        got = result.read(self.repo.root, 'kit/x')[0]
        self.assertEqual((got['gate_once'], got['escalated']), (None, None))

    def test_branches_differing_only_in_case_keep_their_own_records(self):
        result.write(self.repo.root, self.rec(branch='kit/Foo'))
        result.write(self.repo.root, self.rec(branch='kit/foo', verdict='fail'))
        self.assertEqual(result.read(self.repo.root, 'kit/Foo')[0]['verdict'], 'pass')
        self.assertEqual(result.read(self.repo.root, 'kit/foo')[0]['verdict'], 'fail')

    def test_a_device_named_branch_is_written(self):
        result.write(self.repo.root, self.rec(branch='con'))
        self.assertEqual(result.read(self.repo.root, 'con')[0]['branch'], 'con')

    def test_a_replace_held_open_is_tried_five_times_0_2_s_apart_then_a_result_error(self):
        # The equivalence test of _put's move onto kitlib.fs.replace: the loop it replaced tried
        # RETRIES (5) times, sleeping RETRY_SLEEP (0.2 s) between, then raised the
        # PermissionError (a ResultError here). The clock is faked, so the count is exact.
        for fails, tries, written in ((2, 3, True), (4, 5, True), (5, 5, False), (9, 5, False)):
            with self.subTest(fails=fails):
                branch = 'kit/held%d' % fails
                real, calls, clock = os.replace, [], FakeClock()

                def flaky(a, b):
                    calls.append(b)
                    if len(calls) <= fails:
                        raise PermissionError('held open')
                    return real(a, b)
                with clock.patched(), mock.patch('os.replace', flaky):
                    if written:
                        result.write(self.repo.root, self.rec(branch=branch))
                    else:
                        with self.assertRaises(result.ResultError) as cm:
                            result.write(self.repo.root, self.rec(branch=branch))
                self.assertEqual(len(calls), tries)
                self.assertEqual(clock.sleeps, [0.2] * (tries - 1))
                if written:
                    self.assertEqual(result.read(self.repo.root, branch)[0]['verdict'], 'pass')
                else:
                    self.assertIn('held open', str(cm.exception))
                    self.assertFalse(os.path.exists(result.result_path(self.repo.root, branch)))
                self.assertEqual([n for n in os.listdir(result.result_dir(self.repo.root))
                                  if n.endswith('.tmp')], [])

    def test_a_replace_refused_otherwise_is_not_retried(self):
        calls, clock = [], FakeClock()

        def refused(a, b):
            calls.append(b)
            raise OSError('disk said no')
        with clock.patched(), mock.patch('os.replace', refused):
            with self.assertRaises(result.ResultError):
                result.write(self.repo.root, self.rec())
        self.assertEqual((len(calls), clock.sleeps), (1, []))

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

    def test_finish_dead_judges_a_record_written_after_its_read_never_overwrites_it(self):
        """A new run starts the record after finish_dead read the dead run's and before it
        replaced it: the record's lock holds the new run's write until finish_dead's is done, so
        the new run's record is the one left."""
        dead = result.Run(self.repo.root, 'land', 'kit/x', self.tip)
        dead.pid = self.dead_pid()
        dead.start()
        rec = result.read(self.repo.root, 'kit/x', 'land')[0]
        newer = result.Run(self.repo.root, 'land', 'kit/x', self.tip)
        main, started, read = threading.current_thread(), [], result.read_path

        def read_then_start(path):          # finish_dead's read: then the new run starts
            got = read(path)
            if not started and threading.current_thread() is main:
                t = threading.Thread(target=newer.start)
                started.append(t)
                t.start()
                t.join(1.0)                 # it writes now, or waits on the record's lock
            return got
        with mock.patch.object(result, 'read_path', read_then_start):
            result.finish_dead(self.repo.root, rec)
        self.assertEqual(len(started), 1)
        started[0].join(30)
        self.assertFalse(started[0].is_alive())
        got = result.read(self.repo.root, 'kit/x', 'land')[0]
        self.assertEqual((got['run_id'], got['verdict']), (newer.run_id, 'running'))

    def test_a_write_whose_condition_fails_leaves_the_record(self):
        result.write(self.repo.root, self.rec(verdict='running'))
        seen = []

        def when(now):
            seen.append(now['verdict'])
            return False
        self.assertIsNone(result.write(self.repo.root, self.rec(), when=when))
        self.assertEqual(seen, ['running'])
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'running')
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        self.assertEqual(run.start(when=lambda now: False),
                         (None, 'result record of gate kit/x left as it is: it changed before '
                                'this write'))
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'running')
        self.assertEqual(result.write(self.repo.root, self.rec(), when=lambda now: True),
                         result.result_path(self.repo.root, 'kit/x'))
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'pass')

    def test_a_record_whose_lock_stays_held_is_not_written(self):
        path = result.write(self.repo.root, self.rec())
        held = treelock.TreeLock(result.lock_path(path), what='test').acquire()
        try:
            with mock.patch.object(result, 'LOCK_WAIT_S', 0.1):
                with self.assertRaises(result.ResultError) as cm:
                    result.write(self.repo.root, self.rec(verdict='fail'))
                self.assertIn('held by pid %d' % os.getpid(), str(cm.exception))
                run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
                path2, line = run.start()
                self.assertIsNone(path2)
                self.assertTrue(line.startswith('WARNING: the result record could not be '
                                                'written: cannot write %s' % path), line)
        finally:
            held.release()
        self.assertEqual(result.read(self.repo.root, 'kit/x')[0]['verdict'], 'pass')

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


class Folder(unittest.TestCase):
    """The result folder is <dev_drive>/pm-kit/kit-gate-result/<repo key>, outside every checkout
    and one per repository; the old <git common dir>/kit-gate-result is read, never written; with
    no dev_drive there is no result folder (docs/gate.md "Result record")."""

    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('a.txt', 'a\n')
        self.tip = self.repo.commit('a', 'a.txt')
        self.old_dir = os.path.join(self.repo.common_dir(), 'kit-gate-result')

    def rec(self, branch='kit/x', kind='gate', **kw):
        return result.build(branch, self.tip, None, kind, kw.pop('code', G.OK), summary='S', **kw)

    def legacy(self, rec):
        """`rec` written where an older kit wrote it: the old folder."""
        path = os.path.join(self.old_dir, '%s.%s.json' % (result.file_stem(rec['branch']),
                                                         rec['kind']))
        os.makedirs(self.old_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        return path

    def same(self, a, b):
        self.assertEqual(os.path.normcase(os.path.normpath(a)), os.path.normcase(os.path.normpath(b)))

    def test_records_go_under_the_dev_drive_one_folder_per_repository(self):
        drive = result_drive(self)
        path = result.write(self.repo.root, self.rec())
        folder = os.path.dirname(path)
        self.same(os.path.dirname(folder), os.path.join(drive, 'pm-kit', 'kit-gate-result'))
        self.assertRegex(os.path.basename(folder), r'^[a-z0-9_-]{1,40}-[0-9a-f]{12}$')
        wt = self.repo.worktree('other')
        self.same(result.result_dir(wt), folder)                # every worktree: the one folder
        self.assertEqual(result.read(wt, 'kit/x')[0]['run_id'],
                         result.read(self.repo.root, 'kit/x')[0]['run_id'])
        other = gitfixture.TempRepo()
        self.addCleanup(other.cleanup)
        self.same(os.path.dirname(result.result_dir(other.root)), os.path.dirname(folder))
        self.assertNotEqual(os.path.normcase(result.result_dir(other.root)),
                            os.path.normcase(folder))           # another repository: its own
        self.assertFalse(os.path.exists(self.old_dir))           # nothing in the old folder

    def test_a_repo_key_is_the_folder_name_and_a_hash_of_the_common_dir(self):
        a = result.repo_key(r'C:\work\My.Repo\.git')
        self.assertRegex(a, r'^myrepo-[0-9a-f]{12}$')
        self.assertEqual(result.repo_key(r'c:\WORK\my.repo\.GIT'), a)   # Windows ignores case
        self.assertNotEqual(result.repo_key(r'C:\other\My.Repo\.git'), a)
        self.assertRegex(result.repo_key(r'C:\remotes\proj.git'), r'^projgit-[0-9a-f]{12}$')
        self.assertRegex(result.repo_key(r'C:\x\%%%\.git'), r'^repo-[0-9a-f]{12}$')
        self.assertLessEqual(len(result.repo_key('C:\\x\\%s\\.git' % ('n' * 200))), 40 + 13)

    def test_with_no_dev_drive_there_is_no_result_folder_and_nothing_is_written(self):
        temp_home(self)
        machine_config(None, {}, case=self)                      # a machine config, no dev_drive
        with self.assertRaises(result.ResultError) as cm:
            result.result_dir(self.repo.root)
        self.assertIn('dev_drive', str(cm.exception))
        run = result.Run(self.repo.root, 'gate', 'kit/x', self.tip)
        path, line = run.start()
        self.assertIsNone(path)
        self.assertTrue(line.startswith('WARNING: the result record could not be written: '),
                        line)
        self.assertIn('dev_drive', line)
        self.assertFalse(os.path.exists(self.old_dir))
        rec, why = result.read(self.repo.root, 'kit/x')
        self.assertIsNone(rec)
        self.assertIn('dev_drive', why)
        with self.assertRaises(result.ResultError):
            result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=0)
        self.assertEqual(result.running_records(self.repo.root, 'gate', run.run_id), [])

    def test_a_machine_config_that_cannot_be_read_is_no_result_folder(self):
        temp_home(self)
        machine_config(None, '{"not": json', case=self)
        with self.assertRaises(result.ResultError) as cm:
            result.result_dir(self.repo.root)
        self.assertIn('machine config cannot be read', str(cm.exception))

    def test_an_old_folder_record_is_read_when_the_new_folder_has_none(self):
        result_drive(self)
        old = self.rec()
        self.legacy(old)
        self.assertEqual(result.read(self.repo.root, 'kit/x'), (old, None))
        self.assertEqual(result.read(self.repo.root, 'kit/x', legacy=False), (None, 'none'))
        self.assertEqual(result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=0), (old, None))
        self.assertEqual(result.read(self.repo.root, 'kit/x', 'land'), (None, 'none'))

    def test_the_new_record_wins_unless_the_old_one_started_strictly_later(self):
        result_drive(self)
        new = self.rec(started_at='2026-10-01T10:00:00Z')
        result.write(self.repo.root, new)
        for stamp, later in (('2026-10-01T09:59:59Z', False), ('2026-10-01T10:00:00Z', False),
                             ('not a time', False), (None, False),
                             ('2026-10-01T10:00:01Z', True)):
            with self.subTest(stamp=stamp):
                old = self.rec(code=G.FAILED)
                old['started_at'] = stamp
                self.legacy(old)
                want = old if later else new
                self.assertEqual(result.read(self.repo.root, 'kit/x'), (want, None))
                self.assertEqual(result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=0),
                                 (want, None))
                self.assertEqual(result.read(self.repo.root, 'kit/x', legacy=False), (new, None))
        unstamped = dict(new, started_at='not a time')          # the new one's own stamp unread
        result.write(self.repo.root, unstamped)
        self.assertEqual(result.read(self.repo.root, 'kit/x'), (unstamped, None))

    def test_a_new_record_that_cannot_be_read_is_never_covered_by_an_old_one(self):
        result_drive(self)
        self.legacy(self.rec())
        path = result.result_path(self.repo.root, 'kit/x')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"verdict": "pa')                           # cut short
        rec, why = result.read(self.repo.root, 'kit/x')
        self.assertIsNone(rec)
        self.assertTrue(why.startswith('cannot read %s' % path), why)
        self.assertEqual(result.wait(self.repo.root, 'kit/x', self.tip, timeout_s=0),
                         (None, 'timeout'))

    def test_wait_finishes_a_dead_old_folder_record_where_it_is(self):
        result_drive(self)
        p = testutil.dead_process()
        self.addCleanup(p.wait)
        old = self.rec(kind='land', code=None, verdict='running', pid=p.pid, log='L')
        path = self.legacy(old)
        got, why = result.wait(self.repo.root, 'kit/x', self.tip, kind='land', timeout_s=0)
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['run_id'], got['log']), ('error', old['run_id'], 'L'))
        self.assertEqual(result.read_path(path), (got, None))    # finished in the old folder
        self.assertTrue(os.path.exists(result.lock_path(path)))  # under that file's own lock
        self.assertFalse(os.path.exists(result.result_dir(self.repo.root)))

    def test_the_readers_of_every_record_list_the_new_folder_then_the_old(self):
        result_drive(self)
        dirs = result.record_dirs(self.repo.root)
        self.assertEqual(len(dirs), 2)
        self.same(dirs[0], result.result_dir(self.repo.root))
        self.same(dirs[1], self.old_dir)

    def test_running_records_are_the_new_folders_only(self):
        result_drive(self)
        rid = 'ab' * 8
        self.legacy(self.rec(kind='land', code=None, verdict='running', run_id=rid))
        mine = self.rec(branch='kit/y', kind='land', code=None, verdict='running', run_id=rid)
        result.write(self.repo.root, mine)
        self.assertEqual(result.running_records(self.repo.root, 'land', rid), [mine])


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
        fixtures.gate_full(self.repo.root, *(expect_tip if isinstance(expect_tip, list)
                                             else [expect_tip]))
        fixtures.queue_review(self.repo.root, branch, expect_tip)
        return land.Lander(self.repo.root, branch, expect_main, expect_tip, **kw)

    def test_a_landing_writes_running_then_a_land_record(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        seen = []

        def merged_gate(lander):
            # In the landing's own slot, as Lander.gate runs it: a second booking waits for a
            # second fake slot, and under the gate's machine-lock run confine(1) may leave one.
            seen.append(result.read(self.repo.root, 'kit/feature', 'land')[0])
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      slug=lander.branch, log_dir=lander.log_dir,
                                      book_kw=lander.book_kw, slot=lander.slot)
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

    def test_a_landing_appends_its_outcome_to_the_ledger(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander('kit/feature', self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        rec = result.read(self.repo.root, 'kit/feature', 'land')[0]
        self.assertEqual((rec['gate_once'], rec['escalated']), (False, False))
        line, = ledger_lines(self.repo.root)
        self.assertEqual(line, dict((k, rec[k]) for k in result.LEDGER_FIELDS))
        self.assertEqual((line['branch'], line['tip'], line['verdict'], line['code']),
                         ('kit/feature', tip, 'pass', G.OK))
        self.assertRegex(line['finished_at'], UTC)

    def test_a_red_landing_and_its_green_retry_each_leave_a_ledger_line(self):
        wt, red = self.branch('feature', {'RED': 'x\n'})
        out = self.lander('kit/feature', self.main, red).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        os.remove(os.path.join(wt, 'RED'))
        green = self.repo.commit('fix', 'RED', cwd=wt)
        os.remove(verdicts.entry_path(self.repo.common_dir(), 'kit/feature'))   # reviewed again
        out = self.lander('kit/feature', self.main, green).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(result.read(self.repo.root, 'kit/feature', 'land')[0]['verdict'], 'pass')
        got = ledger_lines(self.repo.root)      # the land record says pass; the ledger keeps both
        self.assertEqual([(l['tip'], l['verdict'], l['code']) for l in got],
                         [(red, 'fail', G.FAILED), (green, 'pass', G.OK)])
        self.assertNotEqual(got[0]['run_id'], got[1]['run_id'])

    def test_a_landing_refused_before_its_checks_appends_nothing(self):
        self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander('kit/feature', self.main, 'c' * 40).run()    # wrong --expect-tip
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(ledger_lines(self.repo.root), [])

    def test_a_ledger_that_cannot_be_appended_leaves_the_landing_alone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        blocker = os.path.join(self.tmp, 'not-a-folder')
        with open(blocker, 'w') as f:
            f.write('x\n')
        said = []
        with mock.patch.object(result, 'ledger_path',
                               return_value=os.path.join(blocker, result.LEDGER)):
            out = self.lander('kit/feature', self.main, tip, say=said.append).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.lines[-1].startswith('LANDED '), out.lines)
        self.assertTrue(os.path.isfile(out.result_record))
        self.assertEqual(result.read(self.repo.root, 'kit/feature', 'land')[0]['verdict'], 'pass')
        self.assertTrue(any(l.startswith('WARNING: the land ledger line could not be appended')
                            for l in said), said)

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
        held = lock.take(['kit/someone'], pid=os.getpid())
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

    def test_a_landing_with_no_result_folder_is_refused_and_lands_nothing(self):
        """No dev_drive in the machine config: no folder for the land records the waiting
        session reads, so the landing refuses before it joins the queue, writing nothing."""
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        lander = self.lander('kit/feature', self.main, tip)
        self._kit_dev_drive = None
        machine_config(None, {}, case=self)
        out = lander.run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertEqual(len(out.lines), 1, out.lines)
        self.assertTrue(out.lines[0].startswith(
            'REFUSED: no result folder for the land records: the machine config sets no '
            'dev_drive'), out.lines)
        self.assertTrue(out.lines[0].endswith('; nothing was changed'), out.lines)
        self.assertIsNone(out.result_record)
        self.assertEqual(self.repo.git('rev-parse', 'refs/remotes/origin/main'), self.main)
        self.assertFalse(os.path.exists(result.legacy_dir(self.repo.root)))
        self.assertEqual(self.book_calls, [])

    def test_an_exception_after_the_checks_writes_error(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def merged_gate(_lander):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.lander('kit/feature', self.main, tip, merged_gate=merged_gate).run()
        rec = result.read(self.repo.root, 'kit/feature', 'land')[0]
        self.assertEqual((rec['verdict'], rec['tip']), ('error', tip))
        line, = ledger_lines(self.repo.root)
        self.assertEqual((line['verdict'], line['code'], line['run_id']),
                         ('error', None, rec['run_id']))


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
        result_drive(self)              # before gate_py's test_env: the tool reads it there

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
