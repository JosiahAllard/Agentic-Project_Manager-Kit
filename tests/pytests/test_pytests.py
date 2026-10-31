"""kitlib.pytests and tools/pytests.py: the parallel tools-test runner. Ported
from the PM kit's test_pytests.py: the checkout root is passed explicitly
(the PM kit patched pytests.ROOT and set KIT_REPO_ROOT / KIT_CONFIG), the job is kitlib.proc.job's,
and a fail pattern stands in for the PM kit's gate.FAIL (the kit's fail patterns are per-repo
gate config).

Every unit here is a fake test module written to a temp folder and run through the real child
path (`tools/pytests.py --child <m> -C <root> --path <folder>`), so what is proved is what a gate
step runs. The audit cases run against a fixture checkout (`.kit.json` "tool_tests" tables, a tools
folder), never the kit's or a real repository's tree.
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from kitlib.config import ConfigError, load as load_config
from kitlib.proc import job as job_mod
from kitlib.proc import shield as pid_shield
from kitlib.proc import win32
from kitlib.pytests import child, runner
from kitlib.tooldeps import deps as tool_deps
from tests.gitfixture import TempRepo, rmtree, stale_local_main
from tests.testutil import SHIELDED, ancestor_reach

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_pytests_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'pytests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# What a gate step's `fail` pattern would catch (the PM kit's gate.FAIL; the kit has none built in).
RED = re.compile(r'(?i)\bfail')


def alive(pid):
    """Whether process `pid` is running. Test-only: the runner itself never looks a pid up."""
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x00100000 | 0x1000, False, pid)   # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return k.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102      # WAIT_TIMEOUT
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def wait_dead(pid, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


def wait_for_file(path, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(path) and os.path.getsize(path):
            with open(path) as fh:
                return fh.read()
        time.sleep(0.05)
    raise AssertionError('%s never appeared' % path)


def k32():
    import ctypes
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    lib.OpenProcess.restype = ctypes.c_void_p
    return lib


def cfg(**over):
    """Runner settings: the schema's defaults with `over` applied, checked as `.kit.json` would be."""
    return runner.settings(data=over)


