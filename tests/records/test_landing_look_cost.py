"""kitlib.records.landing: a take that finds the lock held asks git nothing and looks up no process;
a take that wins writes the same record as before."""
import json
import os
import unittest
from unittest import mock

from kitlib.records import landing
from tests import gitfixture

ALIVE = lambda pid, start: True      # noqa: E731
HELD = {'lane': 'full', 'token': 'feedfacefeedface', 'branches': ['kit/held'], 'pid': 7,
        'pid_start': 70, 'phase': 'gate', 'main_branch': 'main'}


class LookCostCase(unittest.TestCase):

    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.starts, self.resolves = [], []
        real = landing.refs.resolve

        def counting(*a, **kw):
            self.resolves.append(a[0])
            return real(*a, **kw)

        patcher = mock.patch.object(landing.refs, 'resolve', counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_of(self, pid):
        self.starts.append(pid)
        return 1234

    def lock(self):
        return landing.LandingLock(self.repo.root, start_of=self.start_of, is_alive=ALIVE)

    def write_raw(self, text):
        with open(self.lock().path, 'w', encoding='utf-8') as f:
            f.write(text)


class ALosingLookTest(LookCostCase):

    def test_a_held_lock_is_busy_with_no_git_call_and_no_process_lookup(self):
        self.write_raw(json.dumps(HELD))
        with self.assertRaises(landing.Busy) as got:
            self.lock().take('full', ['kit/a'], pid=42)
        self.assertEqual(got.exception.record, HELD)
        self.assertEqual((self.starts, self.resolves), ([], []))

    def test_an_unreadable_lock_is_busy_by_nobody_named_with_no_git_call(self):
        self.write_raw('')          # a taker between its create and its write
        with self.assertRaises(landing.Busy) as got:
            self.lock().take('full', ['kit/a'], pid=42)
        self.assertEqual(got.exception.record['lane'], '?')
        self.assertEqual((self.starts, self.resolves), ([], []))

    def test_a_lock_released_between_the_look_and_the_read_is_busy_released(self):
        lk = self.lock()
        self.write_raw(json.dumps(HELD))
        real_read = lk.read

        def released_first():
            os.remove(lk.path)
            return real_read()

        with mock.patch.object(lk, 'read', released_first):
            with self.assertRaises(landing.Busy) as got:
                lk.take('full', ['kit/a'], pid=42)
        self.assertEqual(got.exception.record['phase'], 'released while we looked')
        self.assertFalse(os.path.exists(lk.path))

    def test_a_lock_created_after_the_look_still_loses_at_the_exclusive_create(self):
        self.write_raw(json.dumps(HELD))
        with mock.patch.object(landing.os.path, 'exists', lambda p: False):
            with self.assertRaises(landing.Busy) as got:
                self.lock().take('full', ['kit/a'], pid=42)
        self.assertEqual(got.exception.record, HELD)
        with open(self.lock().path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), HELD)        # the holder's record is untouched


class AWinningTakeTest(LookCostCase):

    def test_the_record_is_built_once_and_holds_what_it_held(self):
        rec = self.lock().take('full', ['kit/a'], pid=42)
        main = self.repo.git('rev-parse', 'main')
        with open(self.lock().path, encoding='utf-8') as f:
            self.assertEqual(json.load(f), rec)
        self.assertEqual(sorted(rec), ['branches', 'lane', 'main_at_take', 'main_branch',
                                       'origin_at_take', 'phase', 'pid', 'pid_start',
                                       'started_utc', 'token'])
        self.assertEqual(
            {k: rec[k] for k in rec if k not in ('token', 'started_utc')},
            {'lane': 'full', 'branches': ['kit/a'], 'pid': 42, 'pid_start': 1234,
             'main_branch': 'main', 'origin_at_take': main, 'main_at_take': main, 'phase': 'merge'})
        self.assertEqual(self.starts, [42])
        self.assertEqual(self.resolves, ['refs/remotes/origin/main', 'refs/heads/main'])

    def test_a_take_after_a_release_reads_main_where_it_is_now(self):
        lk = self.lock()
        before = lk.take('full', ['kit/a'], pid=42)
        lk.release(before['token'])
        self.repo.write('moved.txt', 'x\n')
        main = self.repo.commit('main moves while nobody holds the lock', 'moved.txt')
        rec = lk.take('full', ['kit/b'], pid=43)
        self.assertNotEqual(before['main_at_take'], main)
        self.assertEqual(rec['main_at_take'], main)
        self.assertEqual(self.starts, [42, 43])


if __name__ == '__main__':
    unittest.main()
