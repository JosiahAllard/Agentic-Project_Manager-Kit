"""The lock across pid namespaces (Linux; contain's W1 runs each command in a pid namespace of its
own, docs/linux-support-plan.md section 2.3). A pid is a number in one namespace:

- a run record names its wrapper's namespace (`pidns`), and a checker in another one never judges
  the wrapper by its pid (store.record_state): the run lock decides (run_live, run_dead), as it
  does in the release check (release._owner_live), which on Linux also never takes a pid that
  names no process as proof the wrapper is gone; nor is such a run the caller's own (_is_mine);
- a run nested in a W1 job of its run sees no ancestor past the job's pid 1: it is in the run
  when the record lists its namespace in `jobns` (Slot.start's note_jobns) and the run lock is
  held (nest_check); a background run's wrapper of another namespace counts as an ancestor
  (bg_ancestor, fail closed).

The first classes stand in for Linux (kitlib.platform.NAME and procfs.pidns patched) and run
anywhere; Live runs a nested check inside a real W1 job, where the trial works."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import kitlib.platform
from kitlib.lock import release, run, store as store_mod
from kitlib.lock.store import Store
from kitlib.platform.linux import process as procfs
from kitlib.proc import process
from tests.platforms import posix_only

RID = 'r-20261026T000000Z-abcdef'
OWN, OTHER = 7, 8
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def linux(ns=OWN):
    """Patches standing in for a Linux process in pid namespace ns."""
    return [mock.patch.object(kitlib.platform, 'NAME', 'linux'),
            mock.patch.object(procfs, 'pidns', lambda pid='self': ns, create=True)]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-pidns-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = Store(root=self.root)

    def as_linux(self, ns=OWN):
        for p in linux(ns):
            p.start()
            self.addCleanup(p.stop)

    def record(self, **fields):
        d = {'id': RID, 'state': 'held', 'pid': 1234, 'ctime': 99, 'kind': 'cpu-eval',
             'has': {}, 'cores': [], 'gpus': [], 'shares': {}}
        d.update(fields)
        os.makedirs(self.store.path('active'), exist_ok=True)
        with open(self.store.path('active', RID + '.json'), 'w', encoding='utf-8') as f:
            json.dump(d, f)
        return d


class RecordState(Scratch):
    def test_a_record_of_another_namespace_is_never_judged_by_its_pid(self):
        self.as_linux()
        with mock.patch.object(store_mod, 'wrapper_state', lambda pid, ct: True):
            self.assertIsNone(store_mod.record_state({'pid': 5, 'ctime': 1, 'pidns': OTHER}))
            self.assertIs(store_mod.record_state({'pid': 5, 'ctime': 1, 'pidns': OWN}), True)
            self.assertIs(store_mod.record_state({'pid': 5, 'ctime': 1}), True)

    def test_off_linux_the_namespace_is_not_read(self):
        with mock.patch.object(kitlib.platform, 'NAME', 'windows'), \
                mock.patch.object(store_mod, 'wrapper_state', lambda pid, ct: True):
            self.assertIs(store_mod.record_state({'pid': 5, 'ctime': 1, 'pidns': OTHER}), True)

    def test_the_run_lock_decides_for_a_wrapper_of_another_namespace(self):
        self.as_linux()
        d = {'id': RID, 'pid': 5, 'ctime': 1, 'pidns': OTHER}
        with mock.patch.object(store_mod, 'wrapper_state', lambda pid, ct: True):
            with mock.patch.object(self.store, 'run_mutex_held', lambda rid: False):
                self.assertFalse(self.store.run_live(d))
                self.assertTrue(self.store.run_dead(d))
            with mock.patch.object(self.store, 'run_mutex_held', lambda rid: True):
                self.assertTrue(self.store.run_live(d))
                self.assertFalse(self.store.run_dead(d))


class ReleaseCheck(unittest.TestCase):
    def setUp(self):
        for p in linux():
            p.start()
            self.addCleanup(p.stop)

    def test_on_linux_a_pid_that_names_no_process_is_not_enough(self):
        d = {'id': RID, 'pid': 1234, 'ctime': 5}
        gone = lambda pid, ct: False                    # noqa: E731
        self.assertTrue(release._owner_live(d, gone, lambda rid: False))
        self.assertFalse(release._owner_live(d, gone, lambda rid: True))
        self.assertTrue(release._owner_live(d, gone))   # no lock to ask: live

    def test_a_run_of_another_namespace_is_judged_by_its_run_lock(self):
        d = {'id': RID, 'pid': 1234, 'ctime': 5, 'pidns': OTHER}
        live = lambda pid, ct: True                     # noqa: E731
        self.assertFalse(release._owner_live(d, live, lambda rid: True))
        self.assertTrue(release._owner_live(d, live, lambda rid: False))
        self.assertTrue(release._owner_live(dict(d, pidns=OWN), live, lambda rid: True))

    def test_a_run_of_another_namespace_is_never_the_callers_own(self):
        mine = {(1234, 5)}
        self.assertFalse(release._is_mine({'pid': 1234, 'ctime': 5, 'pidns': OTHER}, mine))
        self.assertTrue(release._is_mine({'pid': 1234, 'ctime': 5, 'pidns': OWN}, mine))
        self.assertTrue(release._is_mine({'pid': 1234, 'ctime': 5}, mine))

    def test_off_linux_a_gone_pid_is_still_enough(self):
        with mock.patch.object(kitlib.platform, 'NAME', 'windows'):
            self.assertFalse(release._owner_live({'id': RID, 'pid': 1234, 'ctime': 5},
                                                 lambda pid, ct: False, lambda rid: False))


class Nest(Scratch):
    VALUE = '%s:1234:99' % RID

    def setUp(self):
        super().setUp()
        p = mock.patch.object(run, '_ancestors', set)
        p.start()
        self.addCleanup(p.stop)

    def check(self, held=True):
        with mock.patch.object(self.store, 'run_mutex_held', lambda rid: held):
            return run.nest_check(self.store, self.VALUE)

    def test_a_run_in_a_job_its_run_started_is_nested_in_it(self):
        self.as_linux()
        self.record(jobns=[OTHER + 3, OWN], has={'cpu': 2})
        parent, why = self.check()
        self.assertIsNone(why)
        self.assertEqual((parent['id'], parent['pid'], parent['has']), (RID, 1234, {'cpu': 2}))

    def test_a_job_the_run_did_not_start_is_not_its_run(self):
        self.as_linux()
        for fields in ({'jobns': [OTHER]}, {}, {'jobns': OWN}):
            with self.subTest(fields=fields):
                self.record(**fields)
                parent, why = self.check()
                self.assertIsNone(parent)
                self.assertIn('nor is this run in a job it started', why)

    def test_a_run_whose_lock_is_not_held_is_dead(self):
        self.as_linux()
        self.record(jobns=[OWN])
        for held in (False, None):
            self.assertEqual(self.check(held), (None, 'the holder is dead'))

    def test_the_record_must_still_name_the_wrapper_and_hold(self):
        self.as_linux()
        self.record(jobns=[OWN], ctime=100)
        self.assertEqual(self.check(), (None, 'the record names another wrapper'))
        self.record(jobns=[OWN], state='waiting')
        self.assertEqual(self.check(), (None, 'the named run holds no lock'))

    def test_off_linux_a_wrapper_not_an_ancestor_is_refused_before_the_record(self):
        self.record(jobns=[OWN])
        with mock.patch.object(kitlib.platform, 'NAME', 'windows'):
            self.assertEqual(self.check(),
                             (None, 'the named wrapper is not an ancestor of this run'))

    def test_a_background_wrapper_of_another_namespace_counts_as_an_ancestor(self):
        self.as_linux()
        env = {run.BG_ENV: self.VALUE}
        self.record(pidns=OTHER)
        self.assertTrue(run.bg_ancestor(env, self.store))
        self.assertFalse(run.bg_ancestor(env))          # no store: the ancestors alone
        self.record(pidns=OWN)
        self.assertFalse(run.bg_ancestor(env, self.store))
        self.record()
        self.assertFalse(run.bg_ancestor(env, self.store))
        os.remove(self.store.path('active', RID + '.json'))
        self.assertFalse(run.bg_ancestor(env, self.store))


class NoteJobns(unittest.TestCase):
    class Run:
        def __init__(self):
            self.rec, self.saved = {}, []

        def save(self):
            self.saved.append(list(self.rec['jobns']))

    class P:
        def __init__(self, ns=None):
            if ns is not None:
                self.pidns = ns

    def test_each_job_namespace_is_recorded_once_before_the_command_runs(self):
        r, seen = self.Run(), []
        before = run.note_jobns(r, seen.append)
        a, b = self.P(11), self.P(12)
        for p in (a, a, b, self.P()):
            before(p)
        self.assertEqual(r.rec['jobns'], [11, 12])
        self.assertEqual(r.saved, [[11], [11, 12]])
        self.assertEqual(len(seen), 4)

    def test_a_record_that_cannot_be_written_does_not_stop_the_start(self):
        r = self.Run()
        r.save = mock.Mock(side_effect=OSError('full'))
        run.note_jobns(r)(self.P(11))
        self.assertEqual(r.rec['jobns'], [11])


NESTED = '''
import json, os, sys
from kitlib.lock import run
from kitlib.lock.store import Store, record_state
s = Store(root=sys.argv[1])
value = sys.argv[2] if len(sys.argv) > 2 else os.environ[run.NEST_ENV]
parent, why = run.nest_check(s, value)
d = s.read('active/%s.json' % value.split(':')[0])
print(json.dumps({'parent': parent and parent['id'], 'why': why, 'state': record_state(d),
                  'live': s.run_live(d), 'dead': s.run_dead(d)}))
'''


@posix_only('pid namespaces are Linux')
class Live(Scratch):
    """A nested run's checks from inside a real W1 job of its run."""

    def setUp(self):
        super().setUp()
        from kitlib.platform.linux import contain
        self.c = contain
        no = contain.w1_trial()
        if no is not None:
            self.skipTest('no W1 here: %s' % no)
        os.makedirs(self.store.path('runs'), exist_ok=True)
        self.lock = self.store.run_mutex(RID)
        self.addCleanup(self.lock.release)

    def nested(self, note):
        r = run.Run.__new__(run.Run)
        r.store, r.rel = self.store, 'active/%s.json' % RID
        r.rec = self.record(pid=os.getpid(), ctime=process.own_ctime(), pidns=procfs.pidns())
        job = self.c.Job(tier=self.c.Tier('W1', self.c.W1_LACKS))
        self.addCleanup(job.close)
        p = job.start([sys.executable, '-c', NESTED, self.root,
                       '%s:%d:%d' % (RID, os.getpid(), process.own_ctime())],
                      cwd=ROOT, stdout=subprocess.PIPE,
                      before_resume=run.note_jobns(r) if note else None)
        out = p.communicate(timeout=60)[0]
        self.assertEqual(p.returncode, 0, out)
        return job, json.loads(out)

    def test_a_run_nested_in_a_w1_job_of_its_run_is_in_it(self):
        job, seen = self.nested(True)
        self.assertEqual(seen, {'parent': RID, 'why': None, 'state': None, 'live': True,
                                'dead': False})
        self.assertEqual(self.store.read('active/%s.json' % RID)['jobns'], [job.ns])

    def test_without_the_job_s_namespace_on_record_it_takes_its_own_lock(self):
        _job, seen = self.nested(False)
        self.assertIsNone(seen['parent'])
        self.assertIn('nor is this run in a job it started', seen['why'])
        self.assertTrue(seen['live'])

    def test_a_held_run_s_w1_command_runs_nested_runs_in_its_run(self):
        self.lock.release()
        with open(self.store.path('machine.json'), 'w', encoding='utf-8') as f:
            json.dump({'cpu_slot_threads': 1, 'agent_cap': 2}, f)
        w1 = self.c.Tier('W1', self.c.W1_LACKS)
        with mock.patch.object(self.c, 'probe', lambda fresh=False: w1):
            s = run.book('engine-test', 'kit', 't', cores=1, store=self.store,
                         kinds_table={'engine-test': {'cpu': 1, 'gpu': 0, 'warn_min': 30}})
            self.addCleanup(s.release)
            rec = self.store.read('active/%s.json' % s.id)
            self.assertEqual(rec['pidns'], procfs.pidns())
            p = s.start([sys.executable, '-c', NESTED, self.root], cwd=ROOT,
                        stdout=subprocess.PIPE)
            out = p.communicate(timeout=60)[0]
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(json.loads(out), {'parent': s.id, 'why': None, 'state': None,
                                           'live': True, 'dead': False})
        self.assertEqual(len(self.store.read('active/%s.json' % s.id)['jobns']), 1)


if __name__ == '__main__':
    unittest.main()
