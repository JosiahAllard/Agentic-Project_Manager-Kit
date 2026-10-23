"""kitlib.tooldeps.pool and pool_worker: kit_tests' long-lived worker interpreters (docs/test-tools.md
"kit_tests"). Real workers run throwaway test modules: one worker runs a unit that changes the
interpreter's state, then a unit that fails if any of it is left; and a unit that times out,
crashes, is interrupted, or leaves a process or a thread running gets a new worker for the next;
so does a worker that ended while idle, one whose job cannot be queried, one never ready, and one
whose unit added an audit hook - but not one a preloaded package added.
Ending a worker returns once every process in its job has ended (GONE_WAIT at most).
"""
import _thread
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.tooldeps import pool
from tests.gitfixture import fixture_parent, rmtree

DIRTY = r'''import atexit
import os
import signal
import subprocess
import sys
import tempfile
import unittest

import tests.p.helper  # noqa: F401 - a module the unit imports


def at_exit(path):
    with open(path, 'w') as fh:
        fh.write('ran')


class T(unittest.TestCase):
    def test_leaves_everything_changed(self):
        out = os.environ['PROBE_OUT']
        with open(os.path.join(out, 'dirty.json'), 'w') as fh:
            fh.write('{"pid": %d, "temp": %s}' % (os.getpid(), __import__('json').dumps(
                tempfile.gettempdir())))
        os.chdir(out)
        os.environ['PROBE_DIRTY'] = '1'
        sys.path.insert(0, 'dirty-path')
        sys.argv.append('dirty-arg')
        tempfile.tempdir = out
        tempfile.template = 'dirty'
        unittest.probe_dirty = True
        signal.signal(signal.SIGINT, lambda *a: None)
        sys.stdout = open(os.devnull, 'w')
        sys.setrecursionlimit(500)
        atexit.register(at_exit, os.path.join(out, 'atexit.txt'))
        print('printed by the unit', file=sys.__stdout__, flush=True)
        subprocess.run([sys.executable, '-c', 'print("printed by its child")'], check=True)
'''

CLEAN = r'''import os
import signal
import sys
import tempfile
import unittest


class T(unittest.TestCase):
    def test_finds_a_fresh_interpreter(self):
        out = os.environ['PROBE_OUT']
        with open(os.path.join(out, 'clean.json'), 'w') as fh:
            fh.write('{"pid": %d}' % os.getpid())
        self.assertEqual(os.path.normcase(os.getcwd()), os.path.normcase(os.environ['PROBE_ROOT']))
        self.assertNotIn('PROBE_DIRTY', os.environ)
        self.assertEqual(os.path.normcase(sys.path[0]), os.path.normcase(os.getcwd()))
        self.assertNotIn('dirty-path', sys.path)
        self.assertEqual(sys.argv, ['python -m unittest', 'tests.p.test_clean'])
        self.assertNotIn('tests.p.helper', sys.modules)
        self.assertNotIn('tests.p.test_dirty', sys.modules)
        self.assertEqual(os.path.normcase(tempfile.gettempdir()),
                         os.path.normcase(os.environ['TEMP']))
        self.assertEqual(tempfile.template, 'tmp')
        self.assertFalse(hasattr(unittest, 'probe_dirty'))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertIs(sys.stdout, sys.__stdout__)
        self.assertEqual(sys.getrecursionlimit(), 1000)
        self.assertTrue(os.path.exists(os.path.join(out, 'atexit.txt')))
'''

PID = '''import os
import unittest


class T(unittest.TestCase):
    def test_pid(self):
        with open(os.path.join(os.environ['PROBE_OUT'], __name__ + '.pid'), 'w') as fh:
            fh.write(str(os.getpid()))
'''


# What the worker's code cache (pool_worker.CODE) holds for tests.p.helper when a unit imports it.
CACHE = r'''import json
import os
import sys
import unittest

import tests.p.helper as helper


class T(unittest.TestCase):
    def test_reads_the_cache(self):
        code = sys.modules['__main__'].CODE
        path = helper.__file__
        with open(os.path.join(os.environ['PROBE_OUT'], __name__ + '.json'), 'w') as fh:
            json.dump({'value': helper.VALUE, 'compiled': code.compiled.get(path, 0),
                       'reused': code.reused.get(path, 0),
                       'own': code.compiled.get(__file__, 0)}, fh)
'''


def unit_body(body, imports=''):
    return ('import os\nimport sys\nimport time\nimport unittest\n%s\n\n'
            'class T(unittest.TestCase):\n    def test_it(self):\n%s' % (imports, body))