class Fakes(unittest.TestCase):
    """A folder of fake test modules, run through the real child path. The folder is also the
    checkout root the units run in (it has no `.kit.json`: every setting is a default)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pytests_test_')
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.root = self.dir

    def write(self, name, text):
        with open(os.path.join(self.dir, name + '.py'), 'w', encoding='utf-8') as fh:
            fh.write(textwrap.dedent(text))

    def run_modules(self, modules, **kw):
        out = io.StringIO()
        code = runner.run(modules, self.root, out=out, path=self.dir, **kw)
        return code, out.getvalue()

    def headers(self, text):
        return [l for l in text.splitlines() if l.startswith('==== ')]


PASSING = '''
import unittest

class Passing(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass
'''


class Verdicts(Fakes):

    def test_passing_units_exit_0_and_end_ok(self):
        self.write('test_p1', PASSING)
        self.write('test_p2', PASSING)
        code, out = self.run_modules(['test_p1', 'test_p2'])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], 'OK')
        self.assertIn('Ran 4 tests in ', out.splitlines()[-2])
        self.assertIn('==== test_p1 (2 tests, ', out)

    def test_a_failing_unit_exits_1_names_it_and_a_fail_pattern_matches(self):
        self.write('test_p1', PASSING)
        self.write('test_bad', '''
            import unittest

            class Bad(unittest.TestCase):
                def test_fails(self):
                    self.assertEqual(1, 2)
            ''')
        code, out = self.run_modules(['test_p1', 'test_bad'])
        self.assertEqual(code, 1, out)
        last = out.splitlines()[-1]
        self.assertEqual(last, 'FAILED (units: test_bad)')
        self.assertTrue(RED.search(last))
        self.assertIn('==== test_bad (1 tests, ', out)
        self.assertIn(', FAILED) ====', out)
        self.assertNotIn('no unittest summary', out)     # it has one: no exit-code note

    def test_a_module_that_cannot_import_fails(self):
        self.write('test_broken', 'import no_such_module_anywhere\n')
        code, out = self.run_modules(['test_broken'])
        self.assertEqual(code, 1, out)
        self.assertIn('FAILED (units: test_broken)', out)
        self.assertIn('no_such_module_anywhere', out)

    def test_a_unit_that_dies_without_output_is_named_with_its_exit_code(self):
        # The log once said only `(0 tests, 0s, FAILED)`; now it says how.
        self.write('test_silent', 'import os\nos._exit(1)\n')
        code, out = self.run_modules(['test_silent'])
        self.assertEqual(code, 1, out)
        self.assertIn('==== test_silent (0 tests, ', out)
        self.assertRegex(out, r'pytests: test_silent ran as pid \d+ and exited 1 \(0x00000001\) '
                              r'[\d.]+s after it started, with no unittest summary and no output '
                              r'at all: no test ran')

    def test_a_unit_that_runs_no_test_fails_named(self):
        # an empty unit proves nothing.
        self.write('test_p1', PASSING)
        self.write('test_empty', 'import unittest\n')
        code, out = self.run_modules(['test_p1', 'test_empty'])
        self.assertEqual(code, 1, out)
        self.assertIn('==== test_empty (0 tests, ', out)
        self.assertIn('pytests: test_empty ran no test; an empty unit is a FAIL', out)
        self.assertEqual(out.splitlines()[-1], 'FAILED (units: test_empty)')

    def test_an_expected_failure_is_ok(self):
        self.write('test_xfail', '''
            import unittest

            class X(unittest.TestCase):
                @unittest.expectedFailure
                def test_known(self):
                    self.assertTrue(False)
            ''')
        code, out = self.run_modules(['test_xfail'])
        self.assertEqual(code, 0, out)

    def test_a_skipped_test_adds_a_py_skip_line(self):
        self.write('test_skips', '''
            import unittest

            class S(unittest.TestCase):
                def test_runs(self):
                    pass

                @unittest.skip('needs   the\\nengine')
                def test_skipped(self):
                    pass
            ''')
        code, out = self.run_modules(['test_skips'])
        self.assertEqual(code, 0, out)
        self.assertIn('PY SKIP test_skips.S.test_skipped needs the engine', out.splitlines())

    def test_a_setupclass_skip_gives_a_one_token_id(self):
        # The fixture's id is `setUpClass (test_cls_skip.S)`; a gate reads the first word after
        # `PY SKIP ` as the id, so the space becomes `_` and the reason stays separate.
        self.write('test_cls_skip', '''
            import unittest

            class Runs(unittest.TestCase):
                def test_runs(self):
                    pass

            class S(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest('no engine')

                def test_never(self):
                    pass
            ''')
        code, out = self.run_modules(['test_cls_skip'])
        self.assertEqual(code, 0, out)
        self.assertIn('PY SKIP setUpClass_(test_cls_skip.S) no engine', out.splitlines())

    def test_output_is_in_sorted_unit_order_whatever_finishes_first(self):
        self.write('test_z_fast', PASSING)
        self.write('test_a_slow', PASSING.replace('pass\n\n    def test_two',
                                                  '__import__("time").sleep(2)\n\n    def test_two'))
        code, out = self.run_modules(['test_z_fast', 'test_a_slow'], jobs=2)
        self.assertEqual(code, 0, out)
        self.assertEqual([h.split()[1] for h in self.headers(out)], ['test_a_slow', 'test_z_fast'])

    def test_every_class_of_a_sharded_module_runs_exactly_once(self):
        body = 'import unittest\n'
        for i in range(7):
            body += textwrap.dedent('''
                class C%d(unittest.TestCase):
                    def test_it(self):
                        print('EX-RAN-C%d')
                ''' % (i, i))
        self.write('test_many', body)
        code, out = self.run_modules(['test_many'], cfg=cfg(shards={'test_many': 3}))
        self.assertEqual(code, 0, out)
        self.assertEqual([h.split()[1] for h in self.headers(out)],
                         ['test_many[1/3]', 'test_many[2/3]', 'test_many[3/3]'])
        ran = sorted(re.findall(r'EX-RAN-C\d', out))
        self.assertEqual(ran, ['EX-RAN-C%d' % i for i in range(7)])

    def test_split_classes_runs_one_unit_per_class(self):
        self.write('test_two_classes', '''
            import unittest

            class A(unittest.TestCase):
                def test_a(self):
                    pass

            class B(unittest.TestCase):
                def test_b(self):
                    pass
            ''')
        self.write('test_broken', 'import no_such_module_anywhere\n')
        code, out = self.run_modules(['test_two_classes', 'test_broken'], cfg=cfg(split_classes=True))
        self.assertEqual(code, 1, out)          # the broken module, whole and named
        self.assertEqual([h.split()[1] for h in self.headers(out)],
                         ['test_broken', 'test_two_classes.A', 'test_two_classes.B'])
        self.assertEqual(out.splitlines()[-1], 'FAILED (units: test_broken)')

    def test_split_classes_off_by_default_keeps_a_module_whole(self):
        self.write('test_two_classes', PASSING + '\nclass More(unittest.TestCase):\n'
                                                 '    def test_m(self):\n        pass\n')
        code, out = self.run_modules(['test_two_classes'])
        self.assertEqual(code, 0, out)
        self.assertEqual([h.split()[1] for h in self.headers(out)], ['test_two_classes'])

    def test_a_unit_runs_with_auto_gc_and_maintenance_off(self):
        # The child sets it itself (kitlib.git.batch.fixture_env), so a
        # module that sets nothing still makes no `maintenance run --auto` per fixture commit.
        self.write('test_env', 'import os, unittest\nclass E(unittest.TestCase):\n'
                               '    def test_e(self):\n'
                               '        n = int(os.environ.get("GIT_CONFIG_COUNT", "0"))\n'
                               '        print(sorted("CFG=%s=%s" % (os.environ["GIT_CONFIG_KEY_%d" % i],'
                               ' os.environ["GIT_CONFIG_VALUE_%d" % i]) for i in range(n)))\n')
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith('GIT_CONFIG_') and k != 'GIT_CONFIG_COUNT'}
        with mock.patch.dict(os.environ, clean, clear=True):
            code, out = self.run_modules(['test_env'])
        self.assertEqual(code, 0, out)
        self.assertIn("['CFG=gc.auto=0', 'CFG=maintenance.auto=false']", out)

    def test_each_unit_is_a_fresh_process(self):
        self.write('test_pid1', 'import os, unittest\nclass A(unittest.TestCase):\n'
                                '    def test_p(self):\n        print("PID=%d" % os.getpid())\n')
        self.write('test_pid2', 'import os, unittest\nclass B(unittest.TestCase):\n'
                                '    def test_p(self):\n        print("PID=%d" % os.getpid())\n')
        code, out = self.run_modules(['test_pid1', 'test_pid2'], jobs=1)
        self.assertEqual(code, 0, out)
        pids = [l for l in out.splitlines() if l.startswith('PID=')]
        self.assertEqual(len(set(pids)), 2, out)

    def test_the_tools_folder_is_on_the_path_and_the_entry_points_folder_is_not(self):
        # A unit imports the checkout's tools as discover had them; the kit's own tools folder
        # (where the child's entry point lives) must not shadow a same-named repository tool.
        os.makedirs(os.path.join(self.root, 'tools'))
        with open(os.path.join(self.root, 'tools', 'testlist.py'), 'w', encoding='utf-8') as fh:
            fh.write('WHOSE = "repo"\n')
        self.write('test_whose', '''
            import unittest
            import testlist

            class W(unittest.TestCase):
                def test_w(self):
                    self.assertEqual(testlist.WHOSE, 'repo')
            ''')
        code, out = self.run_modules(['test_whose'])
        self.assertEqual(code, 0, out)


class Timeouts(Fakes):
    """The tree kill reaches the unit's own processes and nothing else."""

    def test_a_hung_unit_is_killed_with_its_grandchild_and_reported_timeout(self):
        pidfile = os.path.join(self.dir, 'grandchild.pid')
        # The unit reports its own shield first: TerminateJobObject must end a shielded unit.
        self.write('test_hang', SHIELDED + textwrap.dedent('''
            import os, subprocess, sys, time, unittest

            class Hang(unittest.TestCase):
                def test_hang(self):
                    print('SHIELDED=%%s' %% shielded(), flush=True)
                    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
                    with open(%r, 'w') as fh:
                        fh.write(str(p.pid))
                    time.sleep(600)
            ''' % pidfile))
        # An unrelated process, started outside the unit: it must survive the kill.
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        t = time.time()
        code, out = self.run_modules(['test_hang'], timeout=8)
        self.assertLess(time.time() - t, 60, out)
        self.assertEqual(code, 1, out)
        self.assertIn('==== test_hang (0 tests, ', out)
        self.assertIn(', TIMEOUT) ====', out)
        self.assertIn('pytests: TIMEOUT - test_hang still running after 8s', out)
        self.assertIn('SHIELDED=True', out)
        self.assertEqual(out.splitlines()[-1], 'FAILED (units: test_hang)')
        with open(pidfile) as fh:
            grandchild = int(fh.read())
        self.assertTrue(wait_dead(grandchild), 'the unit\'s grandchild survived the timeout kill')
        self.assertIsNone(bystander.poll(), 'the timeout kill reached a process it did not start')

    def test_the_configured_unit_timeout_applies_without_the_flag(self):
        self.write('test_hang3', '''
            import time, unittest

            class Hang(unittest.TestCase):
                def test_hang(self):
                    time.sleep(600)
            ''')
        code, out = self.run_modules(['test_hang3'], cfg=cfg(unit_timeout=5))
        self.assertEqual(code, 1, out)
        self.assertIn('pytests: TIMEOUT - test_hang3 still running after 5s', out)

    def test_the_timeout_kill_goes_through_the_job_and_nothing_else(self):
        # A bystander that a `taskkill /T` on the unit's pid would reach but the job does not hold
        # cannot be built: the child is created suspended and assigned before it runs, so every
        # descendant inherits the job (breakaway is not allowed). So this pins the mechanism
        # instead: the kill is TerminateJobObject, and no pid-based kill (os.kill, taskkill
        # through subprocess, Popen.kill/terminate) is used.
        self.write('test_hang2', '''
            import time, unittest

            class Hang(unittest.TestCase):
                def test_hang(self):
                    time.sleep(600)
            ''')
        calls = []
        real_job = runner.Job

        class RecordingJob(real_job):
            def terminate(self, code=1):
                calls.append('TerminateJobObject')
                return super().terminate(code)

        forbid = lambda name: mock.Mock(side_effect=lambda *a, **k: calls.append(name))  # noqa: E731
        with mock.patch.object(runner, 'Job', RecordingJob), \
                mock.patch.object(os, 'kill', forbid('os.kill')), \
                mock.patch.object(subprocess, 'run', forbid('subprocess.run')), \
                mock.patch.object(subprocess, 'call', forbid('subprocess.call')), \
                mock.patch.object(subprocess.Popen, 'kill', forbid('Popen.kill')), \
                mock.patch.object(subprocess.Popen, 'terminate', forbid('Popen.terminate')):
            code, out = self.run_modules(['test_hang2'], timeout=5)
        self.assertEqual(code, 1, out)
        self.assertIn(', TIMEOUT) ====', out)
        self.assertEqual(calls, ['TerminateJobObject'])

    def test_a_unit_and_what_it_starts_at_import_are_in_its_own_job(self):
        # Created suspended, assigned, then resumed - so even a process the unit starts at import
        # time is inside, and the runner (this process) is not.
        self.write('test_injob', '''
            import ctypes, os, subprocess, sys, unittest
            from ctypes import wintypes

            EARLY = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])

            def job_pids():
                class L(ctypes.Structure):
                    _fields_ = [('assigned', wintypes.DWORD), ('listed', wintypes.DWORD),
                                ('pids', ctypes.c_size_t * 64)]
                info = L()
                k32 = ctypes.WinDLL('kernel32', use_last_error=True)
                k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                          ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
                assert k32.QueryInformationJobObject(None, 3, ctypes.byref(info),
                                                     ctypes.sizeof(info), None)
                return list(info.pids[:info.listed])

            class InJob(unittest.TestCase):
                def test_where(self):
                    print('SELF=%d EARLY=%d JOB=%s' % (os.getpid(), EARLY.pid, job_pids()))
                    EARLY.kill()
                    EARLY.wait()
            ''')
        code, out = self.run_modules(['test_injob'])
        self.assertEqual(code, 0, out)
        m = re.search(r'SELF=(\d+) EARLY=(\d+) JOB=\[([\d, ]*)\]', out)
        self.assertTrue(m, out)
        pids = {int(p) for p in m.group(3).split(',') if p.strip()}
        self.assertIn(int(m.group(1)), pids)
        self.assertIn(int(m.group(2)), pids)
        self.assertNotIn(os.getpid(), pids)

    def test_a_leftover_holding_the_pipe_is_killed_after_linger(self):
        pidfile = os.path.join(self.dir, 'leftover.pid')
        self.write('test_leftover', '''
            import subprocess, sys, unittest

            class Leave(unittest.TestCase):
                def test_leave(self):
                    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],
                                         stdout=sys.stdout, stderr=sys.stderr)
                    with open(%r, 'w') as fh:
                        fh.write(str(p.pid))
            ''' % pidfile)
        with mock.patch.object(runner, 'LINGER', 3):
            t = time.time()
            code, out = self.run_modules(['test_leftover'])
        self.assertLess(time.time() - t, 60, out)
        self.assertEqual(code, 0, out)          # the tests passed; the note says what was killed
        self.assertIn('still held its output 3s after it exited; killed it', out)
        with open(pidfile) as fh:
            self.assertTrue(wait_dead(int(fh.read())))


