"""A gate log prints only its own run's header (docs/gate.md "Whose run a log is";
kitlib.gate.outfile, tools/gate.py main).

Seen twice (KIT-GATE-LOG-FOREIGN-HEADER): two workers of one session ran `gate.py --full >
<scratch>/gate-full.log` at the same time. Each process writes at its own file position, so the
later shell's truncation put the later run's `result record:`, `gate <branch> at <tip>` and budget
lines at the start of the earlier run's log, NUL bytes after them, then the earlier run's own
lines. TwoGatesOneLog runs that: two real tools/gate.py processes on one output file, the first
held after its header (its hook module waits at load, before anything is booked); the second
must say only its REFUSED lines there. Library cases use two handles of one file in this process.
Scratch repositories in temp; nothing is booked on any lock store."""
import importlib
import io
import os
import subprocess
import sys
import tempfile
import time
import shutil
import unittest

from kitlib import gate as G
from kitlib.gate import steps
from tests.gate import fixtures

GATE_PY = os.path.join(steps.KIT_TOOLS, 'gate.py')
HOLD_VAR = 'KIT_TEST_OUTFILE_HOLD'

# Loaded by the gate before it books (runner.gated); with HOLD_VAR set it says it is in, then
# waits while that file exists. Either way it exits, so the gate is REFUSED before any booking.
HOOK = (
    'import os, sys, time\n'
    'hold = os.environ.get(%r)\n'
    'if hold:\n'
    '    open(hold + ".in", "w").close()\n'
    '    end = time.time() + 120\n'
    '    while os.path.exists(hold) and time.time() < end:\n'
    '        time.sleep(0.05)\n'
    'sys.exit(0)\n' % HOLD_VAR)


def outfile():
    return importlib.import_module('kitlib.gate.outfile')


def same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class Claim(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(prefix='kit-outfile-')
        self.addCleanup(shutil.rmtree, d, True)
        self.path = os.path.join(d, 'gate-full.log')

    def handle(self, mode='w'):
        f = open(self.path, mode, encoding='utf-8')
        self.addCleanup(f.close)
        return f

    def test_a_second_handle_on_a_claimed_file_is_refused(self):
        first, second = self.handle(), self.handle('a')
        held, refused, warning = outfile().claim(first)
        self.assertIsNotNone(held)
        self.addCleanup(held.release)
        self.assertEqual((refused, warning), (None, None))
        held2, refused, warning = outfile().claim(second)
        self.assertIsNone(held2)
        self.assertIsNone(warning)
        self.assertTrue(refused[0].startswith('REFUSED: this gate\'s output file '), refused)
        self.assertTrue(refused[1].startswith('Instead: '), refused)
        named = refused[0][len('REFUSED: this gate\'s output file '):].split(' is the output')[0]
        self.assertTrue(same_file(named, self.path), refused)

    def test_a_released_claim_can_be_taken_again(self):
        first, second = self.handle(), self.handle('a')
        held, _, _ = outfile().claim(first)
        held.release()
        again, refused, _ = outfile().claim(second)
        self.assertIsNotNone(again, refused)
        again.release()

    def test_the_claim_covers_no_byte_a_log_holds(self):
        """Writing and reading the file through another handle still work while it is held."""
        first = self.handle()
        held, _, _ = outfile().claim(first)
        self.addCleanup(held.release)
        with open(self.path, 'a', encoding='utf-8') as other:
            other.write('a line\n')
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'a line\n')

    def test_no_disk_file_claims_nothing(self):
        self.assertEqual(outfile().claim(io.StringIO()), (None, None, None))
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        with os.fdopen(w, 'w') as pipe:
            self.assertEqual(outfile().claim(pipe), (None, None, None))
        with open(os.devnull, 'w') as nul:
            self.assertEqual(outfile().claim(nul), (None, None, None))


class TwoGatesOneLog(fixtures.GateRepoCase):
    KIT = {'hook': {'module': 'kit_hooks'}}
    GATE = {'steps': [{'name': 'held', 'always': True, 'check': 'held'}]}

    def setUp(self):
        super().setUp()
        self.repo.write('tools/kit_hooks.py', HOOK)
        self.repo.commit('hook', 'tools/kit_hooks.py')
        self.repo.git('push', '-q', 'origin', 'main')
        self.log = os.path.join(self.tmp, 'gate-full.log')
        self.hold = os.path.join(self.tmp, 'hold')

    def start(self, wt, hold=None):
        """tools/gate.py -C wt, its stdout and stderr the shared log opened as `>` opens it."""
        env = dict(self.env)
        env.pop(HOLD_VAR, None)
        if hold:
            env[HOLD_VAR] = hold
        with open(self.log, 'w') as out:      # truncates, as the second shell's redirect did
            return subprocess.Popen([sys.executable, GATE_PY, '-C', wt, '--log-dir', self.logs],
                                    cwd=wt, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT)

    def wait_for(self, path, proc):
        end = time.time() + 60
        while not os.path.exists(path):
            if proc.poll() is not None or time.time() > end:
                self.fail('the first gate never reached its hook (exit %r): %s'
                          % (proc.poll(), self.text()))
            time.sleep(0.05)

    def text(self):
        with open(self.log, 'rb') as f:
            return f.read().replace(b'\0', b'').decode('utf-8', 'replace')

    def stop(self, proc):
        if os.path.exists(self.hold):
            os.remove(self.hold)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def test_a_second_gate_on_a_running_gates_log_prints_no_header_there(self):
        one, _ = self.branch('one', {'one.txt': '1\n'})
        two, _ = self.branch('two', {'two.txt': '2\n'})
        with open(self.hold, 'w'):
            pass
        first = self.start(one, hold=self.hold)
        self.addCleanup(self.stop, first)
        self.wait_for(self.hold + '.in', first)
        self.assertIn('gate kit/one at ', self.text())  # the first run is past its header
        second = self.start(two)
        code = second.wait(timeout=120)
        self.stop(first)
        text = self.text()
        self.assertNotIn('gate kit/two at ', text)      # the observable: the foreign header
        self.assertNotIn('kit%2Ftwo', text)             # nor its result record line
        self.assertEqual(code, G.REFUSED, text)
        self.assertIn('REFUSED: this gate\'s output file ', text)
        self.assertIn('kit%2Fone', text)                # the first run's own lines go on
        # The claim ends with its process: the file can be claimed again.
        with open(self.log, 'a') as f:
            held, refused, _ = outfile().claim(f)
            self.assertIsNotNone(held, refused)
            held.release()


if __name__ == '__main__':
    unittest.main()
