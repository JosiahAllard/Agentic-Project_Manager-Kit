"""kitlib.records.landing: the landing lock against real throwaway repositories."""
import contextlib
import json
import os
import threading
import time
import unittest
from unittest import mock

from kitlib import config
from kitlib.config import schema
from kitlib.records import landing
from tests import gitfixture
from tests.config.helpers import TempTree

ALIVE = lambda pid, start: True      # noqa: E731
DEAD = lambda pid, start: False      # noqa: E731


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


class LockCase(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)

    def lock(self, root=None, **kw):
        kw.setdefault('start_of', lambda pid: 1234)
        kw.setdefault('is_alive', ALIVE)
        return landing.LandingLock(root or self.repo.root, **kw)

    def write_raw(self, text):
        with open(os.path.join(self.repo.common_dir(), landing.LOCK_NAME), 'w', encoding='utf-8') as f:
            f.write(text)


class TakeReleaseTest(LockCase):

    def test_the_lock_lives_in_the_common_dir_for_every_checkout(self):
        wt = self.repo.worktree('a')
        here, there = self.lock(), self.lock(wt)
        self.assertEqual(os.path.normcase(here.path), os.path.normcase(there.path))
        self.assertTrue(os.path.samefile(os.path.dirname(here.path), self.repo.common_dir()))

    def test_take_writes_the_full_record(self):
        rec = self.lock().take(['kit/a'], pid=42)
        with open(self.lock().path, encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertEqual(rec, on_disk)
        main = self.repo.git('rev-parse', 'main')
        self.assertEqual((rec['lane'], rec['branches'], rec['pid'], rec['pid_start'], rec['phase']),
                         ('full', ['kit/a'], 42, 1234, 'merge'))
        self.assertEqual((rec['origin_at_take'], rec['main_at_take'], rec['main_branch']), (main, main, 'main'))
        self.assertRegex(rec['token'], r'^[0-9a-f]{16}$')
        self.assertRegex(rec['started_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_a_record_with_no_lane_or_another_lane_is_read_and_held(self):
        for extra in ({}, {'lane': 'pvp'}):
            with self.subTest(extra=extra):
                rec = dict({'token': 'f' * 16, 'branches': ['kit/old'], 'pid': None, 'phase': 'gate'}, **extra)
                self.write_raw(json.dumps(rec))
                self.assertEqual(self.lock().read()['branches'], ['kit/old'])
                with self.assertRaises(landing.Busy):
                    self.lock().take(['kit/a'])

    def test_a_token_holder_records_no_process(self):
        rec = self.lock().take(['kit/a'])
        self.assertIsNone(rec['pid'])
        self.assertIsNone(rec['pid_start'])

    def test_a_second_take_from_any_checkout_is_busy_and_names_the_holder(self):
        self.lock().take(['kit/a'], pid=42)
        with self.assertRaises(landing.Busy) as cm:
            self.lock(self.repo.worktree('b')).take(['kit/b'])
        self.assertEqual(cm.exception.record['branches'], ['kit/a'])
        self.assertIn('HELD - branches kit/a, pid 42', str(cm.exception))
        self.assertNotIn('lane', str(cm.exception))

    def test_of_many_racing_takers_exactly_one_wins(self):
        locks = [self.lock() for _ in range(8)]
        gate = threading.Barrier(len(locks))
        won, busy = [], []

        def run(lk):
            gate.wait()
            try:
                won.append(lk.take(['kit/x']))
            except landing.Busy:
                busy.append(1)

        threads = [threading.Thread(target=run, args=(lk,)) for lk in locks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual((len(won), len(busy)), (1, len(locks) - 1))

    def test_no_branch_refuses(self):
        with self.assertRaises(landing.LockError):
            self.lock().take([])
        self.assertFalse(os.path.exists(self.lock().path))

    def test_release_needs_the_holders_token(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        for bad in ('0' * 16, '', None):
            with self.assertRaises(landing.NotHolder):
                lk.release(bad)
        self.assertEqual(lk.release(rec['token'])['token'], rec['token'])
        self.assertIsNone(lk.read())
        with self.assertRaises(landing.NotHolder):
            lk.release(rec['token'])

    def test_set_phase_needs_the_token_and_a_known_phase(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        self.assertEqual(lk.set_phase(rec['token'], 'gate')['phase'], 'gate')
        self.assertEqual(lk.read()['phase'], 'gate')
        with self.assertRaises(landing.LockError):
            lk.set_phase(rec['token'], 'done')
        with self.assertRaises(landing.NotHolder):
            lk.set_phase('f' * 16, 'push')
        self.assertEqual([n for n in os.listdir(self.repo.common_dir()) if n.endswith('.tmp')], [])

    def tmp_files(self):
        return [n for n in os.listdir(self.repo.common_dir()) if n.endswith('.tmp')]

    def test_a_set_phase_whose_write_fails_leaves_no_tmp(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        with mock.patch.object(landing.json, 'dumps', side_effect=ValueError('cannot encode')):
            with self.assertRaises(ValueError):
                lk.set_phase(rec['token'], 'gate')
        self.assertEqual(self.tmp_files(), [])
        self.assertEqual(lk.read()['phase'], 'merge')

    def test_a_holder_change_before_the_replace_refuses_the_phase(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        real_dumps, other = json.dumps, {}

        def swap_holder(*a, **kw):        # the first call is set_phase writing its tmp
            if not other:
                other['token'] = None
                lk.release(rec['token'])
                other.update(lk.take(['kit/b']))
            return real_dumps(*a, **kw)
        with mock.patch.object(landing.json, 'dumps', side_effect=swap_holder):
            with self.assertRaises(landing.NotHolder):
                lk.set_phase(rec['token'], 'push')
        now = lk.read()
        self.assertEqual((now['token'], now['branches'], now['phase']), (other['token'], ['kit/b'], 'merge'))
        self.assertEqual(self.tmp_files(), [])

    def test_a_lock_that_is_not_a_record_is_held_by_nobody_we_can_name(self):
        for text in ('', '{', '[]', '{"lane": "full"}', '{"lane": "full", "token": ""}'):
            self.write_raw(text)
            with self.assertRaises(landing.LockError):
                self.lock().read()
            with self.assertRaises(landing.Busy):
                self.lock().take(['kit/a'])

    def test_a_take_whose_write_fails_leaves_no_lock(self):
        lk = self.lock()
        with mock.patch.object(landing.os, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(landing.LockError) as cm:
                lk.take(['kit/a'])
        self.assertIn('disk full', str(cm.exception))
        self.assertFalse(os.path.exists(lk.path))
        self.assertIsNotNone(lk.take(['kit/a']))

    def test_a_permission_error_is_retried_then_refused(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        real_remove = os.remove
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError('held open')
            real_remove(path)

        with mock.patch.object(landing.time, 'sleep'), mock.patch.object(landing.os, 'remove', flaky):
            lk.release(rec['token'])
        self.assertEqual(len(calls), 3)
        rec = lk.take(['kit/a'])
        with mock.patch.object(landing.time, 'sleep'), \
                mock.patch.object(landing.os, 'remove', side_effect=PermissionError('held open')):
            with self.assertRaises(landing.LockError) as cm:
                lk.release(rec['token'])
        self.assertIn('failed %d times' % landing.RETRIES, str(cm.exception))
        self.assertEqual(lk.read()['token'], rec['token'])

    def test_a_phase_replace_held_open_is_tried_five_times_0_2_s_apart_then_a_lock_error(self):
        # The equivalence test of set_phase's move onto kitlib.fs.replace: the loop it replaced
        # tried RETRIES (5) times, sleeping RETRY_SLEEP (0.2 s) between, then raised LockError
        # ('failed 5 times'), the phase unchanged. The clock is faked, so the count is exact.
        lk = self.lock()
        for fails, tries, written in ((2, 3, True), (4, 5, True), (5, 5, False), (9, 5, False)):
            with self.subTest(fails=fails):
                rec = lk.take(['kit/a'])
                real, calls, clock = os.replace, [], FakeClock()

                def flaky(a, b):
                    calls.append(b)
                    if len(calls) <= fails:
                        raise PermissionError('held open')
                    return real(a, b)
                with clock.patched(), mock.patch.object(landing.os, 'replace', flaky):
                    if written:
                        lk.set_phase(rec['token'], 'gate')
                    else:
                        with self.assertRaises(landing.LockError) as cm:
                            lk.set_phase(rec['token'], 'gate')
                self.assertEqual(len(calls), tries)
                self.assertEqual(clock.sleeps, [0.2] * (tries - 1))
                self.assertEqual(lk.read()['phase'], 'gate' if written else 'merge')
                if not written:
                    self.assertIn('failed 5 times (held open)', str(cm.exception))
                    self.assertNotIsInstance(cm.exception, landing.NotHolder)
                self.assertEqual(self.tmp_files(), [])
                lk.release(rec['token'])

    def test_the_phase_replace_goes_through_kitlib_fs_replace_with_the_old_bounds(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        real, seen = landing.fsreplace.replace, []

        def spy(src, dst, wait=None, pause=None):
            seen.append((os.path.normcase(dst), wait, pause))
            return real(src, dst, wait=wait, pause=pause)
        with mock.patch.object(landing.fsreplace, 'replace', spy):
            lk.set_phase(rec['token'], 'push')
        self.assertEqual(seen, [(os.path.normcase(lk.path), landing.REPLACE_WAIT, 0.2)])
        self.assertAlmostEqual(landing.REPLACE_WAIT, 0.7)
        self.assertEqual(lk.read()['phase'], 'push')

    def test_a_phase_replace_refused_otherwise_is_not_retried(self):
        lk = self.lock()
        rec = lk.take(['kit/a'])
        calls, clock = [], FakeClock()

        def refused(a, b):
            calls.append(b)
            raise OSError('disk said no')
        with clock.patched(), mock.patch.object(landing.os, 'replace', refused):
            with self.assertRaises(OSError) as cm:
                lk.set_phase(rec['token'], 'gate')
        self.assertNotIsInstance(cm.exception, landing.LockError)   # raised as it was, as before
        self.assertEqual((len(calls), clock.sleeps), (1, []))
        self.assertEqual(lk.read()['phase'], 'merge')
        self.assertEqual(self.tmp_files(), [])


class BreakTest(LockCase):

    def test_a_free_lock_has_nothing_to_break(self):
        self.assertEqual(self.lock().break_lock(True), (False, 'nobody holds the landing lock: nothing to break'))

    def test_a_live_holder_is_never_broken_however_old(self):
        self.lock().take(['kit/a'], pid=42, now=landing.datetime.datetime(2001, 1, 1))
        ok, why = self.lock().break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('the holder is alive', why)

    def test_the_recorded_start_time_is_what_liveness_is_asked_about(self):
        self.lock().take(['kit/a'], pid=42)
        seen = []
        lk = self.lock(is_alive=lambda pid, start: seen.append((pid, start)) or False)
        self.assertTrue(lk.break_lock()[0])
        self.assertEqual(seen, [(42, 1234)])

    def test_a_dead_holder_with_nothing_unpushed_is_broken(self):
        rec = self.lock().take(['kit/a'], pid=42)
        ok, removed = self.lock(is_alive=DEAD).break_lock()
        self.assertTrue(ok)
        self.assertEqual(removed['token'], rec['token'])
        self.assertIsNone(self.lock().read())

    def test_a_dead_holder_with_an_unpushed_commit_is_not_broken(self):
        self.lock().take(['kit/a'], pid=42)
        self.repo.write('x.txt', 'x\n')
        self.repo.commit('unpushed', 'x.txt')
        ok, why = self.lock(is_alive=DEAD).break_lock()
        self.assertFalse(ok)
        self.assertIn('an unpushed merge or commit', why)
        self.assertIsNotNone(self.lock().read())

    def test_a_merge_in_progress_in_the_main_checkout_blocks_a_break_from_a_worktree(self):
        wt = self.repo.worktree('a')
        self.lock().take(['kit/a'], pid=42)
        with open(os.path.join(self.repo.common_dir(), 'MERGE_HEAD'), 'w') as f:
            f.write(self.repo.initial + '\n')
        ok, why = self.lock(wt, is_alive=DEAD).break_lock()
        self.assertFalse(ok)
        self.assertIn('MERGE_HEAD', why)

    def test_a_token_holder_is_broken_only_when_declared_gone(self):
        self.lock().take(['kit/a'])
        ok, why = self.lock().break_lock()
        self.assertFalse(ok)
        self.assertIn('token holder', why)
        self.assertTrue(self.lock().break_lock(holder_gone=True)[0])

    def test_an_unparseable_lock_is_never_broken(self):
        # It was a token holder, broken on holder_gone=True; a taker
        # mid-write leaves such a file, so it now fails closed (tests/records/test_landing_break_live.py)
        self.write_raw('{')
        self.assertFalse(self.lock().break_lock()[0])
        self.assertFalse(self.lock().break_lock(holder_gone=True)[0])

    def test_a_lock_taken_again_while_it_is_judged_is_not_broken(self):
        self.lock().take(['kit/a'], pid=42)
        holder = self.lock()

        def gone_and_replaced(pid, start):
            path = holder.path
            os.remove(path)
            self.lock().take(['kit/b'], pid=43)
            return False

        ok, why = self.lock(is_alive=gone_and_replaced).break_lock()
        self.assertFalse(ok)
        self.assertIn('changed while it was judged', why)
        self.assertEqual(self.lock().read()['branches'], ['kit/b'])

    def test_an_older_kits_lane_is_ignored_and_its_process_still_checked(self):
        self.write_raw(json.dumps({'lane': 'pvp', 'token': 't', 'pid': 42, 'pid_start': 7}))
        ok, why = self.lock().break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('alive', why)
        self.write_raw(json.dumps({'lane': 'full', 'token': 't', 'pid': 'x'}))
        self.assertFalse(self.lock(is_alive=DEAD).break_lock(holder_gone=True)[0])


class GateRefusalTest(LockCase):

    def test_a_merged_gate_needs_the_lock_held_with_origin_unmoved(self):
        lk = self.lock()
        self.assertIn('nobody holds', lk.gate_refusal())
        rec = lk.take(['kit/a'])
        self.assertIsNone(lk.gate_refusal())
        lk.set_phase(rec['token'], 'stuck')
        self.assertIn('stuck', lk.gate_refusal())
        lk.set_phase(rec['token'], 'gate')
        self.repo.write('y.txt', 'y\n')
        self.repo.commit('moved', 'y.txt')
        self.repo.git('push', '-q', 'origin', 'main')
        self.assertIn('moved under this landing', lk.gate_refusal())


class ConfigAndProcTest(unittest.TestCase):

    def cfg(self, kit):
        t = TempTree(kit)
        self.addCleanup(t.cleanup)
        return config.load(t.root)

    def test_a_config_still_setting_the_retired_lock_file_loads_without_it(self):
        # `landing.lock_file` is retired (schema.RETIRED): any value, the old git:<name> form or
        # one that form refused, is dropped unchecked, and no lock name is read from the config
        for value in ('git:alpha-landing.lock', 'git:../x.lock', 'tools/landing.lock', 3):
            with self.subTest(value=value):
                cfg = self.cfg({'landing': {'batch_max': 2, 'lock_file': value}})
                self.assertEqual(cfg.section('landing')['batch_max'], 2)
                self.assertNotIn('lock_file', cfg.section('landing'))
        self.assertIn('lock_file', schema.RETIRED['landing'])
        self.assertNotIn('lock_file', schema.SECTIONS['landing'])
        self.assertFalse(hasattr(landing, 'lock_name'))

    def test_for_config_takes_main_and_remote_from_the_config_and_the_kit_lock_name(self):
        repo = gitfixture.TempRepo(branch='master', bare_remote=True)
        self.addCleanup(repo.cleanup)
        repo.write('.kit.json', json.dumps({'repo': {'main_branch': 'master'},
                                            'landing': {'lock_file': 'git:alpha-landing.lock'}}))
        lk = landing.LandingLock.for_config(repo.root, config.load(repo.root),
                                            start_of=lambda pid: None, is_alive=ALIVE)
        self.assertEqual(os.path.basename(lk.path), landing.LOCK_NAME)
        self.assertEqual(landing.LOCK_NAME, 'kit-landing.lock')
        with self.assertRaises(TypeError):          # no option names another lock file
            landing.LandingLock(repo.root, name='other.lock')
        rec = lk.take(['kit/a'])
        self.assertEqual(rec['main_at_take'], repo.git('rev-parse', 'master'))
        self.assertEqual(rec['origin_at_take'], rec['main_at_take'])

    def test_without_kitlib_proc_a_process_holder_is_always_alive(self):
        with mock.patch.object(landing, '_proc', return_value=None):
            self.assertIsNone(landing.process_start(os.getpid()))
            self.assertTrue(landing.process_alive(42, 7))

    def test_kitlib_proc_answers_when_present_and_its_failures_count_as_alive(self):
        class FakeProc(object):
            @staticmethod
            def proc_ctime(pid):
                return 99

            @staticmethod
            def alive(pid, start):
                return start == 99

        with mock.patch.object(landing, '_proc', return_value=FakeProc):
            self.assertEqual(landing.process_start(1), 99)
            self.assertTrue(landing.process_alive(1, 99))
            self.assertFalse(landing.process_alive(1, 98))

        class Broken(object):
            @staticmethod
            def alive(pid, start):
                raise OSError('boom')

        with mock.patch.object(landing, '_proc', return_value=Broken):
            self.assertTrue(landing.process_alive(1, 2))
            self.assertIsNone(landing.process_start(1))


if __name__ == '__main__':
    unittest.main()
