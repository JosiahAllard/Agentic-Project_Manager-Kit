"""One daily full run at a time per tree (kitlib.dailyfull.treelock), in a scratch repository with
a bare remote, booking on the scratch lock store. Holders are real: a second run in a thread while
the first is mid-gate, and child processes that hold the lock, exit holding it, or are killed."""
import errno
import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from kitlib import dailyfull as D
from kitlib import gate as G
from kitlib.gate import runner
from tests.dailyfull.test_dailyfull import FAILING, KIT, Case

SIGNALS = 'KIT_TEST_DAILY_SIGNALS'
# A gate step that says it started (file `started`), then waits for the file `go`, then passes.
HOLD = {'name': 'unit', 'always': True, 'pass': '^ok$', 'cmd': [
    '{python}', '-c',
    'import os, time; d = os.environ["%s"]; open(os.path.join(d, "started"), "w").close(); '
    '[time.sleep(0.05) for _ in range(2400) if not os.path.exists(os.path.join(d, "go"))]; '
    'print("ok")' % SIGNALS]}
# A child that takes the lock at argv[2], says so, and holds it until its stdin closes.
HOLDER = ('import sys; sys.path.insert(0, sys.argv[1]); '
          'from kitlib.dailyfull import treelock as T; '
          'T.TreeLock(sys.argv[2], "a test holder").acquire(); print("held", flush=True); '
          'sys.stdin.read()')
WAIT_S = 60


def wait_for(path, seconds=WAIT_S):
    end = time.monotonic() + seconds
    while not os.path.exists(path):
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


class LockCase(Case):
    def setUp(self):
        Case.setUp(self)
        self.wt = os.path.join(self.main_dir, '.claude', 'worktrees', D.SLUG)

    def lock_path(self):
        lock, why = D.tree_lock(self.main_dir, self.wt)
        self.assertIsNone(why)
        return lock.path

    def head(self):
        return self.repo.git('rev-parse', 'HEAD', cwd=self.wt)

    def assert_free(self):
        """Another holder can take the lock now, and no record was left behind."""
        lock = D.treelock.TreeLock(self.lock_path())
        lock.acquire()
        try:
            self.assertEqual(lock.took_over, {})
        finally:
            lock.release()

    def holder(self):
        """A child process holding the lock: (Popen, its pid)."""
        p = subprocess.Popen([sys.executable, '-c', HOLDER, KIT, self.lock_path()],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end, p)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        return p

    @staticmethod
    def _end(p):
        if p.poll() is None:
            p.kill()             # only this test's own child, by its handle
        p.wait(30)
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass


class SecondRunMidGate(LockCase):
    """The race: a second run starts while the first is mid-gate, after the ref moved."""
    GATE = {'steps': [HOLD]}

    def setUp(self):
        LockCase.setUp(self)
        self.sig = os.path.join(self.tmp, 'signals')
        os.makedirs(self.sig)
        self.book_kw = dict(self.book_kw, env=dict(self.env, **{SIGNALS: self.sig}))

    def test_the_second_run_is_busy_and_never_moves_the_tree(self):
        first = self.remote_main()
        seen = {}

        def second():
            try:
                if not wait_for(os.path.join(self.sig, 'started')):
                    seen['error'] = 'the first run never reached its gate step'
                    return
                seen['head_mid_gate'] = self.head()
                self.push({'RED': 'x\n'})             # the ref moves under the first run
                seen['pushed'] = self.remote_main()
                try:
                    seen['outcome'] = D.run(self.main_dir, self.cfg(), out=self.out,
                                            stamp='20261008-040100',
                                            book_kw=dict(self.book_kw, wait_s=0))
                except Exception as e:                  # recorded, judged below
                    seen['raised'] = repr(e)
                seen['head_after_second'] = self.head()
            finally:
                open(os.path.join(self.sig, 'go'), 'w').close()

        t = threading.Thread(target=second)
        t.start()
        first_outcome = self.run_daily('20261008-040000')
        t.join(WAIT_S)
        self.assertFalse(t.is_alive())
        self.assertNotIn('error', seen)
        self.assertEqual(seen['head_mid_gate'], first)
        self.assertNotEqual(seen['pushed'], first)
        self.assertEqual(seen['head_after_second'], first, 'the second run moved the tree mid-gate')
        self.assertNotIn('raised', seen)
        o = seen['outcome']
        self.assertEqual(o.code, G.BUSY, o.lines)
        self.assertIn('busy: held by pid %d since ' % os.getpid(), o.lines[0])
        self.assertFalse(os.path.exists(os.path.join(self.out, '20261008-040100')))
        self.assertEqual(first_outcome.code, G.OK, first_outcome.lines)
        r = self.result('20261008-040000')
        self.assertEqual((r['verdict'], r['sha']), ('green', first))
        self.assert_free()


