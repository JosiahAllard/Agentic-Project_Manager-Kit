"""kitlib.records.landing: the landing lock against real throwaway repositories."""
import json
import os
import threading
import unittest
from unittest import mock

from kitlib import config
from kitlib.records import landing
from tests import gitfixture
from tests.config.helpers import TempTree

ALIVE = lambda pid, start: True      # noqa: E731
DEAD = lambda pid, start: False      # noqa: E731


class LockCase(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)

    def lock(self, root=None, **kw):
        kw.setdefault('start_of', lambda pid: 1234)
        kw.setdefault('is_alive', ALIVE)
        kw.setdefault('lanes', ('gamma',))
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
        rec = self.lock().take('full', ['kit/a'], pid=42)
        with open(self.lock().path, encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertEqual(rec, on_disk)
        main = self.repo.git('rev-parse', 'main')
        self.assertEqual((rec['lane'], rec['branches'], rec['pid'], rec['pid_start'], rec['phase']),
                         ('full', ['kit/a'], 42, 1234, 'merge'))
        self.assertEqual((rec['origin_at_take'], rec['main_at_take'], rec['main_branch']), (main, main, 'main'))
        self.assertRegex(rec['token'], r'^[0-9a-f]{16}$')
        self.assertRegex(rec['started_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_a_token_holder_records_no_process(self):
        rec = self.lock().take('full', ['kit/a'])
        self.assertIsNone(rec['pid'])
        self.assertIsNone(rec['pid_start'])

    def test_a_second_take_from_any_checkout_is_busy_and_names_the_holder(self):
        self.lock().take('gamma', ['kit/a'], pid=42)
        with self.assertRaises(landing.Busy) as cm:
            self.lock(self.repo.worktree('b')).take('full', ['kit/b'])
        self.assertEqual(cm.exception.record['lane'], 'gamma')
        self.assertIn('lane gamma, branches kit/a, pid 42', str(cm.exception))

    def test_of_many_racing_takers_exactly_one_wins(self):
        locks = [self.lock() for _ in range(8)]
        gate = threading.Barrier(len(locks))
        won, busy = [], []

        def run(lk):
            gate.wait()
            try:
                won.append(lk.take('full', ['kit/x']))
            except landing.Busy:
                busy.append(1)

        threads = [threading.Thread(target=run, args=(lk,)) for lk in locks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual((len(won), len(busy)), (1, len(locks) - 1))

    def test_an_undeclared_lane_or_no_branch_refuses(self):
        with self.assertRaises(landing.LockError):
            self.lock().take('pvp', ['kit/a'])
        with self.assertRaises(landing.LockError):
            self.lock().take('full', [])
        self.assertFalse(os.path.exists(self.lock().path))

    def test_release_needs_the_holders_token(self):
        lk = self.lock()
        rec = lk.take('full', ['kit/a'])
        for bad in ('0' * 16, '', None):
            with self.assertRaises(landing.NotHolder):
                lk.release(bad)
        self.assertEqual(lk.release(rec['token'])['token'], rec['token'])
        self.assertIsNone(lk.read())
        with self.assertRaises(landing.NotHolder):
            lk.release(rec['token'])

    def test_set_phase_needs_the_token_and_a_known_phase(self):
        lk = self.lock()
        rec = lk.take('full', ['kit/a'])
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
        rec = lk.take('full', ['kit/a'])
        with mock.patch.object(landing.json, 'dumps', side_effect=ValueError('cannot encode')):
            with self.assertRaises(ValueError):
                lk.set_phase(rec['token'], 'gate')
        self.assertEqual(self.tmp_files(), [])
        self.assertEqual(lk.read()['phase'], 'merge')

    def test_a_holder_change_before_the_replace_refuses_the_phase(self):
        lk = self.lock()
        rec = lk.take('full', ['kit/a'])
        real_dumps, other = json.dumps, {}

        def swap_holder(*a, **kw):        # the first call is set_phase writing its tmp
            if not other:
                other['token'] = None
                lk.release(rec['token'])
                other.update(lk.take('full', ['kit/b']))
            return real_dumps(*a, **kw)
        with mock.patch.object(landing.json, 'dumps', side_effect=swap_holder):
            with self.assertRaises(landing.NotHolder):
                lk.set_phase(rec['token'], 'push')
        now = lk.read()
        self.assertEqual((now['token'], now['branches'], now['phase']), (other['token'], ['kit/b'], 'merge'))
        self.assertEqual(self.tmp_files(), [])

    def test_a_lock_that_is_not_a_record_is_held_by_nobody_we_can_name(self):
        for text in ('', '{', '[]', '{"lane": "full"}', '{"lane": "pvp", "token": "t"}'):
            self.write_raw(text)
            with self.assertRaises(landing.LockError):
                self.lock().read()
            with self.assertRaises(landing.Busy):
                self.lock().take('full', ['kit/a'])

    def test_a_take_whose_write_fails_leaves_no_lock(self):
        lk = self.lock()
        with mock.patch.object(landing.os, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(landing.LockError) as cm:
                lk.take('full', ['kit/a'])
        self.assertIn('disk full', str(cm.exception))
        self.assertFalse(os.path.exists(lk.path))
        self.assertIsNotNone(lk.take('full', ['kit/a']))

    def test_a_permission_error_is_retried_then_refused(self):
        lk = self.lock()
        rec = lk.take('full', ['kit/a'])
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
        rec = lk.take('full', ['kit/a'])
        with mock.patch.object(landing.time, 'sleep'), \
                mock.patch.object(landing.os, 'remove', side_effect=PermissionError('held open')):
            with self.assertRaises(landing.LockError) as cm:
                lk.release(rec['token'])
        self.assertIn('failed %d times' % landing.RETRIES, str(cm.exception))
        self.assertEqual(lk.read()['token'], rec['token'])


class BreakTest(LockCase):

    def test_a_free_lock_has_nothing_to_break(self):
        self.assertEqual(self.lock().break_lock(True), (False, 'nobody holds the landing lock: nothing to break'))

    def test_a_live_holder_is_never_broken_however_old(self):
        self.lock().take('full', ['kit/a'], pid=42, now=landing.datetime.datetime(2001, 1, 1))
        ok, why = self.lock().break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('the holder is alive', why)

    def test_the_recorded_start_time_is_what_liveness_is_asked_about(self):
        self.lock().take('full', ['kit/a'], pid=42)
        seen = []
        lk = self.lock(is_alive=lambda pid, start: seen.append((pid, start)) or False)
        self.assertTrue(lk.break_lock()[0])
        self.assertEqual(seen, [(42, 1234)])

    def test_a_dead_holder_with_nothing_unpushed_is_broken(self):
        rec = self.lock().take('full', ['kit/a'], pid=42)
        ok, removed = self.lock(is_alive=DEAD).break_lock()
        self.assertTrue(ok)
        self.assertEqual(removed['token'], rec['token'])
        self.assertIsNone(self.lock().read())

    def test_a_dead_holder_with_an_unpushed_commit_is_not_broken(self):
        self.lock().take('full', ['kit/a'], pid=42)
        self.repo.write('x.txt', 'x\n')
        self.repo.commit('unpushed', 'x.txt')
        ok, why = self.lock(is_alive=DEAD).break_lock()
        self.assertFalse(ok)
        self.assertIn('an unpushed merge or commit', why)
        self.assertIsNotNone(self.lock().read())

    def test_a_merge_in_progress_in_the_main_checkout_blocks_a_break_from_a_worktree(self):
        wt = self.repo.worktree('a')
        self.lock().take('full', ['kit/a'], pid=42)
        with open(os.path.join(self.repo.common_dir(), 'MERGE_HEAD'), 'w') as f:
            f.write(self.repo.initial + '\n')
        ok, why = self.lock(wt, is_alive=DEAD).break_lock()
        self.assertFalse(ok)
        self.assertIn('MERGE_HEAD', why)

    def test_a_token_holder_is_broken_only_when_declared_gone(self):
        self.lock().take('full', ['kit/a'])
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
        self.lock().take('full', ['kit/a'], pid=42)
        holder = self.lock()

        def gone_and_replaced(pid, start):
            path = holder.path
            os.remove(path)
            self.lock().take('full', ['kit/b'], pid=43)
            return False

        ok, why = self.lock(is_alive=gone_and_replaced).break_lock()
        self.assertFalse(ok)
        self.assertIn('changed while it was judged', why)
        self.assertEqual(self.lock().read()['branches'], ['kit/b'])

    def test_an_undeclared_lane_still_names_a_process_that_is_checked(self):
        self.write_raw(json.dumps({'lane': 'pvp', 'token': 't', 'pid': 42, 'pid_start': 7}))
        ok, why = self.lock().break_lock(holder_gone=True)
        self.assertFalse(ok)
        self.assertIn('alive', why)
        self.write_raw(json.dumps({'lane': 'full', 'token': 't', 'pid': 'x'}))
        self.assertFalse(self.lock(is_alive=DEAD).break_lock(holder_gone=True)[0])


class GateRefusalTest(LockCase):

    def test_a_merged_gate_needs_the_lock_for_its_lane_with_origin_unmoved(self):
        lk = self.lock()
        self.assertIn('nobody holds', lk.gate_refusal('full'))
        rec = lk.take('gamma', ['kit/a'])
        self.assertIn('held for lane gamma', lk.gate_refusal('full'))
        self.assertIsNone(lk.gate_refusal('gamma'))
        lk.set_phase(rec['token'], 'stuck')
        self.assertIn('stuck', lk.gate_refusal('gamma'))
        lk.set_phase(rec['token'], 'gate')
        self.repo.write('y.txt', 'y\n')
        self.repo.commit('moved', 'y.txt')
        self.repo.git('push', '-q', 'origin', 'main')
        self.assertIn('moved under this landing', lk.gate_refusal('gamma'))


class ConfigAndProcTest(unittest.TestCase):

    def cfg(self, kit):
        t = TempTree(kit)
        self.addCleanup(t.cleanup)
        return config.load(t.root)

    def test_lock_name_honours_a_legacy_git_lock_file(self):
        self.assertEqual(landing.lock_name(None), 'kit-landing.lock')
        self.assertEqual(landing.lock_name(self.cfg({})), 'kit-landing.lock')
        self.assertEqual(landing.lock_name(self.cfg({'landing': {'lock_file': 'git:alpha-landing.lock'}})),
                         'alpha-landing.lock')
        with self.assertRaises(landing.LockError):
            landing.lock_name(self.cfg({'landing': {'lock_file': 'git:../x.lock'}}))

    def test_a_lock_file_that_is_not_git_name_refuses(self):
        for value in ('tools/landing.lock', 'alpha-landing.lock', 'GIT:x.lock'):
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigError) as cm:
                    landing.lock_name(self.cfg({'landing': {'lock_file': value}}))
                self.assertIn('must be git:<name>', str(cm.exception))

    def test_for_config_takes_main_remote_and_name_from_the_config(self):
        repo = gitfixture.TempRepo(branch='master', bare_remote=True)
        self.addCleanup(repo.cleanup)
        repo.write('.kit.json', json.dumps({'repo': {'main_branch': 'master'},
                                            'landing': {'lock_file': 'git:alpha-landing.lock'}}))
        lk = landing.LandingLock.for_config(repo.root, config.load(repo.root), lanes=('gamma',),
                                            start_of=lambda pid: None, is_alive=ALIVE)
        self.assertEqual(os.path.basename(lk.path), 'alpha-landing.lock')
        rec = lk.take('gamma', ['kit/a'])
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