def write(root, files):
    for rel, text in files.items():
        full = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as fh:
            fh.write(text)


@unittest.skipUnless(os.name == 'nt', 'workers run in Windows job objects')
class Pool(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-pool-', dir=fixture_parent())
        self.addCleanup(rmtree, self.root)
        self.out = os.path.join(self.root, 'out')
        self.folder = os.path.join(self.root, 'work')
        os.makedirs(self.out)
        os.makedirs(self.folder)
        write(self.root, {'tests/__init__.py': '', 'tests/p/__init__.py': '',
                          'tests/p/helper.py': 'VALUE = 1\n',
                          'tests/p/test_dirty.py': DIRTY, 'tests/p/test_clean.py': CLEAN,
                          'tests/p/test_pid.py': PID, 'tests/p/test_pid2.py': PID})
        # As kit_tests runs them: no bytecode file, so each import of a source compiles it (or
        # takes it from the worker's code cache).
        self.env = dict(os.environ, PROBE_OUT=self.out, PROBE_ROOT=self.root,
                        PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('PYTHONPATH', None)
        self.pool = pool.WorkerPool(self.root, self.folder, self.env)
        self.addCleanup(self.pool.close)
        self.n = 0

    def unit(self, name, timeout=120):
        self.n += 1
        temp = os.path.join(self.root, 'temp%d' % self.n)
        os.makedirs(temp)
        env = dict(self.env, TEMP=temp, TMP=temp)
        return self.pool.run_unit((name, 1, 1), False, timeout, env=env)

    def read(self, name):
        with open(os.path.join(self.out, name)) as fh:
            text = fh.read()
        return json.loads(text) if name.endswith('.json') else int(text)

    def test_one_worker_runs_units_one_after_another_and_resets_between_them(self):
        unit, status, ran, _secs, text = self.unit('tests.p.test_dirty')
        self.assertEqual((status, ran), ('OK', 1), text)
        self.assertIn('printed by the unit', text)
        self.assertIn('printed by its child', text)          # fds 1 and 2 reach its children
        self.assertTrue(os.path.exists(os.path.join(self.out, 'atexit.txt')))   # at its end
        _u, status, ran, _secs, text = self.unit('tests.p.test_clean')
        self.assertEqual((status, ran), ('OK', 1), text)
        dirty, clean = self.read('dirty.json'), self.read('clean.json')
        self.assertEqual(dirty['pid'], clean['pid'])         # the same interpreter
        self.assertEqual(os.path.normcase(dirty['temp']),
                         os.path.normcase(os.path.join(self.root, 'temp1')))
        self.assertEqual(self.pool.stats(), (1, {}))
        self.assertEqual(os.listdir(self.folder), [])         # output files are read and removed

    def test_each_unit_s_process_starts_are_counted_and_the_worker_s_own_are_not(self):
        # The worker has a console (its own or this process's), so each child shares it: no
        # conhost, one start each. A child given a console of its own adds its conhost.
        write(self.root, {'tests/p/test_three.py': unit_body(
            "        for _ in range(3):\n"
            "            subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
            "        subprocess.run([sys.executable, '-c', 'pass'], check=True,\n"
            "                       creationflags=subprocess.CREATE_NO_WINDOW)\n",
            imports='import subprocess')})
        _u, status, _ran, _s, text = self.unit('tests.p.test_three')
        self.assertEqual(status, 'OK', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (1, {}))             # one worker ran both
        self.assertEqual(self.pool.starts(), {'tests.p.test_three': 3 + 2, 'tests.p.test_pid': 0})
        self.assertEqual(self.pool.starts('tests.p.test_three'), 5)

    def cache_units(self, between=None):
        """Run two units that import tests.p.helper in one worker, `between()` after the first;
        what each read from the worker's code cache."""
        write(self.root, {'tests/p/test_cache1.py': CACHE, 'tests/p/test_cache2.py': CACHE})
        seen = []
        for i, name in enumerate(('tests.p.test_cache1', 'tests.p.test_cache2')):
            if i and between:
                between()
            _u, status, _ran, _s, text = self.unit(name)
            self.assertEqual(status, 'OK', text)
            seen.append(self.read(name + '.json'))
        self.assertEqual(self.pool.stats(), (1, {}))             # one worker ran both
        return seen

    def test_a_later_unit_reuses_the_code_compiled_for_the_same_source(self):
        first, second = self.cache_units()
        self.assertEqual(first, {'value': 1, 'compiled': 1, 'reused': 0, 'own': 1})
        self.assertEqual(second, {'value': 1, 'compiled': 1, 'reused': 1, 'own': 1})

    def test_a_changed_source_is_compiled_again_whatever_its_time_stamp_and_size(self):
        helper = os.path.join(self.root, 'tests', 'p', 'helper.py')

        def change():
            st = os.stat(helper)
            write(self.root, {'tests/p/helper.py': 'VALUE = 2\n'})     # the same size
            os.utime(helper, ns=(st.st_atime_ns, st.st_mtime_ns))    # and time stamp
            self.assertEqual(os.stat(helper).st_size, st.st_size)
        first, second = self.cache_units(change)
        self.assertEqual(first, {'value': 1, 'compiled': 1, 'reused': 0, 'own': 1})
        self.assertEqual(second, {'value': 2, 'compiled': 2, 'reused': 0, 'own': 1})

    def test_a_failing_unit_reports_its_output_and_the_worker_is_kept(self):
        write(self.root, {'tests/p/test_fails.py': unit_body("        self.assertEqual(1, 2)\n")})
        _u, status, ran, _s, text = self.unit('tests.p.test_fails')
        self.assertEqual((status, ran), ('FAILED', 1), text)
        self.assertIn('AssertionError: 1 != 2', text)
        self.assertIn('FAILED (failures=1)', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (1, {}))

    def test_a_unit_that_runs_no_test_or_does_not_import_fails(self):
        write(self.root, {'tests/p/test_none.py': 'import unittest\n',
                          'tests/p/test_broken.py': 'import no_such_module_anywhere\n'})
        _u, status, ran, _s, text = self.unit('tests.p.test_none')
        self.assertEqual((status, ran), ('FAILED', 0), text)
        self.assertIn('NO TESTS RAN', text)                 # unittest's exit 5, as a process
        _u, status, _ran, _s, text = self.unit('tests.p.test_broken')
        self.assertEqual(status, 'FAILED', text)
        self.assertIn("No module named 'no_such_module_anywhere'", text)
        self.assertEqual(self.pool.stats(), (1, {}))

    def test_a_unit_past_its_timeout_is_killed_and_the_next_gets_a_new_worker(self):
        write(self.root, {'tests/p/test_slow.py': unit_body(
            "        with open(os.path.join(os.environ['PROBE_OUT'], 'slow.pid'), 'w') as fh:\n"
            "            fh.write(str(os.getpid()))\n        time.sleep(60)\n")})
        t = time.time()
        _u, status, _ran, _s, text = self.unit('tests.p.test_slow', timeout=3)
        self.assertLess(time.time() - t, 30)
        self.assertEqual(status, 'TIMEOUT', text)
        self.assertIn('pytests: TIMEOUT - tests.p.test_slow still running after 3s', text)
        self.unit('tests.p.test_pid')
        self.assertNotEqual(self.read('slow.pid'), self.read('tests.p.test_pid.pid'))
        self.assertEqual(self.pool.stats(), (2, {'timeout': 1}))

    def test_a_worker_that_crashes_fails_its_unit_and_is_replaced(self):
        self.unit('tests.p.test_pid')
        write(self.root, {'tests/p/test_crash.py': unit_body("        os._exit(3)\n")})
        _u, status, _ran, _s, text = self.unit('tests.p.test_crash')
        self.assertEqual(status, 'FAILED', text)
        self.assertIn('running tests.p.test_crash ended', text)
        self.assertIn('exit 3 (0x00000003)', text)
        self.unit('tests.p.test_pid2')
        self.assertNotEqual(self.read('tests.p.test_pid.pid'), self.read('tests.p.test_pid2.pid'))
        self.assertEqual(self.pool.stats(), (2, {'ended during a unit': 1}))

    def test_ctrl_c_inside_a_unit_ends_its_worker(self):
        write(self.root, {'tests/p/test_ctrlc.py': unit_body("        raise KeyboardInterrupt\n")})
        _u, status, _ran, _s, text = self.unit('tests.p.test_ctrlc')
        self.assertEqual(status, 'FAILED', text)
        self.assertIn('KeyboardInterrupt', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (2, {'Ctrl+C': 1}))

    def test_ctrl_c_in_the_runner_ends_the_worker_of_the_unit_it_waits_for(self):
        from kitlib.proc import process
        write(self.root, {'tests/p/test_wait.py': unit_body(
            "        with open(os.path.join(os.environ['PROBE_OUT'], 'wait.pid'), 'w') as fh:\n"
            "            fh.write(str(os.getpid()))\n        time.sleep(30)\n")})
        timer = threading.Timer(2.0, _thread.interrupt_main)     # Ctrl+C, as the console
        timer.start()
        self.addCleanup(timer.cancel)
        t = time.time()
        with self.assertRaises(KeyboardInterrupt):
            self.unit('tests.p.test_wait')
        self.assertLess(time.time() - t, 15)
        self.assertFalse(process.alive(self.read('wait.pid'), None))
        self.assertEqual(self.pool.stats(), (1, {'interrupted': 1}))

    def test_a_process_the_unit_left_running_ends_with_its_worker(self):
        from kitlib.proc import process
        write(self.root, {'tests/p/test_orphan.py': unit_body(
            "        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "        with open(os.path.join(os.environ['PROBE_OUT'], 'orphan.pid'), 'w') as fh:\n"
            "            fh.write(str(p.pid))\n", imports='import subprocess')})
        _u, status, _ran, _s, text = self.unit('tests.p.test_orphan')
        self.assertEqual(status, 'OK', text)
        self.assertFalse(process.alive(self.read('orphan.pid'), None))
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (2, {'processes it started still running': 1}))
        self.assertEqual(self.pool.why_replaced(),
                         [('tests.p.test_orphan', 'processes it started still running')])

    def slow_to_end(self, delay):
        """The idle worker's job made to hold, too, a process that ends `delay` seconds after the
        job is terminated: terminating a job only starts each process's end, and one with much to
        tear down ends after the worker. As a real job does, it is no longer listed once the job is
        terminated. (It is outside the job: a real one ends with the rest.)"""
        self.unit('tests.p.test_pid')
        job = self.pool.idle[0].job
        slow = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(slow.wait)
        self.addCleanup(slow.kill)
        terminate, pids, terminated = job.terminate, job.pids, []

        def slow_terminate(code=1):
            terminated.append(code)
            timer = threading.Timer(delay, slow.kill)
            timer.start()
            self.addCleanup(timer.cancel)
            return terminate(code)
        job.terminate = slow_terminate
        job.pids = lambda: pids() + ([] if terminated or slow.poll() is not None else [slow.pid])
        return slow

    def test_ending_a_worker_waits_until_every_process_in_its_job_has_ended(self):
        from kitlib.proc import process
        slow = self.slow_to_end(1.0)
        t = time.time()
        self.pool.close()
        self.assertFalse(process.alive(slow.pid, None))     # Popen's handle keeps its pid
        self.assertLess(time.time() - t, 15)

    def test_a_process_in_the_job_that_does_not_end_holds_up_its_worker_s_end_gone_wait_at_most(
            self):
        from kitlib.proc import process
        slow = self.slow_to_end(60)
        with mock.patch.object(pool, 'GONE_WAIT', 1.0):
            t = time.time()
            self.pool.close()
            self.assertLess(time.time() - t, 15)
        self.assertTrue(process.alive(slow.pid, None))

    def test_a_thread_the_unit_left_running_fails_it_and_retires_its_worker(self):
        # As its own process the unit would never exit (a non-daemon thread holds it open) and
        # would time out; in a worker it FAILS rather than passing.
        write(self.root, {'tests/p/test_thread.py': unit_body(
            "        threading.Thread(target=time.sleep, args=(30,), name='left-over').start()\n",
            imports='import threading')})
        _u, status, ran, _s, text = self.unit('tests.p.test_thread')
        self.assertEqual((status, ran), ('FAILED', 1), text)
        self.assertIn('kit_tests: tests.p.test_thread left a non-daemon thread running', text)
        self.assertIn('left-over', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (2, {'threads still running: left-over': 1}))
        self.assertEqual(self.pool.why_replaced(),
                         [('tests.p.test_thread', 'threads still running: left-over')])

    def test_a_daemon_thread_the_unit_left_running_retires_its_worker_only(self):
        # A daemon thread does not keep a process open: the unit passes, the worker is not reused.
        write(self.root, {'tests/p/test_daemon.py': unit_body(
            "        threading.Thread(target=time.sleep, args=(30,), name='daemon-left',\n"
            "                         daemon=True).start()\n", imports='import threading')})
        _u, status, _ran, _s, text = self.unit('tests.p.test_daemon')
        self.assertEqual(status, 'OK', text)
        self.assertNotIn('non-daemon', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (2, {'threads still running: daemon-left': 1}))

    def test_a_worker_that_ended_while_idle_is_replaced_without_blaming_the_next_unit(self):
        self.unit('tests.p.test_pid')
        idle = self.pool.idle[0]
        idle.job.terminate()
        idle.proc.wait(30)
        _u, status, ran, _s, text = self.unit('tests.p.test_pid2')
        self.assertEqual((status, ran), ('OK', 1), text)
        self.assertNotEqual(self.read('tests.p.test_pid.pid'), self.read('tests.p.test_pid2.pid'))
        self.assertEqual(self.pool.stats(), (2, {'ended while idle': 1}))
        self.assertEqual(self.pool.why_replaced(), [('tests.p.test_pid', 'ended while idle')])

    def test_a_worker_whose_job_cannot_be_queried_is_retired(self):
        self.unit('tests.p.test_pid')
        w = self.pool.idle[0]

        def denied():
            raise OSError(5, 'Access is denied')
        w.job.pids = denied
        _u, status, _ran, _s, text = self.unit('tests.p.test_pid2')
        self.assertEqual(status, 'OK', text)
        self.assertEqual(self.read('tests.p.test_pid.pid'), self.read('tests.p.test_pid2.pid'))
        self.assertEqual(self.pool.stats(), (1, {'its job could not be queried': 1}))
        self.assertEqual(self.pool.idle, [])

    def test_a_worker_that_is_not_ready_in_time_is_ended_and_its_unit_fails(self):
        from kitlib.proc import process
        stall = os.path.join(self.root, 'stall.py')
        write(self.root, {'stall.py': (
            "import os, time\n"
            "with open(os.path.join(os.environ['PROBE_OUT'], 'stall.pid'), 'w') as fh:\n"
            "    fh.write(str(os.getpid()))\n"
            "time.sleep(60)\n")})
        with mock.patch.object(pool, 'WORKER', stall), mock.patch.object(pool, 'READY_TIMEOUT', 5):
            _u, status, ran, _s, text = self.unit('tests.p.test_pid')
        self.assertEqual((status, ran), ('FAILED', 0), text)
        self.assertIn('a pool worker was not ready after 5s', text)
        self.assertFalse(process.alive(self.read('stall.pid'), None))
        self.assertEqual(self.pool.all, [])

    def test_a_unit_that_adds_an_audit_hook_retires_its_worker(self):
        write(self.root, {'tests/p/test_hook.py': unit_body(
            "        sys.addaudithook(lambda event, args: None)\n")})
        _u, status, _ran, _s, text = self.unit('tests.p.test_hook')
        self.assertEqual(status, 'OK', text)
        self.unit('tests.p.test_pid')
        self.assertEqual(self.pool.stats(), (2, {'an audit hook was added': 1}))

    def test_an_audit_hook_a_preloaded_package_adds_is_part_of_every_unit_s_start(self):
        # tests/__init__.py's guard: imported once per worker, it retires none.
        write(self.root, {'tests/__init__.py': (
            "import os\nimport sys\n\n\ndef _seen(event, args):\n"
            "    if event == 'kit.probe':\n        args[0].append(os.getpid())\n\n\n"
            "sys.addaudithook(_seen)\n"),
            'tests/p/test_probe.py': unit_body(
                "        seen = []\n        sys.audit('kit.probe', seen)\n"
                "        self.assertEqual(seen, [os.getpid()])\n")})
        self.pool = pool.WorkerPool(self.root, self.folder, self.env, preload=['tests'])
        self.addCleanup(self.pool.close)
        for name in ('tests.p.test_probe', 'tests.p.test_pid', 'tests.p.test_probe'):
            _u, status, ran, _s, text = self.unit(name)
            self.assertEqual((status, ran), ('OK', 1), text)    # the hook is there, once
        self.assertEqual(self.pool.stats(), (1, {}))

    def test_a_preloaded_package_that_does_not_import_fails_the_units_that_need_it(self):
        write(self.root, {'tests/__init__.py': 'import no_such_module_anywhere\n'})
        self.pool = pool.WorkerPool(self.root, self.folder, self.env, preload=['tests'])
        self.addCleanup(self.pool.close)
        _u, status, _ran, _s, text = self.unit('tests.p.test_pid')
        self.assertEqual(status, 'FAILED', text)
        self.assertIn("No module named 'no_such_module_anywhere'", text)

    def test_close_ends_every_worker(self):
        from kitlib.proc import process
        self.unit('tests.p.test_pid')
        pid = self.read('tests.p.test_pid.pid')
        self.assertTrue(process.alive(pid, None))            # kept for the next unit
        self.pool.close()
        self.assertFalse(process.alive(pid, None))


if __name__ == '__main__':
    unittest.main()