class ChildHolders(LockCase):
    def test_a_live_holder_makes_a_run_busy_and_the_tree_stays(self):
        self.assertEqual(self.run_daily('20261007-040000').code, G.OK)
        before = self.head()
        self.push({'RED': 'x\n'})
        p = self.holder()
        o = self.run_daily()
        self.assertEqual(o.code, G.BUSY, o.lines)
        self.assertIn('busy: held by pid %d since ' % p.pid, o.lines[0])
        self.assertIn('a test holder', o.lines[0])
        self.assertEqual(self.head(), before)
        self.assertFalse(os.path.exists(os.path.join(self.out, '20261008-040000')))
        p.stdin.close()                                 # the holder lets go and exits
        self.assertEqual(p.wait(30), 0)
        self.assertEqual(self.run_daily('20261009-040000').code, G.FAILED)
        self.assertEqual(self.head(), self.remote_main())

    def test_a_killed_holder_is_taken_over(self):
        p = self.holder()
        p.kill()
        p.wait(30)
        left = D.treelock.read_record(self.lock_path())
        self.assertEqual(left.get('pid'), p.pid)        # its record is still there: stale
        said = []
        o = D.run(self.main_dir, self.cfg(), out=self.out, stamp='20261008-040000',
                  say=said.append, book_kw=self.book_kw)
        self.assertEqual(o.code, G.OK, o.lines)
        self.assertTrue(any('took over' in s and 'pid %d' % p.pid in s for s in said), said)
        self.assertEqual(self.head(), self.remote_main())
        self.assert_free()

    def test_a_holder_that_exits_without_releasing_is_taken_over(self):
        code = HOLDER.replace('sys.stdin.read()', 'import os; os._exit(0)')
        p = subprocess.run([sys.executable, '-c', code, KIT, self.lock_path()],
                           capture_output=True, text=True, timeout=WAIT_S)
        self.assertEqual(p.stdout.strip(), 'held', p.stderr)
        self.assertTrue(D.treelock.read_record(self.lock_path()).get('pid'))
        self.assertEqual(self.run_daily().code, G.OK)
        self.assert_free()


class Released(LockCase):
    """The lock is let go on every way out of a run that took it."""

    def test_after_a_green_run(self):
        self.assertEqual(self.run_daily().code, G.OK)
        self.assert_free()

    def test_after_a_refused_run(self):
        self.repo.write('.kit.json', '{"gate": %s, "daily_full": {"enabled": true, '
                        '"kind": "engine-test", "ref": "origin/no-such"}}\n'
                        % json.dumps(self.GATE))
        self.repo.commit('ref', '.kit.json')
        self.assertEqual(self.run_daily().code, G.REFUSED)
        self.assert_free()

    def test_after_an_exception(self):
        with mock.patch.object(runner, 'gated', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.run_daily()
        self.assert_free()

    def test_a_busy_run_leaves_the_holder_holding(self):
        lock = D.treelock.TreeLock(self.lock_path(), 'the first run').acquire()
        try:
            self.assertEqual(self.run_daily().code, G.BUSY)
            self.assertTrue(lock.held)
            with self.assertRaises(D.treelock.TreeBusy):
                D.treelock.TreeLock(self.lock_path()).acquire()
        finally:
            lock.release()
        self.assert_free()


class ReleasedAfterRed(LockCase):
    GATE = {'steps': [FAILING]}

    def test_after_a_red_run(self):
        self.assertEqual(self.run_daily().code, G.FAILED)
        self.assert_free()


class Names(LockCase):
    def test_one_lock_per_tree(self):
        a = D.treelock.lock_name(self.wt)
        self.assertEqual(a, D.treelock.lock_name(self.wt.upper()))   # Windows paths fold case
        self.assertNotEqual(a, D.treelock.lock_name(self.wt + '-2'))
        self.assertEqual(os.path.normcase(os.path.dirname(self.lock_path())),
                         os.path.normcase(os.path.realpath(self.repo.common_dir())))


class Failures(LockCase):
    """A lock that fails for any reason but contention is a refusal, never busy; a record that
    cannot be read is no record; a release whose truncate fails still lets go."""

    def assert_closed(self, path):
        """No handle is left open on the lock file: Windows refuses to remove an open one."""
        os.remove(path)

    def test_a_lock_error_that_is_not_contention_is_refused_not_busy(self):
        path = self.lock_path()
        broken = OSError(errno.EIO, 'simulated I/O error')
        with mock.patch.object(D.treelock.TreeLock, '_lock_byte', side_effect=broken):
            with self.assertRaises(D.treelock.TreeLockError) as cm:
                D.treelock.TreeLock(path).acquire()
            self.assertNotIsInstance(cm.exception, D.treelock.TreeBusy)
            o = self.run_daily()
        self.assertEqual(o.code, G.REFUSED, o.lines)
        r = self.result()
        self.assertEqual((r['verdict'], r['red']), ('red', ['infra']))
        self.assertIn('the tree lock could not be taken: cannot lock', r['error'])
        self.assertIn('simulated I/O error', r['error'])
        self.assertFalse(os.path.exists(self.wt))           # never fetched into or added
        self.assert_closed(path)

    def test_contention_is_busy(self):
        path = self.lock_path()
        held = OSError(errno.EACCES, 'locked')
        with mock.patch.object(D.treelock.TreeLock, '_lock_byte', side_effect=held):
            with self.assertRaises(D.treelock.TreeBusy):
                D.treelock.TreeLock(path).acquire()
        self.assert_closed(path)

    def test_a_record_that_is_not_utf8_or_json_reads_as_none(self):
        path = self.lock_path()
        for raw in (bytes([0xff, 0xfe]) + b'{"pid": 1}', b'{"pid": ', b'[1, 2]'):
            with open(path, 'wb') as f:
                f.write(raw)
            self.assertEqual(D.treelock.read_record(path), {}, raw)

    def test_release_lets_go_when_the_truncate_fails(self):
        path = self.lock_path()
        lock = D.treelock.TreeLock(path, 'first').acquire()
        with mock.patch.object(D.treelock.os, 'ftruncate',
                               side_effect=OSError(errno.EIO, 'simulated')):
            lock.release()
        self.assertFalse(lock.held)
        second = D.treelock.TreeLock(path).acquire()     # unlocked: the next taker gets it
        try:
            self.assertEqual(second.took_over.get('what'), 'first')   # the record stayed
        finally:
            second.release()
        self.assert_closed(path)


if __name__ == '__main__':
    import unittest
    unittest.main()