class TheShield(Fakes):
    """A unit's own process refuses a kill by its pid (shield_unit) - a
    stale `taskkill /PID <n> /T /F` meant for a dead shell cannot end it - while the runner's job
    still does (TerminateJobObject: Timeouts above; KILL_ON_JOB_CLOSE: here)."""

    def test_a_unit_refuses_a_kill_by_its_pid(self):
        import ctypes
        pidfile = os.path.join(self.dir, 'unit.pid')
        release = os.path.join(self.dir, 'release')
        self.write('test_shielded', '''
            import os, time, unittest

            class S(unittest.TestCase):
                def test_wait(self):
                    with open(%r, 'w') as fh:
                        fh.write(str(os.getpid()))
                    end = time.time() + 60
                    while not os.path.exists(%r) and time.time() < end:
                        time.sleep(0.05)
                    self.assertTrue(os.path.exists(%r), 'never released')
            ''' % (pidfile, release, release))
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            zip(('code', 'out'), self.run_modules(['test_shielded']))))
        thread.start()
        try:
            pid = int(wait_for_file(pidfile))
            # The unit waits for `release` and the runner holds its handle: the pid is the unit's.
            k = k32()
            h = k.OpenProcess(0x0001, False, pid)                       # PROCESS_TERMINATE
            err = ctypes.get_last_error()
            if h:
                k.CloseHandle(ctypes.c_void_p(h))
            self.assertFalse(h, 'the unit could be opened for PROCESS_TERMINATE by its pid')
            self.assertEqual(err, 5)                                    # ERROR_ACCESS_DENIED
            # The killer itself, the way Claude Code runs it on a shell's pid.
            tk = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
            self.assertNotEqual(tk.returncode, 0, tk.stdout)
            self.assertTrue(alive(pid), 'taskkill ended the unit')
        finally:
            with open(release, 'w') as fh:
                fh.write('go')
            thread.join(120)
        self.assertEqual(result.get('code'), 0, result.get('out'))

    def test_a_unit_dies_with_its_runner(self):
        # The orphan case: nothing can end a shielded unit by its pid any more, so when the runner
        # is killed abruptly its job's KILL_ON_JOB_CLOSE must.
        import ctypes
        pidfile = os.path.join(self.dir, 'unit.pid')
        self.write('test_orphan', SHIELDED + textwrap.dedent('''
            import os, time, unittest

            class O(unittest.TestCase):
                def test_wait(self):
                    with open(%r, 'w') as fh:
                        fh.write('%%d %%s' %% (os.getpid(), shielded()))
                    time.sleep(120)
            ''' % pidfile))
        script = os.path.join(self.dir, 'runner_script.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write('import sys\nsys.path.insert(0, %r)\nfrom kitlib.pytests import runner\n'
                     'sys.exit(runner.run(["test_orphan"], %r, path=%r))\n'
                     % (KIT, self.root, self.dir))
        proc = subprocess.Popen([sys.executable, script], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        pid, shielded = wait_for_file(pidfile).split()
        self.assertEqual(shielded, 'True')
        k = k32()
        h = k.OpenProcess(0x00100000 | 0x1000, False, int(pid))   # SYNCHRONIZE | QUERY_LIMITED
        self.assertTrue(h, 'the unit is not running')
        try:
            # Alive, and from here pinned: our handle keeps its pid from being reused.
            self.assertEqual(k.WaitForSingleObject(ctypes.c_void_p(h), 0), 0x102)  # WAIT_TIMEOUT
            proc.kill()                     # abrupt: TerminateProcess on the runner's own handle
            proc.wait(30)
            self.assertEqual(k.WaitForSingleObject(ctypes.c_void_p(h), 30000), 0,
                             'the unit outlived its runner')
        finally:
            k.CloseHandle(ctypes.c_void_p(h))

    def test_a_unit_that_cannot_be_shielded_still_runs_and_says_so(self):
        self.write('test_p1', PASSING)
        with mock.patch.object(runner, 'shield_unit',
                               mock.Mock(side_effect=OSError('SetSecurityInfo returned 5'))):
            code, out = self.run_modules(['test_p1'])
        self.assertEqual(code, 0, out)
        note = [l for l in out.splitlines() if l.startswith('pytests: could not shield')]
        self.assertEqual(note, ['pytests: could not shield test_p1 from kills by pid '
                                '(SetSecurityInfo returned 5); it ran unshielded'])
        self.assertIsNone(RED.search(note[0]))       # a passing unit's note is no red line

    def test_a_null_dacl_is_left_alone_and_the_unit_runs_unshielded(self):
        # A NULL DACL grants everyone everything; merging the deny entry into it would leave an ACL
        # holding only that entry. GetSecurityInfo is made to report one: no ACL may be written.
        # The unit reads its DACL only once an outer tree's shield of every new process is gone.
        import ctypes
        self.write('test_nulldacl', SHIELDED + textwrap.dedent('''
            import time, unittest

            class N(unittest.TestCase):
                def test_n(self):
                    time.sleep(REACH)
                    print('SHIELDED=%s' % shielded())
            ''').replace('REACH', repr(ancestor_reach())))
        null = mock.Mock(return_value=(ctypes.c_void_p(None), ctypes.c_void_p(None)))
        with mock.patch.object(runner, 'process_dacl', null):
            code, out = self.run_modules(['test_nulldacl'])
        self.assertEqual(code, 0, out)
        self.assertEqual(null.call_count, 1, out)
        self.assertIn('SHIELDED=False', out)               # nothing was written to its DACL
        note = [l for l in out.splitlines() if l.startswith('pytests: could not shield')]
        self.assertEqual(note, ['pytests: could not shield test_nulldacl from kills by pid (its '
                                'process has a NULL DACL; left as it is); it ran unshielded'])
        self.assertIsNone(RED.search(note[0]))


class ThePidHold(Fakes):
    """runner.PidHoldRun, the opt-in `--pid-hold`: the shield covers a unit's own process only; the
    run's pid hold covers what the unit starts (a test's git), so a stale taskkill cannot end it
    by a reused pid. Off unless asked; a hold that fails says so plainly and the run goes on."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)       # publish() writes HOLD_ENV; put it back after
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(pid_shield.HOLD_ENV, None)

    def hold_lines(self, text):
        return [l for l in text.splitlines() if 'pid hold' in l]

    def test_a_run_makes_no_hold_unless_asked(self):
        made = []
        self.write('test_ok', PASSING)
        with mock.patch.object(pid_shield, 'hold', lambda: made.append(1)):
            code, text = self.run_modules(['test_ok'])
        self.assertEqual((code, made, self.hold_lines(text)), (0, [], []), text)
        self.assertNotIn(pid_shield.HOLD_ENV, os.environ)

    def test_what_a_unit_starts_runs_under_the_runs_published_hold(self):
        held = pid_shield.PidHold()
        self.addCleanup(held.close)
        report = os.path.join(self.dir, 'unit.json')
        self.write('test_grandchild', textwrap.dedent('''
            import json, os, subprocess, sys, unittest
            from kitlib.proc import shield

            class G(unittest.TestCase):
                def test_starts_a_child(self):
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.5)'])
                    child.wait()
                    with open(%r, 'w') as fh:
                        json.dump({'beacon': os.environ.get(shield.HOLD_ENV),
                                   'inherited': shield.inherited(), 'child': child.pid}, fh)
            ''' % report))
        with mock.patch.object(runner, 'WARM', 0.2):
            code, text = self.run_modules(['test_grandchild'],
                                          hold=runner.PidHoldRun(make=lambda: held))
        self.assertEqual(code, 0, text)
        with open(report) as fh:
            got = json.load(fh)
        self.assertEqual((got['beacon'], got['inherited']), (held.publish(), True), text)
        self.assertIn(got['child'], [pid for _s, pid, what in held.record() if what == 'held'])
        lines = text.splitlines()
        self.assertEqual(self.hold_lines(text), ['pytests: pid hold: on (warmed 0.2 s, published)',
                                                 'pytests: pid hold: held to the end of the run'])
        # The end line comes before the floor line, so the last line stays OK / FAILED.
        self.assertLess(lines.index('pytests: pid hold: held to the end of the run'),
                        [i for i, l in enumerate(lines) if l.startswith('pysel floor')][0])
        self.assertEqual(lines[-1], 'OK')

    def test_a_run_under_an_inherited_hold_makes_none(self):
        made, still = [], [True]
        run = runner.PidHoldRun(make=lambda: made.append(1), inherited=lambda: still[0])
        self.assertEqual(run.start(), 'pid hold: on (a running hold this process inherited)')
        self.assertEqual(made, [])
        self.assertEqual(run.end(), 'pid hold: held to the end of the run (inherited)')
        still[0] = False
        self.assertEqual(run.end(), 'pid hold: FAILED during the run, the inherited hold ended: '
                                    'units after it ended were NOT held')

    def test_a_hold_that_cannot_be_made_says_so_and_the_run_goes_on_unheld(self):
        class Stopped(object):
            def warm(self, seconds):
                raise OSError('the pid hold stopped (OSError: K32EnumProcesses returned error 5)')

        def unmade():
            raise OSError('no handle')

        def no_thread():                # not an OSError: once a crash, now reported the same way
            raise RuntimeError("can't start new thread")
        self.write('test_ok', PASSING)
        for make, why in ((unmade, 'OSError: no handle'),
                          (Stopped, 'OSError: the pid hold stopped (OSError: K32EnumProcesses '
                                    'returned error 5)'),
                          (no_thread, "RuntimeError: can't start new thread")):
            with self.subTest(why=why):
                code, text = self.run_modules(['test_ok'], hold=runner.PidHoldRun(
                    make=make, inherited=lambda: False))
                self.assertEqual(code, 0, text)
                self.assertEqual(self.hold_lines(text), [
                    'pytests: pid hold: FAILED, this run is NOT held (%s): a stale taskkill can '
                    'still end a process a test starts' % why,
                    'pytests: pid hold: FAILED at the start, this run was NOT held (%s)' % why])
                self.assertEqual(text.splitlines()[-1], 'OK')
                self.assertNotIn(pid_shield.HOLD_ENV, os.environ)

    def test_a_hold_that_stops_during_the_run_says_so_at_the_end(self):
        class Held(object):
            stopped, error = False, None

            def warm(self, seconds):
                pass

            def publish(self):
                return 'beacon'
        held = Held()
        run = runner.PidHoldRun(make=lambda: held, inherited=lambda: False)
        self.assertEqual(run.start(), 'pid hold: on (warmed 3 s, published)')
        held.stopped, held.error = True, 'OSError: K32EnumProcesses returned error 5'
        self.assertEqual(run.end(), 'pid hold: FAILED during the run, units after it stopped were '
                                    'NOT held (OSError: K32EnumProcesses returned error 5)')

    def test_the_gate_refuses_the_same_flag(self):
        from kitlib.gate import steps
        self.assertEqual(steps.PID_HOLD_FLAG, runner.PID_HOLD_FLAG)


# The fixture checkout the audit reads: its tables are what a repository's tables held for the
# same cases (a pinned doc, a README read by backlog's tests, a whole-tree class).
AUDIT_TABLES = {
    'pinned': [['docs/reference/BACKLOG_STORE.md', ['test_backlog']]],
    'extra': {'README.md': ['backlog']},
    'whole_tree': {'test_machine_paths.TheRealTree': 'reads every tracked file on purpose'},
}
AUDIT_FILES = {
    'tools/backlog.py': 'X = 1\n',
    'tools/test_backlog.py': 'import unittest\nimport backlog\n\nclass T(unittest.TestCase):\n'
                             '    def test_x(self):\n        pass\n',
    'tools/gate.py': 'import tool_deps\n',
    'tools/tool_deps.py': 'X = 1\n',
    'tools/codemap.py': 'X = 1\n',
    'tools/test_exit_status.py': 'import unittest\nimport gate\n\nclass T(unittest.TestCase):\n'
                                 '    def test_x(self):\n        pass\n',
    'tools/README.md': 'readme\n',
    'tools/notes.json': '{}\n',
    'docs/INDEX.md': 'index\n',
    'docs/architecture.md': 'arch\n',
    'docs/reference/BACKLOG_STORE.md': 'store\n',
    'pvp/x.txt': 'x\n',
}


class AuditRepo(Fakes):
    """A fixture checkout as the units' root (so a child reads its tables)."""

    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='pytests_repo_'))
        self.addCleanup(rmtree, self.root)
        for rel, text in AUDIT_FILES.items():
            path = os.path.join(self.root, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'tool_tests': AUDIT_TABLES}, fh)

    def join(self, rel):
        return os.path.join(self.root, *rel.split('/'))


