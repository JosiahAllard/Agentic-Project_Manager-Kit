"""kitlib.gate.pin and Ctrl+C around the start of the pinned tool (docs/gate.md "Pinned tools").

pin._run ignores SIGINT from just before it starts the child until the child exits, so a Ctrl+C
inside Popen, or between its return and the assignment, cannot give 130 ("the tool did not run")
and drop the lease while the child runs on. That is only right if the child still gets the Ctrl+C:

- Inheritance: a Windows child started while its parent has SIGINT set to SIG_IGN is interrupted
  by a real console Ctrl+C. The event is sent in a console of its own (the helper is started with
  CREATE_NO_WINDOW), never the console this test runs in; the helper refuses to send it unless
  that console holds exactly itself and its child. A control shows the test can see inheritance:
  SetConsoleCtrlHandler(NULL, TRUE), the console-level ignore, IS inherited. The helper clears
  that ignore first: measured 2026-10-11, a test run from an agent's shell had it already, and
  without the clear no child was interrupted, whatever its parent's SIGINT handler.
- The window: a Ctrl+C raised inside Popen, once the child has started, is ignored; the pin waits
  for the tool and returns its code, the lease held until it exits. The handler is SIG_IGN while
  the child starts and runs, and the one before is back after, when the start fails too. Off the
  main thread (where signal.signal raises) the tool still runs.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import gitfixture
from tests.gate.test_live_pin import PROBE, isolated, pins_root, temp_places
from tests.pluginsource import Kit

CREATE_NO_WINDOW = 0x08000000

CHILD = r'''import signal, sys, time
try:        # the line is printed inside the try: the helper sends Ctrl+C as soon as it reads it
    print('python-handler' if signal.getsignal(signal.SIGINT) is signal.default_int_handler
          else repr(signal.getsignal(signal.SIGINT)), flush=True)
    time.sleep(30)
except KeyboardInterrupt:
    print('interrupted', flush=True)
    sys.exit(5)
sys.exit(0)
'''

# argv: <child script> <mode> <seconds to wait for the child>. Prints one JSON line.
HELPER = r'''import ctypes, json, signal, subprocess, sys
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.SetConsoleCtrlHandler.argtypes = (ctypes.c_void_p, wintypes.BOOL)
k32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
k32.GetConsoleProcessList.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
child_py, mode, wait_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
k32.SetConsoleCtrlHandler(None, False)  # clear a console-level ignore inherited from our parent
signal.signal(signal.SIGINT, signal.SIG_IGN)            # what pin._run does
if mode == 'console-ignore':
    k32.SetConsoleCtrlHandler(None, True)               # the control: the console-level ignore
child = subprocess.Popen([sys.executable, child_py], stdout=subprocess.PIPE, text=True)
out = {'handler': child.stdout.readline().strip()}
ids = (wintypes.DWORD * 16)()
n = k32.GetConsoleProcessList(ids, 16)
out['console'] = sorted(ids[i] for i in range(min(n, 16)))
if out['console'] != sorted([__import__('os').getpid(), child.pid]):
    out['refused'] = 'the console is not this helper and its child alone'
else:
    out['sent'] = bool(k32.GenerateConsoleCtrlEvent(0, 0))      # CTRL_C_EVENT, this console
try:
    out['code'] = child.wait(wait_s)
    out['rest'] = child.stdout.read().strip()
except subprocess.TimeoutExpired:
    child.kill()
    child.wait()
    out['code'] = None
print(json.dumps(out), flush=True)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows console Ctrl+C')
class ChildInheritance(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-pin-sigint-')
        self.addCleanup(gitfixture.rmtree, self.dir)
        self.child = os.path.join(self.dir, 'child.py')
        self.helper = os.path.join(self.dir, 'helper.py')
        for path, text in ((self.child, CHILD), (self.helper, HELPER)):
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)

    def run_helper(self, mode, wait_s):
        r = subprocess.run([sys.executable, self.helper, self.child, mode, str(wait_s)],
                           capture_output=True, text=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn('refused', got, got)
        self.assertTrue(got['sent'], got)
        return got

    def test_a_child_of_a_parent_ignoring_sigint_still_gets_ctrl_c(self):
        got = self.run_helper('sig-ign', 20)
        self.assertEqual(got['handler'], 'python-handler', got)
        self.assertEqual((got['code'], got['rest']), (5, 'interrupted'), got)

    def test_control_the_console_level_ignore_is_inherited(self):
        """Proves the test above can see an inherited ignore: under SetConsoleCtrlHandler(NULL,
        TRUE) the child's Python handler is installed all the same, yet no Ctrl+C reaches it."""
        got = self.run_helper('console-ignore', 2)
        self.assertEqual(got['handler'], 'python-handler', got)
        self.assertIsNone(got['code'], got)


def _pin():
    from kitlib.gate import pin
    return pin


def _sentinel(signum, frame):
    raise KeyboardInterrupt


REAP_S = 60     # what a child the pin lost gets to exit by itself before the cleanup ends it


