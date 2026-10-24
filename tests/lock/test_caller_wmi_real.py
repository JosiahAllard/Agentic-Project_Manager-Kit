"""kitlib.lock.store.caller in a process WMI started, on real processes: the shape a detached lock
run or a submitted landing has (kitlib.lock.detach: Win32_Process.Create starts pythonw.exe running
the carrier tools/no_window.py, which starts python.exe). WMI's provider host, WmiPrvSE.exe, is
listed but this user cannot open it, so the walk ends below it at the running carrier; that read
as the owner, and must read as an agent: by the chain (caller with no environment), and by the
marker detach.create sets (caller with the child's own), which still holds once WMI's host has
exited, a case this test cannot wait for. tests/lock/test_caller_tree.py has both on fakes."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

from kitlib.lock import detach
from kitlib.proc import process, spawn

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAIT_S = 90.0

# The child: who caller() says it is, from its tree alone and with its own environment, the
# marker it was given, and the names of its parent and grandparent as the listing
# gives them, written to argv[1] in one replace.
CHILD = r'''
import json, os, sys
sys.path.insert(0, %r)
out = sys.argv[1]
try:
    from kitlib.lock import store
    from kitlib.proc import process
    snap = process.snapshot()
    names = {p: n for p, _pp, n in snap}
    parents = {p: pp for p, pp, _n in snap}
    me = store.caller(env={})
    up = parents.get(os.getpid())
    got = {'role': me['role'], 'host': me['host'], 'own_env_role': store.caller()['role'],
           'marker': os.environ.get(store.DETACHED_ENV), 'parent': names.get(up),
           'grandparent': names.get(parents.get(up))}
except Exception as e:
    got = {'error': repr(e)}
with open(out + '.tmp', 'w', encoding='utf-8') as f:
    json.dump(got, f)
os.replace(out + '.tmp', out)
''' % ROOT


class UnderWmi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='caller-wmi-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_process_wmi_started_under_the_carrier_is_an_agent(self):
        script = os.path.join(self.tmp, 'child.py')
        out = os.path.join(self.tmp, 'who.json')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(CHILD)
        pythonw, console = detach.pythons(sys.executable)
        argv = [pythonw, os.path.join(ROOT, 'tools', spawn.CARRIER), console, '-B', script, out]
        why = detach.missing(argv)
        if why:
            self.skipTest('no detached start here: %s' % why)
        pid, why = detach.create(argv, self.tmp, dict(os.environ))
        if pid is None:
            self.skipTest('WMI is not available: %s' % why)
        ct = process.proc_ctime(pid)
        end = time.monotonic() + WAIT_S
        try:
            while not os.path.exists(out) and time.monotonic() < end:
                time.sleep(0.2)
            self.assertTrue(os.path.exists(out), 'the WMI-started child wrote nothing in %g s'
                            % WAIT_S)
        finally:
            if ct is not None and process.alive(pid, ct):
                process.kill_tree(pid, ct)          # by pid and creation time only
        with open(out, encoding='utf-8') as f:
            got = json.load(f)
        self.assertNotIn('error', got)
        # The premise: the child runs under the carrier, and the carrier under WMI's host.
        self.assertEqual((got['parent'] or '').lower(), 'pythonw.exe', got)
        self.assertEqual((got['grandparent'] or '').lower(), 'wmiprvse.exe', got)
        self.assertEqual(got['role'], 'agent', got)
        self.assertIsNone(got['host'], got)
        self.assertEqual(got['marker'], '1', got)
        self.assertEqual(got['own_env_role'], 'agent', got)


if __name__ == '__main__':
    unittest.main()