class TheAudit(AuditRepo):
    """--audit: an unmapped real-tree read FAILs the unit; a mapped one passes."""

    def reader(self, name, rel):
        self.write(name, '''
            import os, unittest

            class R(unittest.TestCase):
                def test_read(self):
                    with open(os.path.join(%r, %r)) as fh:
                        fh.read(1)
            ''' % (self.root, rel))

    def test_an_unmapped_read_fails_the_unit(self):
        self.reader('test_reads_index', 'docs/INDEX.md')
        code, out = self.run_modules(['test_reads_index'], audit=True)
        self.assertEqual(code, 1, out)
        self.assertIn('[FAIL] audit: test_reads_index.R.test_read read docs/INDEX.md', out)
        self.assertIn('add a .kit.json tool_tests pinned / extra entry naming it', out)
        self.assertTrue(RED.search(out))

    def test_the_same_read_without_audit_passes(self):
        self.reader('test_reads_index', 'docs/INDEX.md')
        code, out = self.run_modules(['test_reads_index'])
        self.assertEqual(code, 0, out)

    def test_a_read_the_pinned_table_maps_to_the_module_passes(self):
        # A fake named like the module `pinned` names for this path.
        self.reader('test_backlog', 'docs/reference/BACKLOG_STORE.md')
        code, out = self.run_modules(['test_backlog'], audit=True)
        self.assertIn('pytests: audit test_backlog: 0 unmapped', out)
        self.assertEqual(code, 0, out)

    def test_the_hook_rules(self):
        a = child.Audit('test_backlog', self.root)
        a.current = 'test_backlog.X.test_y'
        for rel in ('docs/reference/BACKLOG_STORE.md', 'tools/backlog.py', 'tools/README.md',
                    'tools/__pycache__/x.pyc', '.git/HEAD'):
            a.hook('open', (self.join(rel), 'r', 0))
        a.hook('open', (os.path.join(tempfile.gettempdir(), 'elsewhere.md'), 'r', 0))
        self.assertEqual(a.findings, {})
        a.hook('open', (self.join('docs/INDEX.md'), 'r', 0))
        a.hook('os.scandir', (self.join('pvp'),))
        a.hook('open', (self.join('tools/gate.py'), 'r', 0))       # not in test_backlog's closure
        self.assertEqual(sorted(a.findings), ['docs/INDEX.md', 'pvp', 'tools/gate.py'])

    def test_the_tools_folder_comes_from_the_kit_json(self):
        # repo.tools_dir moves the tools folder; the audit's own-module rule follows it.
        os.rename(self.join('tools'), self.join('scripts'))
        with open(self.join('.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'repo': {'tools_dir': 'scripts'}, 'tool_tests': AUDIT_TABLES}, fh)
        a = child.Audit('test_backlog', self.root)
        a.current = 'test_backlog.X.test_y'
        a.hook('open', (self.join('scripts/backlog.py'), 'r', 0))
        self.assertEqual(a.findings, {})
        a.hook('open', (self.join('scripts/gate.py'), 'r', 0))
        self.assertEqual(sorted(a.findings), ['scripts/gate.py'])

    def test_a_whole_tree_class_reads_anything(self):
        a = child.Audit('test_machine_paths', self.root)
        a.current = 'test_machine_paths.TheRealTree.test_x'
        a.hook('open', (self.join('docs/INDEX.md'), 'r', 0))
        self.assertEqual(a.findings, {})
        a.current = 'test_machine_paths.PlantedTree.test_x'
        a.hook('open', (self.join('docs/INDEX.md'), 'r', 0))
        self.assertEqual(list(a.findings), ['docs/INDEX.md'])

    def test_other_checkouts_and_a_scan_readers_non_py_reads(self):
        # .claude/worktrees/** is another checkout, never this tree's input; a module reaching
        # tool_deps may read tools/*.py, but README/json stay audited.
        a = child.Audit('test_exit_status', self.root)  # its closure holds tool_deps (via gate)
        self.assertIn('tool_deps', a.reach)
        a.current = 'test_exit_status.X.test_y'
        for rel in ('.claude/worktrees/other/tools/gate.py', '.claude/worktrees/other/docs/x.md',
                    'tools/%s.py' % 'codemap'):      # spelled so it is not an edge
            a.hook('open', (self.join(rel), 'r', 0))
        self.assertEqual(a.findings, {})
        a.hook('open', (self.join('tools/README.md'), 'r', 0))
        a.hook('open', (self.join('tools/notes.json'), 'r', 0))       # in no `extra` entry
        self.assertEqual(sorted(a.findings), ['tools/README.md', 'tools/notes.json'])

    def test_a_read_at_import_is_labelled_and_audited(self):
        self.write('test_imports_a_read', '''
            import os, unittest
            open(os.path.join(%r, 'docs', 'architecture.md')).close()

            class T(unittest.TestCase):
                def test_nothing(self):
                    pass
            ''' % self.root)
        code, out = self.run_modules(['test_imports_a_read'], audit=True)
        self.assertEqual(code, 1, out)
        self.assertIn('[FAIL] audit: test_imports_a_read (import) read docs/architecture.md', out)

    def test_a_whole_tree_class_setupclass_read_passes_and_another_class_fails(self):
        # A fake named like the module whose TheRealTree `whole_tree` lists.
        self.write('test_machine_paths', '''
            import os, unittest

            def read():
                open(os.path.join(%r, 'docs', 'architecture.md')).close()

            class TheRealTree(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    read()

                def test_a(self):
                    pass

            class Planted(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    read()

                def test_b(self):
                    pass
            ''' % self.root)
        code, out = self.run_modules(['test_machine_paths'], audit=True)
        self.assertEqual(code, 1, out)
        audit = [l for l in out.splitlines() if l.startswith('[FAIL] audit:')]
        self.assertEqual(len(audit), 1, out)
        self.assertIn('test_machine_paths.Planted.setUpClass read docs/architecture.md', audit[0])

    def test_the_audit_uses_the_kits_tool_deps_over_a_same_named_repo_file(self):
        # The child puts the checkout's tools folder first; the fixture's tools/tool_deps.py
        # (X = 1, no scan) must not be the module the audit uses.
        self.reader('test_backlog', 'docs/reference/BACKLOG_STORE.md')
        code, out = self.run_modules(['test_backlog'], audit=True)
        self.assertNotIn('AttributeError', out)
        self.assertEqual(code, 0, out)


class ClassFixtureLabels(unittest.TestCase):
    """label_class_fixtures must not change what a fixture does - only what its reads are
    credited to (a staticmethod over a bound method ran Base's fixture with cls=Base when a
    subclass called super().setUpClass())."""

    def test_super_setupclass_runs_with_the_subclass_and_is_credited_to_it(self):
        labels = []

        class Base(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.marker = cls.__name__
                labels.append(('Base.setUpClass', cls.__name__, auditor_ref[0].current))

            def test_base(self):             # inherited by both subclasses: each sees its own
                self.assertEqual(self.marker, type(self).__name__)

        class Sub(Base):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                cls.sub_ran = True

            def test_sub(self):
                self.assertEqual(self.marker, 'Sub')
                self.assertEqual(Base.marker, 'Base')
                self.assertTrue(self.sub_ran)

        class Inherits(Base):
            def test_inherits(self):
                self.assertEqual(self.marker, 'Inherits')

        auditor_ref = [None]

        class Auditor(object):
            current = None
        auditor_ref[0] = Auditor()
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                                   for c in (Base, Sub, Inherits))
        child.label_class_fixtures(suite, auditor_ref[0])
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.errors + result.failures, [], result.errors + result.failures)
        self.assertEqual(result.testsRun, 5)
        mod = __name__
        self.assertEqual(labels, [('Base.setUpClass', 'Base', '%s.Base.setUpClass' % mod),
                                  ('Base.setUpClass', 'Sub', '%s.Sub.setUpClass' % mod),
                                  ('Base.setUpClass', 'Inherits', '%s.Inherits.setUpClass' % mod)])
        self.assertIsNone(auditor_ref[0].current)


class TheHandshake(unittest.TestCase):
    """The child is created SUSPENDED, assigned to its job, shielded, and only then resumed
    (kitlib.proc.job.Job.start with shield_unit as its before_resume). Fails if CREATE_SUSPENDED
    is dropped or the order changes."""

    def test_created_suspended_then_assigned_then_shielded_then_resumed(self):
        calls, spawned = [], {}
        real_close = job_mod.Job.close

        class FakePopen(object):
            def __init__(self, cmd, **kw):
                spawned.update(kw)
                calls.append('popen')
                self.stdout = io.BytesIO(b'Ran 1 test in 0.0s\n\nOK\n')
                self.returncode = None
                self.pid = 1
                self._handle = 0

            def wait(self, timeout=None):
                calls.append('wait')
                self.returncode = 0
                return 0

            def kill(self):
                calls.append('kill')

        def close(job):
            if job.h:
                calls.append('close')
            real_close(job)

        with mock.patch.object(subprocess, 'Popen', FakePopen), \
                mock.patch.object(job_mod.Job, 'assign', lambda job, p: calls.append('assign')), \
                mock.patch.object(job_mod.Job, 'close', close), \
                mock.patch.object(win32, 'resume', lambda p: calls.append('resume') or 0), \
                mock.patch.object(runner, 'shield_unit', lambda p: calls.append('shield')):
            unit, status, ran, _, out = runner.run_unit(('test_x', 1, 1), False, 5,
                                                        root=tempfile.gettempdir(), cmd=['x'])
        self.assertEqual(status, 'OK', out)
        self.assertTrue(spawned.get('creationflags', 0) & runner.CREATE_SUSPENDED, spawned)
        self.assertEqual(runner.CREATE_SUSPENDED, 0x4)
        # Shielded while still suspended: not one instruction of the unit runs unshielded.
        self.assertEqual(calls[:5], ['popen', 'assign', 'shield', 'resume', 'wait'])
        self.assertEqual(calls[-1], 'close')
        self.assertNotIn('kill', calls)

    def test_a_child_that_cannot_be_placed_fails_named_and_is_killed(self):
        calls = []

        def refuse(job, p):
            raise job_mod.JobError('AssignProcessToJobObject: windows error 5')
        with mock.patch.object(job_mod.Job, 'assign', refuse), \
                mock.patch.object(runner, 'shield_unit', lambda p: calls.append('shield')):
            unit, status, ran, _, out = runner.run_unit(
                ('test_x', 1, 1), False, 5, root=tempfile.gettempdir(),
                cmd=[sys.executable, '-c', 'print("never")'])
        self.assertEqual(status, 'FAILED')
        self.assertIn('pytests: could not place the child in its job (AssignProcessToJobObject: '
                      'windows error 5); it was killed', out)
        self.assertNotIn('never', out)
        self.assertEqual(calls, [])                 # never shielded: it never got that far

    def test_a_command_that_cannot_start_fails_named(self):
        unit, status, ran, _, out = runner.run_unit(
            ('test_x', 1, 1), False, 5, root=tempfile.gettempdir(),
            cmd=[os.path.join(tempfile.gettempdir(), 'pytests_no_such_program_%d.exe' % os.getpid())])
        self.assertEqual(status, 'FAILED')
        self.assertIn('pytests: could not start', out)

    def test_no_job_object_means_the_unit_does_not_run(self):
        def no_job(*a, **k):
            raise job_mod.JobError('CreateJobObjectW: windows error 8')
        with mock.patch.object(runner, 'Job', no_job):
            unit, status, ran, _, out = runner.run_unit(('test_x', 1, 1), False, 5,
                                                        root=tempfile.gettempdir(), cmd=['x'])
        self.assertEqual(status, 'FAILED')
        self.assertIn('pytests: no job object for this unit (CreateJobObjectW: windows error 8); '
                      'not run', out)


def ok_runner(log=None, ran=1, status='OK'):
    """A fake run_unit: no process, `ran` tests, `status`; records (start|end, unit) in `log`."""
    def fake(unit, audit, timeout, path=None, root=None):
        if log is not None:
            log.append(('start', runner.unit_name(unit)))
            time.sleep(0.05)
            log.append(('end', runner.unit_name(unit)))
        return unit, status, ran, 0.1, 'Ran %d tests in 0.1s\n\nOK\n' % ran
    return fake


class Summary(unittest.TestCase):
    """The lines a gate step reads at the end of a run (a repository's pysel summary, merged)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pytests_floor_')
        self.addCleanup(rmtree, self.dir)

    def test_floor_counts_whole_modules_statically_and_a_single_id_as_one(self):
        with open(os.path.join(self.dir, 'test_a.py'), 'w', encoding='utf-8') as fh:
            fh.write('class A:\n    def test_x(self): pass\n    def test_y(self): pass\n    def helper(self): pass\n')
        self.assertEqual(runner.floor_of(['test_a', 'test_b.C.test_z'], [self.dir]), 3)

    def test_floor_counts_a_class_by_its_own_test_methods(self):
        with open(os.path.join(self.dir, 'test_a.py'), 'w', encoding='utf-8') as fh:
            fh.write('class A:\n    def test_x(self): pass\n    def test_y(self): pass\n'
                     'class B:\n    def test_z(self): pass\n    def helper(self): pass\n')
        self.assertEqual(runner.floor_of(['test_a.A'], [self.dir]), 2)
        self.assertEqual(runner.floor_of(['test_a.B', 'test_missing'], [self.dir]), 1)

    def run_with(self, modules, fake, floor=7, **kw):
        out = io.StringIO()
        with mock.patch.object(runner, 'floor_of', lambda names, dirs: floor):
            rc = runner.run(modules, self.dir, out=out, runner=fake, path=self.dir, **kw)
        return rc, out.getvalue().splitlines()

    def test_a_green_run_ends_in_the_summary_the_gate_reads(self):
        rc, lines = self.run_with(['test_a', 'test_b'], ok_runner(ran=4), jobs=2)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-3], 'pysel floor 7')
        self.assertRegex(lines[-2], r'^Ran 8 tests in ')
        self.assertEqual(lines[-1], 'OK')

    def test_a_failed_or_timed_out_unit_fails_the_run(self):
        for status in ('FAILED', 'TIMEOUT'):
            rc, lines = self.run_with(['test_a'], ok_runner(status=status))
            self.assertEqual(rc, 1, status)
            self.assertEqual(lines[-1], 'FAILED (units: test_a)')
            self.assertTrue(RED.search(lines[-1]))

    def test_a_runner_bug_fails_its_unit_named(self):
        def broken(unit, audit, timeout, path=None, root=None):
            raise RuntimeError('boom')
        rc, lines = self.run_with(['test_a'], broken)
        self.assertEqual(rc, 1)
        self.assertIn("pytests: the runner itself failed on this unit: RuntimeError('boom')", lines)

    def test_serial_units_run_after_the_pool_one_at_a_time(self):
        log = []
        rc, _ = self.run_with(['test_a', 'test_b', 'test_s', 'test_t'], ok_runner(log), jobs=4,
                              cfg=cfg(serial=['test_s', 'test_t.Only']))
        self.assertEqual(rc, 0)
        # test_t.Only names a class: the whole module test_t is not serial.
        firsts = [u for e, u in log if e == 'start']
        last_pool_end = max(i for i, (e, u) in enumerate(log) if e == 'end' and u != 'test_s')
        s_start = log.index(('start', 'test_s'))
        self.assertGreater(s_start, last_pool_end, log)
        self.assertEqual(firsts[-1], 'test_s')

    def test_a_serial_class_entry_matches_that_class_unit(self):
        log = []
        rc, _ = self.run_with(['test_a', 'test_t.Only'], ok_runner(log), jobs=2,
                              cfg=cfg(serial=['test_t.Only']))
        self.assertEqual(rc, 0)
        self.assertEqual([u for e, u in log if e == 'start'][-1], 'test_t.Only')
        self.assertEqual(log[-2:], [('start', 'test_t.Only'), ('end', 'test_t.Only')])


def git_run(results):
    """A fake subprocess.run for changed_paths: `results` maps the git verb to (rc, out, err)."""
    def run(cmd, **_k):
        rc, out, err = results.get(cmd[1], (0, '', ''))
        return subprocess.CompletedProcess(cmd, rc, out, err)
    return run


class Repo(unittest.TestCase):
    """A temporary git checkout with a tools folder, run through the entry point with `-C`."""

    FILES = {}

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        for name, text in self.FILES.items():
            self.repo.write('tools/' + name, text)

    def write_config(self, data):
        with open(os.path.join(self.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump(data, fh)

    def main(self, *argv, root=None):
        out = io.StringIO()
        with mock.patch.object(runner, 'run', return_value=0) as run:
            code = cli.main(list(argv) + ['-C', root or self.root], out=out)
        return code, out.getvalue().splitlines(), run


class ChangedPaths(Repo):
    """--select reads the change list from git and refuses, naming the command, when it cannot."""

    def refusal(self, run):
        with self.assertRaises(runner.Refused) as cm:
            runner.changed_paths('main', tempfile.gettempdir(), run=run)
        return str(cm.exception)

    def test_a_git_failure_refuses_naming_the_command(self):
        # A git failure refuses: returning None and running every module instead would be a silent
        # degrade.
        msg = self.refusal(git_run({'merge-base': (0, 'abc123\n', ''),
                                    'diff': (128, '', 'hint: x\nfatal: bad revision\n')}))
        self.assertIn('pytests: REFUSED - `git diff --name-only --no-renames -z abc123` failed '
                      '(exit 128: fatal: bad revision)', msg)

    def test_an_empty_merge_base_or_an_oserror_refuses(self):
        msg = self.refusal(git_run({'merge-base': (0, '\n', '')}))
        self.assertIn('`git merge-base main HEAD` failed (exit 0 but no merge-base printed)', msg)

        def boom(cmd, **_k):
            raise FileNotFoundError('git not found')
        msg = self.refusal(boom)
        self.assertIn('`git merge-base main HEAD` failed (FileNotFoundError: git not found)', msg)

    def test_the_cause_is_the_fatal_line_else_the_last_line(self):
        self.assertEqual(runner.stderr_cause('hint: a\nfatal: b\nhint: c\n'), 'fatal: b')
        self.assertEqual(runner.stderr_cause('one\n\ntwo\n'), 'two')
        self.assertEqual(runner.stderr_cause(''), 'no stderr')

    def test_changed_and_untracked_paths_are_read(self):
        out = runner.changed_paths('main', tempfile.gettempdir(), run=git_run({
            'merge-base': (0, 'abc\n', ''), 'diff': (0, 'tools/a.py\0docs/b.md\0', ''),
            'ls-files': (0, 'tools/new.py\0', '')}))
        self.assertEqual(out, {'tools/a.py', 'docs/b.md', 'tools/new.py'})

    def test_main_refuses_before_selecting_on_a_git_failure(self):
        self.repo.write('tools/lib.py', 'X = 1\n')
        with mock.patch.object(tool_deps, 'selection') as sel, \
                mock.patch.object(tool_deps, 'scan') as scan:
            code, lines, run = self.main('--select', '--base', 'no_such_branch')
        self.assertEqual(code, 2)
        sel.assert_not_called()                             # no selection, so no run
        scan.assert_not_called()
        run.assert_not_called()
        self.assertTrue(lines[0].startswith(
            'pytests: REFUSED - `git merge-base no_such_branch HEAD` failed'), lines)
        self.assertEqual(lines[-1], 'pytests FAILED | no test ran: git could not read the change list')
        self.assertTrue(RED.search(lines[-1]))

    def test_a_rename_lists_both_the_old_and_the_new_path(self):
        self.repo.write('tools/old_name.py', 'X = 1\n' * 20)
        self.repo.commit('base', 'tools/old_name.py')
        self.repo.git('checkout', '-q', '-b', 'topic')
        self.repo.git('mv', 'tools/old_name.py', 'tools/new_name.py')
        self.repo.git('commit', '-q', '-m', 'rename')
        self.assertEqual(runner.changed_paths('main', self.root),
                         {'tools/old_name.py', 'tools/new_name.py'})


class Select(Repo):
    """--select on a fixture checkout: kitlib.tooldeps picks, the entry point prints why and runs
    them."""

    FILES = {'lib.py': 'X = 1\n',
             'test_lib.py': 'import unittest\nimport lib\n\nclass T(unittest.TestCase):\n'
                            '    def test_x(self):\n        pass\n',
             'test_other.py': 'import unittest\n\nclass O(unittest.TestCase):\n'
                              '    def test_o(self):\n        pass\n'}

    def setUp(self):
        super().setUp()
        self.write_config({'tool_tests': {'always_run': ['test_other.O']}})

    def test_plan_prints_why_and_the_names_and_runs_nothing(self):
        code, lines, run = self.main('--select', '--plan', '--changed', 'tools/lib.py')
        self.assertEqual(code, 0)
        self.assertEqual(lines, ['pytests: 1 changed path(s) -> 1 test module(s) + 1 always-run',
                                 'pytests: test_lib test_other.O'])
        run.assert_not_called()

    def test_the_selection_runs_with_the_settings(self):
        self.write_config({'tool_tests': {'always_run': ['test_other.O'], 'unit_timeout': 77}})
        code, lines, run = self.main('--select', '--changed', 'tools/lib.py')
        self.assertEqual(code, 0)
        args, kw = run.call_args
        self.assertEqual(args[0], ['test_lib', 'test_other.O'])
        self.assertEqual(os.path.normcase(args[1]), os.path.normcase(self.root))
        self.assertEqual(kw['cfg']['unit_timeout'], 77)

    def test_nothing_selected_runs_nothing(self):
        self.write_config({})
        code, lines, run = self.main('--select', '--changed', 'docs/x.md')
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], 'pytests: no test module selected; nothing ran')
        run.assert_not_called()

    def test_all_runs_every_test_module(self):
        code, lines, run = self.main('--select', '--changed', 'tools/pytests.py')
        self.assertEqual(code, 0)
        self.assertEqual(lines[1], 'pytests: ALL (2 test modules)')
        self.assertEqual(run.call_args[0][0], ['test_lib', 'test_other'])

    def test_select_with_modules_or_plan_without_select_is_usage(self):
        self.assertEqual(self.main('--select', 'test_lib')[0], 2)
        self.assertEqual(self.main('--select', '--all')[0], 2)
        self.assertEqual(self.main('--plan', 'test_lib')[0], 2)
        self.assertEqual(self.main('--changed', 'x', '--all')[0], 2)

    def test_a_named_class_of_a_test_module_is_accepted(self):
        code, _, run = self.main('test_lib.T')
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][0], ['test_lib.T'])

    def test_the_merged_flag_adds_the_whole_tree_classes_not_every_module(self):
        # A landing's merged gate: the change's own selection plus the whole_tree classes, not ALL.
        self.write_config({'tool_tests': {'whole_tree': {'test_other.O': 'reads the whole tree'}}})
        code, lines, _ = self.main('--select', '--plan', '--merged', '--changed', 'tools/lib.py')
        self.assertEqual(code, 0)
        self.assertEqual(lines, ['pytests: 1 changed path(s) -> 1 test module(s) + 1 whole-tree '
                                 'class(es) (merged)', 'pytests: test_lib test_other.O'])

    def test_a_landing_merge_selects_over_base_to_merge(self):
        # The landing tree: HEAD is the merge of a branch into main, --base is main before it. A
        # tool main added since the branch forked (unmapped: ALL if it were read) is not in the
        # change list; the branch's lib.py is, and its test runs with the whole_tree class.
        self.write_config({'tool_tests': {'whole_tree': {'test_other.O': 'reads the whole tree'}}})
        r = self.repo
        r.commit('tools', '.kit.json', 'tools')
        r.git('checkout', '-q', '-b', 'topic')
        r.write('tools/lib.py', 'X = 2\n')
        r.commit('branch', 'tools/lib.py')
        r.git('checkout', '-q', 'main')
        r.write('tools/side.py', 'Y = 1\n')
        base = r.commit('main moved', 'tools/side.py')
        r.git('merge', '-q', '--no-ff', '-m', 'Merge topic', 'topic')
        code, lines, _ = self.main('--select', '--plan', '--merged', '--base', base)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ['pytests: 1 changed path(s) -> 1 test module(s) + 1 whole-tree '
                                 'class(es) (merged)', 'pytests: test_lib test_other.O'])
        code, lines, _ = self.main('--select', '--plan', '--base', base + '~1')   # side.py read
        self.assertEqual(lines[0], 'pytests: full suite: nothing maps tools/side.py to a test')

    def test_the_tools_dir_is_the_checkouts(self):
        os.rename(os.path.join(self.root, 'tools'), os.path.join(self.root, 'scripts'))
        self.write_config({'repo': {'tools_dir': 'scripts'}})
        code, _, run = self.main('test_lib')
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[1]['tools_rel'], 'scripts')

    def test_a_folder_that_is_no_checkout_refuses(self):
        empty = tempfile.mkdtemp(prefix='pytests_nogit_')
        self.addCleanup(rmtree, empty)
        code, lines, run = self.main('--all', root=empty)
        self.assertEqual(code, 2)
        self.assertTrue(lines[0].startswith('pytests: refused:'), lines)
        run.assert_not_called()

    def test_a_whole_run_through_the_entry_point(self):
        # No patch: the entry point runs both modules through real children, in the checkout.
        out = io.StringIO()
        code = cli.main(['--all', '-C', self.root], out=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(lines[-1], 'OK')
        self.assertRegex(lines[-2], r'^Ran 2 tests in ')
        self.assertEqual(lines[-3], 'pysel floor 2')


class Settings(unittest.TestCase):
    """`.kit.json` "tool_tests": the runner's keys, a default and a set test for each."""

    def test_the_defaults(self):
        s = runner.settings(data={})
        self.assertEqual(s['shards'], {})
        self.assertEqual(s['serial'], frozenset())
        self.assertIs(s['split_classes'], False)
        self.assertEqual((s['jobs_max'], s['jobs_divisor'], s['jobs_env']), (6, 4, None))
        self.assertEqual(s['unit_timeout'], 3600)

    def test_each_key_can_be_set(self):
        s = runner.settings(data={'shards': {'test_backlog': 4}, 'serial': ['test_a', 'test_b.C'],
                                  'split_classes': True, 'jobs_max': 8, 'jobs_divisor': 2,
                                  'jobs_env': 'REPO_PY_JOBS', 'unit_timeout': 60,
                                  'always_run': ['test_x']})       # a tool_deps key: ignored here
        self.assertEqual(s['shards'], {'test_backlog': 4})
        self.assertEqual(s['serial'], frozenset({'test_a', 'test_b.C'}))
        self.assertIs(s['split_classes'], True)
        self.assertEqual((s['jobs_max'], s['jobs_divisor'], s['jobs_env']), (8, 2, 'REPO_PY_JOBS'))
        self.assertEqual(s['unit_timeout'], 60)
        self.assertNotIn('always_run', s)

    def test_bad_values_are_config_errors(self):
        for bad in ({'shards': {'test_x': 0}}, {'shards': {'test_x.C': 2}}, {'shards': []},
                    {'serial': 'test_x'}, {'serial': ['']}, {'split_classes': 1},
                    {'jobs_max': 0}, {'jobs_divisor': '4'}, {'unit_timeout': True},
                    {'jobs_env': ''}, {'jobs_env': 3}, {'no_such_key': 1}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                runner.settings(data=bad)

    def test_tool_deps_leaves_exactly_these_keys_to_the_runner(self):
        self.assertEqual(set(tool_deps.PYTESTS_KEYS), set(runner.KEYS))

    def test_no_kit_json_gives_the_defaults(self):
        root = tempfile.mkdtemp(prefix='pytests_bare_')
        self.addCleanup(rmtree, root)
        self.assertEqual(runner.settings(load_config(root)), runner.settings(data={}))

    def test_a_loaded_section_is_read(self):
        root = tempfile.mkdtemp(prefix='pytests_cfg_')
        self.addCleanup(rmtree, root)
        with open(os.path.join(root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'tool_tests': {'serial': ['test_s'], 'jobs_env': 'REPO_PY_JOBS'}}, fh)
        s = runner.settings(load_config(root))
        self.assertEqual((s['serial'], s['jobs_env']), (frozenset({'test_s'}), 'REPO_PY_JOBS'))

    def test_main_refuses_a_bad_section_with_exit_2(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        with open(os.path.join(repo.root, '.kit.json'), 'w', encoding='utf-8') as fh:
            json.dump({'tool_tests': {'unit_timeout': 0}}, fh)
        out = io.StringIO()
        self.assertEqual(cli.main(['--all', '-C', repo.root], out=out), 2)
        self.assertTrue(out.getvalue().startswith('pytests: refused: kit configuration:'),
                        out.getvalue())
        self.assertIn('unit_timeout', out.getvalue())


class Usage(unittest.TestCase):

    def test_the_default_jobs_are_conservative(self):
        self.assertEqual([runner.default_jobs(n, environ={}) for n in (1, 4, 8, 12, 24, 64)],
                         [2, 2, 2, 3, 6, 6])

    def test_jobs_max_and_divisor_set_the_default(self):
        # an example setting: min(8, max(2, cpus // 2)).
        c = cfg(jobs_max=8, jobs_divisor=2)
        self.assertEqual([runner.default_jobs(n, c, environ={}) for n in (1, 4, 8, 12, 24, 64)],
                         [2, 2, 4, 6, 8, 8])

    def test_jobs_env_overrides_when_it_holds_an_integer(self):
        c = cfg(jobs_env='REPO_PY_JOBS')
        self.assertEqual(runner.default_jobs(64, c, environ={'REPO_PY_JOBS': '3'}), 3)
        self.assertEqual(runner.default_jobs(64, c, environ={'REPO_PY_JOBS': '0'}), 1)
        self.assertEqual(runner.default_jobs(64, c, environ={'REPO_PY_JOBS': 'x'}), 6)
        self.assertEqual(runner.default_jobs(64, c, environ={}), 6)
        # Unset jobs_env: the variable is never read.
        self.assertEqual(runner.default_jobs(64, environ={'REPO_PY_JOBS': '3'}), 6)

    def test_modules_and_all_together_or_neither_is_usage(self):
        here = tempfile.gettempdir()
        self.assertEqual(cli.main(['--all', 'test_gate', '-C', here], out=io.StringIO()), 2)
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', here], out=out), 2)
        self.assertTrue(out.getvalue().startswith('USAGE: '), out.getvalue())

    def test_a_checkout_without_its_tools_folder_refuses(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        out = io.StringIO()
        self.assertEqual(cli.main(['--all', '-C', repo.root], out=out), 2)
        self.assertEqual(out.getvalue(), 'pytests: refused: the checkout has no tools folder tools\n')

    def test_an_unknown_module_is_usage(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        repo.write('tools/lib.py', 'X = 1\n')
        out = io.StringIO()
        self.assertEqual(cli.main(['test_no_such_module', '-C', repo.root], out=out), 2)
        self.assertIn('test_no_such_module', out.getvalue())

    def test_a_bad_option_is_usage(self):
        with mock.patch('sys.stderr', new=io.StringIO()) as err, \
                self.assertRaises(SystemExit) as cm:
            cli.main(['--jobs', 'many'])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('USAGE: ', err.getvalue())

    def test_a_child_command_runs_the_kits_entry_point_on_the_checkout(self):
        cmd = runner.child_command(('test_backlog', 2, 4), True, 'R', path='P')
        self.assertEqual(cmd[1:], [os.path.join(KIT, 'tools', 'pytests.py'), '--child',
                                   'test_backlog', '-C', 'R', '--path', 'P', '--shard', '2/4',
                                   '--audit'])
        self.assertEqual(cmd[0], sys.executable)


class SelectDefaultBase(Repo):
    """--select with no --base measures against the remote's main when it resolves, and prints the
    line naming the local main when that one differs (kitlib.git.refs.default_base)."""

    FILES = {'lib.py': 'X = 1\n'}

    def stale(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.repo.write('tools/lib.py', 'X = 1\n')
        self.repo.commit('tools', 'tools/lib.py')
        self.repo.git('push', '-q', 'origin', 'main')
        return stale_local_main(self.repo)

    def test_a_stale_local_main_selects_against_origin_main_and_says_so(self):
        stale, fresh = self.stale()
        bases = []

        def changed(base, top, run=None):
            bases.append(base)
            return set()
        with mock.patch.object(runner, 'changed_paths', side_effect=changed):
            code, lines, _run = self.main('--select', '--plan')
        self.assertEqual((code, bases), (0, ['refs/remotes/origin/main']), lines)
        self.assertIn('pytests: base: local main %s is 1 behind origin/main %s; measuring against '
                      'refs/remotes/origin/main' % (stale[:7], fresh[:7]), lines)

    def test_a_local_main_at_origin_main_prints_no_base_line(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.repo.write('tools/lib.py', 'X = 1\n')
        bases = []
        with mock.patch.object(runner, 'changed_paths',
                               side_effect=lambda base, top, run=None: bases.append(base) or set()):
            code, lines, _run = self.main('--select', '--plan', root=self.repo.root)
        self.assertEqual((code, bases), (0, ['refs/remotes/origin/main']), lines)
        self.assertFalse([l for l in lines if 'measuring against' in l], lines)



class PidHoldFlag(Repo):
    """pytests.py --pid-hold is the opt-in: without it the run gets no hold."""

    FILES = {'test_x.py': PASSING}

    def test_off_by_default_on_with_the_flag(self):
        code, lines, run = self.main('--all')
        self.assertEqual(code, 0, lines)
        self.assertIsNone(run.call_args.kwargs['hold'])
        code, lines, run = self.main('--all', '--pid-hold')
        self.assertEqual(code, 0, lines)
        self.assertIsInstance(run.call_args.kwargs['hold'], runner.PidHoldRun)


if __name__ == '__main__':
    unittest.main()