class _PinSubprocess:
    """What kitlib.gate.pin sees as `subprocess` while a test wraps the tool's start: its Popen is
    the test's, every other name the real module's. Only the pin's own start of the tool goes
    through it. The git children the pin's reads start on real git (kitlib.proc.job) use the real
    module: they are not counted, refused or interrupted, so the tests hold on both tiers."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


class Window(unittest.TestCase):
    """pin.run on a kit fixture, as in test_live_pin's Run, with the pin's start of the tool
    wrapped."""

    def setUp(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest('signal handlers are set on the main thread only')
        # A handler of this test's own: unittest's catch-break handler must never see the
        # SIGINT raised here, and every test checks the one before is back.
        before = signal.signal(signal.SIGINT, _sentinel)
        self.addCleanup(signal.signal, signal.SIGINT, before)
        self.k = Kit(self)
        self.places = temp_places(self)
        self.env = isolated(self, self.places)
        self.k.repo.write('tools/probe.py', PROBE)
        self.sha = self.k.repo.commit('probe', 'tools/probe.py')
        self.k.repo.git('push', '-q', 'origin', 'main')
        self.tool = os.path.join(self.k.kit, 'tools', 'probe.py')
        self.out = os.path.join(self.places['HOME'], 'probe.json')
        self.started = []
        self.addCleanup(self.reap)
        self.seen = []                  # the SIGINT handler at each step: (step, handler)

    def reap(self, grace=REAP_S):
        """A child the pin lost (the gap) still exits by itself before cleanup. One that does not
        within `grace` seconds is ended here, through its own handle, so no test leaves a process
        behind; the cleanup fails all the same, naming it."""
        left = []
        for proc in self.started:
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                left.append(proc)
        for proc in left:
            proc.kill()
        for proc in left:
            proc.wait(REAP_S)
        self.assertEqual([proc.args for proc in left], [],
                         'still running %s s after the test; ended by the cleanup' % grace)

    def wrap(self, popen):
        """Patch the pin's own start of the tool, and no other Popen call in the process."""
        return mock.patch.object(_pin(), 'subprocess', _PinSubprocess(popen))

    def popen(self, then=None):
        real = subprocess.Popen

        def start(*args, **kwargs):
            proc = real(*args, **kwargs)
            self.started.append(proc)
            self.seen.append(('popen', signal.getsignal(signal.SIGINT)))
            if then:
                then()
            return proc
        return self.wrap(start)

    def read_probe(self):
        with open(self.out, encoding='utf-8') as fh:
            return json.load(fh)

    def test_ctrl_c_inside_popen_after_the_child_started_waits_for_it(self):
        pin = _pin()
        with self.popen(then=lambda: signal.raise_signal(signal.SIGINT)):
            code, said = pin.run(self.tool, [self.out, '--slow'], environ=self.env)
        self.assertEqual((code, said), (7, None))
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.read_probe()['leases']), 1)      # held while the tool ran
        self.assertEqual(os.listdir(os.path.join(pins_root(self.places), 'leases')), [])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_sigint_is_ignored_while_the_tool_starts_and_runs_and_restored_after(self):
        pin = _pin()
        real_wait = pin._wait

        def wait(proc):
            self.seen.append(('wait', signal.getsignal(signal.SIGINT)))
            return real_wait(proc)
        with self.popen(), mock.patch.object(pin, '_wait', side_effect=wait):
            code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertEqual((code, said), (7, None))
        self.assertEqual(self.seen, [('popen', signal.SIG_IGN), ('wait', signal.SIG_IGN)])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_start_that_fails_restores_the_handler_and_fails_open(self):
        pin = _pin()

        def refuse(*args, **kwargs):
            self.seen.append(('popen', signal.getsignal(signal.SIGINT)))
            raise OSError('no such interpreter')
        with self.wrap(refuse):
            code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn('OSError: no such interpreter', said)
        self.assertEqual(self.seen, [('popen', signal.SIG_IGN)])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_ctrl_c_as_the_handler_comes_back_keeps_the_tools_code(self):
        """The tool has run and exited: a Ctrl+C raised as the handler is restored is not the
        pin's 130 ('the tool did not run')."""
        pin = _pin()
        real = pin._restore_ctrl_c

        def restore(before):
            real(before)
            signal.raise_signal(signal.SIGINT)      # _sentinel raises KeyboardInterrupt
        with mock.patch.object(pin, '_restore_ctrl_c', side_effect=restore):
            code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertEqual((code, said), (7, None))
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_a_restore_that_fails_after_the_tool_ran_keeps_its_code_and_does_not_fail_open(self):
        """Any other error from putting the handler back, once the tool has exited, is not a
        fail-open: the caller would run the tool a second time, live."""
        pin = _pin()
        real = pin._restore_ctrl_c

        def restore(before):
            real(before)
            raise RuntimeError('the handler cannot be put back')
        with self.popen(), mock.patch.object(pin, '_restore_ctrl_c', side_effect=restore):
            code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertEqual((code, said), (7, None))
        self.assertEqual(len(self.started), 1)
        self.assertNotEqual(self.env.get(pin.PINNED_ENV), pin.LIVE)    # nothing runs it again
        self.assertEqual(os.listdir(os.path.join(pins_root(self.places), 'leases')), [])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)

    def test_the_cleanup_ends_a_child_that_outlives_it_and_fails_naming_it(self):
        """reap, the cleanup of every test here, on a child that does not exit by itself."""
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(proc.kill)      # before reap's own turn, should the lines below fail
        self.started.append(proc)
        with self.assertRaises(AssertionError) as caught:
            self.reap(grace=0.5)
        self.assertIsNotNone(proc.poll())
        self.assertIn('time.sleep(120)', str(caught.exception))

    def test_off_the_main_thread_the_tool_still_runs(self):
        pin = _pin()
        got = []
        worker = threading.Thread(
            target=lambda: got.append(pin.run(self.tool, [self.out], environ=self.env)))
        worker.start()
        worker.join(120)
        self.assertEqual(got, [(7, None)])
        self.assertIs(signal.getsignal(signal.SIGINT), _sentinel)


if __name__ == '__main__':
    unittest.main()
