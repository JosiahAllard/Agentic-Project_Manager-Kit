"""kitlib.gate.pin and Ctrl+C around the start of the pinned tool (KIT-PIN-CTRLC-POPEN-GAP).

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
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import gitfixture

CREATE_NO_WINDOW = 0x08000000

CHILD = r'''import signal, sys, time
print('python-handler' if signal.getsignal(signal.SIGINT) is signal.default_int_handler
      else repr(signal.getsignal(signal.SIGINT)), flush=True)
try:
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


if __name__ == '__main__':
    unittest.main()
