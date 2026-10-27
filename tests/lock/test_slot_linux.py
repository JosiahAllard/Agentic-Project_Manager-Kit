"""A held run on Linux for real (docs/linux-support-plan.md section 8, slice 7): a Slot on a
scratch store, whose JobSet starts each command in a systemd scope of its own. A give-back narrows
the cpuset of every live command's scope and the next command's; at the release a straggler leaves
the run's scope for a leftovers scope of its own, so the run's scope ends, and that is said.
Needs the user's systemd with cpuset (tier S1) and two cpus; skipped elsewhere. The Slot on
Windows job objects: tests/lock/test_slot_jobset.py."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests.platforms import posix_only

PREFIX = 'pmkit-lockruns-%d' % os.getpid()
KINDS = {"engine-test": {"cpu": 2, "gpu": 0, "warn_min": 30}}


def units():
    """This test process's units that systemd still lists, any state."""
    r = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend',
                        PREFIX + '-*'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    return [line.split()[0] for line in r.stdout.decode().splitlines() if line.split()]


def gone(pid):
    """True when pid is no process, or an ended one (a zombie)."""
    try:
        with open('/proc/%d/stat' % pid) as f:
            text = f.read()
    except OSError:
        return True
    return text[text.rindex(')') + 2] in 'ZX'


def wait_until(test, what, timeout=10.0):
    end = time.monotonic() + timeout
    while not test():
        if time.monotonic() > end:
            raise AssertionError('not within %g s: %s' % (timeout, what))
        time.sleep(0.02)


@posix_only('systemd scopes and cgroup cpusets are Linux')
class Live(unittest.TestCase):
    def setUp(self):
        from kitlib.lock import run as rm
        from kitlib.lock.store import Store
        from kitlib.platform.linux import contain
        self.rm, self.c = rm, contain
        if contain.probe().name != 'S1':
            self.skipTest('no cgroup cpuset here: %s' % contain.probe().text)
        if len(os.sched_getaffinity(0)) < 2:
            self.skipTest('one cpu only')
        patch = mock.patch.dict(os.environ, {contain.PREFIX_ENV: PREFIX})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(lambda: wait_until(lambda: not units(), 'no unit of %s left (%s)'
                                           % (PREFIX, units())))
        root = tempfile.mkdtemp(prefix='kit-lockruns-')
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, 'machine.json'), 'w', encoding='utf-8') as f:
            json.dump({"cpu_slot_threads": 1, "agent_cap": 2}, f)  # two one-thread slots
        self.said = []
        self.store = Store(root=root, say=self.said.append)

    def held(self):
        s = self.rm.book("engine-test", "kit", "t", cores=2, store=self.store, kinds_table=KINDS)
        self.addCleanup(s.release)
        return s

    def sleeper(self, s, argv=('sleep', '60'), **kw):
        p = s.start(list(argv), **kw)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        return p

    def scope(self, pid):
        """(the scope pid's job runs in, that scope's cpuset.cpus.effective as a set)."""
        cg = self.c.cgroup_of(pid)
        while cg and not cg.endswith('.scope'):
            cg = os.path.dirname(cg)
        with open(os.path.join(self.c.cg_dir(cg), 'cpuset.cpus.effective')) as f:
            text = f.read().strip()
        cpus = set()
        for part in text.split(','):
            lo, _, hi = part.partition('-')
            cpus.update(range(int(lo), int(hi or lo) + 1))
        return cg, cpus

    def test_a_give_back_narrows_the_cpuset_of_every_live_command_and_the_next(self):
        s = self.held()
        a = self.sleeper(s)
        b = self.sleeper(s, give_back=True)
        (ca, sa), (cb, sb) = self.scope(a.pid), self.scope(b.pid)
        self.assertNotEqual(ca, cb, 'each command in a scope of its own')
        both = os.sched_getaffinity(a.pid)
        self.assertEqual(len(both), 2, 'the run holds two one-thread slots')
        self.assertEqual((sa, sb), (both, both))
        from kitlib.proc import process
        give = s.cores[1]
        req = {"id": "x1", "cpu": [give], "by_pid": b.pid,
               "by_ctime": process.proc_ctime(b.pid), "at": self.rm.iso()}
        with open(s.offer, 'w', encoding='utf-8') as f:
            json.dump(req, f)
        self.assertIsNone(s.serve_give_back())
        self.assertEqual(s.cores, [s.cores[0]])
        kept = os.sched_getaffinity(a.pid)
        self.assertEqual(len(kept), 1)
        self.assertLess(kept, both)
        self.assertEqual((self.scope(a.pid)[1], self.scope(b.pid)[1]), (kept, kept))
        self.assertEqual(os.sched_getaffinity(b.pid), kept)
        self.assertTrue(any(t.startswith('run %s gave back cpu slot' % s.id) for t in self.said),
                        self.said)
        c = self.sleeper(s)
        self.assertEqual(self.scope(c.pid)[1], kept, 'the next command starts narrowed')

    def test_a_straggler_leaves_for_a_leftovers_scope_and_the_run_s_scope_ends(self):
        s = self.held()
        d = s.start(['sh', '-c', 'sleep 60 & echo $!'], stdout=subprocess.PIPE, text=True)
        left = int(d.stdout.readline())
        d.stdout.close()
        d.wait(10)
        self.addCleanup(lambda: gone(left) or os.kill(left, 9))
        job_scope = self.scope(left)[0]
        self.assertIn('/%s-job-' % PREFIX, job_scope)
        rec = s.release()
        self.assertEqual(rec['how'], 'done')
        self.assertFalse(gone(left), 'the straggler runs on')
        now = self.c.cgroup_of(left)
        self.assertRegex(now, r'/%s-leftovers-[^/]+\.scope$' % PREFIX)
        wait_until(lambda: not os.path.exists(self.c.cg_dir(job_scope)), 'the run scope ended')
        self.assertEqual(os.sched_getaffinity(left), os.sched_getaffinity(0), 'unpinned')
        self.assertIn('run %s: left pid %d running, moved out of its job' % (s.id, left),
                      self.said)


if __name__ == '__main__':
    unittest.main()
