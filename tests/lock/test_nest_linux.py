"""The lock's process checks on Linux (docs/linux-support-plan.md section 8, slice 4), on the /proc
ctime `(boot << 64) | starttime` (kitlib.platform.linux.process):

- a record or token written before a reboot names no live process after it (the boot id is the
  seam BOOT_ID, pointed at a file), and one naming a reused pid (another starttime) names none;
- wrapper_state's three answers; run_dead and run_live's reap need the pid and ctime gone and
  the run lock free too (a checker in its own pid namespace sees no wrapper's pid, but sees its
  flock);
- nest_check takes a token whose wrapper is a live ancestor holding its record, and refuses one
  from another boot or a reused pid;
- _end_others ends a job process created before its stamp and not one created after it."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib.lock import run, store as store_mod
from kitlib.lock.store import Store
from kitlib.platform.linux import process as procfs
from kitlib.proc import process
from tests.platforms import posix_only

LOW64 = (1 << 64) - 1
RID = 'r-20261026T000000Z-abcdef'
SLEEPER = [sys.executable, '-c', 'import time; time.sleep(60)']


def as_boot_id(n):
    h = '%032x' % n
    return '%s-%s-%s-%s-%s\n' % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def wait_until(test, what, timeout=10.0):
    end = time.monotonic() + timeout
    while not test():
        if time.monotonic() > end:
            raise AssertionError('not within %g s: %s' % (timeout, what))
        time.sleep(0.02)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-nest-linux-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(root=self.root)

    def spawn(self, cmd=SLEEPER):
        p = subprocess.Popen(cmd)
        self.addCleanup(self._end, p)
        return p

    @staticmethod
    def _end(p):
        if p.poll() is None:
            p.kill()             # only this test's own child, by its handle
        p.wait(30)


@posix_only('the lock reads /proc on Linux')
class Reboot(Base):
    def test_a_ctime_from_before_a_reboot_names_no_live_process(self):
        me, now = os.getpid(), process.own_ctime()
        before = now                                    # this boot's ctime of this process
        boot_file = os.path.join(self.root, 'boot_id')
        with open(boot_file, 'w') as f:
            f.write(as_boot_id(procfs.boot() ^ 1))      # the next boot
        with mock.patch.object(procfs, 'BOOT_ID', boot_file):
            after = process.own_ctime()
            self.assertEqual(after & LOW64, before & LOW64)
            self.assertNotEqual(after, before)
            self.assertFalse(process.alive(me, before))
            self.assertTrue(process.alive(me, after))
            self.assertIs(store_mod.wrapper_state(me, before), False)
            self.assertIs(store_mod.wrapper_state(me, after), True)
            old = {'id': RID, 'pid': me, 'ctime': before}
            self.assertFalse(self.store.run_live(old))
            self.assertTrue(self.store.run_dead(old))
            self.assertFalse(self.store.run_dead({'id': RID, 'pid': me, 'ctime': after}))

    def test_a_boot_id_that_cannot_be_read_cannot_be_told(self):
        with mock.patch.object(procfs, 'BOOT_ID', os.path.join(self.root, 'no-such-file')):
            self.assertIsNone(store_mod.wrapper_state(os.getpid(), 1))
            self.assertTrue(process.alive(os.getpid(), 1))
            self.assertIsNone(process.own_ctime())


@posix_only('the lock reads /proc on Linux')
class WrapperState(Base):
    def test_a_live_process_at_its_ctime_and_at_another(self):
        me, ct = os.getpid(), process.own_ctime()
        self.assertIs(store_mod.wrapper_state(me, ct), True)
        self.assertIs(store_mod.wrapper_state(me, ct - 1), False)   # a reused pid
        self.assertIsNone(store_mod.wrapper_state(me, None))
        self.assertIsNone(store_mod.wrapper_state('x', ct))
        self.assertIs(store_mod.wrapper_state(0, ct), False)

    def test_an_ended_process_is_not_live_before_or_after_it_is_reaped(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        self.addCleanup(p.wait, 30)
        ct = process.proc_ctime(p.pid)
        wait_until(lambda: procfs.identity(p.pid)[1], 'the child is a zombie')
        self.assertIs(store_mod.wrapper_state(p.pid, ct), False)
        p.wait(30)
        self.assertIs(store_mod.wrapper_state(p.pid, ct), False)
        self.assertIs(store_mod.wrapper_state(p.pid, None), False)

    def gone_record(self):
        """A record whose pid and ctime name a process that has ended and been reaped."""
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        ct = process.proc_ctime(p.pid)
        p.wait(30)
        self.assertIs(store_mod.wrapper_state(p.pid, ct), False)
        return {'id': RID, 'state': 'held', 'kind': 'build', 'pid': p.pid, 'ctime': ct,
                'has': {'cpu': 1}}

    def test_run_dead_needs_the_pid_gone_and_the_run_lock_free(self):
        d = self.gone_record()
        m = self.store.run_mutex(RID)
        self.addCleanup(m.release)
        # The pid check alone says gone (a checker in its own pid namespace sees none): the
        # held run lock keeps it.
        self.assertFalse(self.store.run_dead(d))
        self.assertTrue(self.store.run_live(d))
        me = {'id': RID, 'pid': os.getpid(), 'ctime': process.own_ctime()}
        self.assertFalse(self.store.run_dead(me))
        self.assertTrue(self.store.run_live(me))
        m.release()
        self.assertTrue(self.store.run_dead(d))
        self.assertFalse(self.store.run_live(d))
        self.assertFalse(self.store.run_dead(me))       # its run lock is free, but it is alive

    def test_a_record_whose_pid_is_gone_is_not_reaped_while_its_run_lock_is_held(self):
        d = self.gone_record()
        self.store.write('active/%s.json' % RID, d)
        m = self.store.run_mutex(RID)
        self.addCleanup(m.release)
        self.assertEqual([x['id'] for x in self.store.live_runs(prune=True)], [RID])
        self.assertIsNotNone(self.store.read('active/%s.json' % RID))
        self.assertIsNone(self.store.read('ended/%s.json' % RID))
        m.release()
        self.assertEqual(self.store.live_runs(prune=True), [])
        self.assertIsNone(self.store.read('active/%s.json' % RID))
        self.assertEqual(self.store.read('ended/%s.json' % RID)['reaped'],
                         'its wrapper process is gone')

    def test_a_record_without_a_run_id_whose_pid_is_gone_is_not_live(self):
        d = dict(self.gone_record(), id=None)
        self.assertFalse(self.store.run_live(d))


@posix_only('the lock reads /proc on Linux')
class Nest(Base):
    def setUp(self):
        super().setUp()
        self.ppid = os.getppid()
        self.pct = process.proc_ctime(self.ppid)
        self.assertIsNotNone(self.pct)

    def hold(self, ct):
        self.store.write('active/%s.json' % RID, {'id': RID, 'state': 'held', 'kind': 'build',
                                                  'pid': self.ppid, 'ctime': ct,
                                                  'has': {'cpu': 1}})

    def test_a_live_ancestor_holding_its_record_is_the_parent(self):
        self.hold(self.pct)
        parent, why = run.nest_check(self.store, '%s:%d:%d' % (RID, self.ppid, self.pct))
        self.assertIsNone(why)
        self.assertEqual((parent['id'], parent['pid'], parent['ctime'], parent['kind']),
                         (RID, self.ppid, self.pct, 'build'))

    def test_a_token_from_before_a_reboot_is_refused(self):
        old = ((procfs.boot() ^ 1) << 64) | (self.pct & LOW64)
        self.hold(old)
        self.assertEqual(run.nest_check(self.store, '%s:%d:%d' % (RID, self.ppid, old)),
                         (None, 'the named wrapper is not an ancestor of this run'))

    def test_a_token_naming_a_reused_pid_is_refused(self):
        reused = self.pct - 1           # the pid's process before: an earlier starttime
        self.hold(reused)
        self.assertEqual(run.nest_check(self.store, '%s:%d:%d' % (RID, self.ppid, reused)),
                         (None, 'the named wrapper is not an ancestor of this run'))


class FakeJob:
    """A job of the given processes: pids() the ones still running, each one "in"."""

    def __init__(self, procs):
        self.procs = procs

    def pids(self):
        return [p.pid for p in self.procs if p.poll() is None]

    def contains(self, pid):
        return 'in'


@posix_only('the lock reads /proc on Linux')
class EndOthers(Base):
    def test_the_stamp_is_after_a_process_created_before_it_and_not_after_one_created_after(self):
        a = self.spawn()
        time.sleep(0.05)
        stamp = run.filetime_now()
        time.sleep(0.05)
        b = self.spawn()
        self.assertEqual(stamp >> 64, procfs.boot())
        self.assertLess(process.proc_ctime(a.pid), stamp)
        self.assertGreaterEqual(process.proc_ctime(b.pid), stamp)
        self.assertLessEqual(stamp, run.filetime_now())

    def test_a_job_process_created_before_the_stamp_is_ended_and_one_after_it_is_not(self):
        a = self.spawn()
        time.sleep(0.05)
        stamp = run.filetime_now()
        time.sleep(0.05)
        b = self.spawn()
        with mock.patch.object(run, 'filetime_now', return_value=stamp), \
                mock.patch.object(run, 'END_PASSES', 2), mock.patch.object(run, 'END_WAIT_S', 0.2):
            left = run._end_others(FakeJob([a, b]), [])
        self.assertEqual(left, [b.pid])
        self.assertEqual(a.wait(30), -9)
        self.assertIsNone(b.poll())

    def test_a_kept_process_and_its_tree_are_spared(self):
        a = self.spawn()
        time.sleep(0.05)
        with mock.patch.object(run, 'END_PASSES', 2), mock.patch.object(run, 'END_WAIT_S', 0.2):
            left = run._end_others(FakeJob([a]), [(a.pid, process.proc_ctime(a.pid))])
        self.assertEqual(left, [])
        self.assertIsNone(a.poll())


if __name__ == '__main__':
    unittest.main()
