"""kitlib.gate.runner and the window monitor: every gate run says `windows opened: N` (and a line per
window) after its steps, from its lock run's monitor (kitlib.lock.run, kitlib.proc.winwatch). A
window warns and does not change the verdict; runner.WINDOWS_FAIL is the one switch that makes it
fail the gate. The monitor is swapped for a fake that reports the windows a test gives it, so no
window opens."""
import os
from unittest import mock

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.gate import runner
from kitlib.lock import run as lockrun
from kitlib.proc import winwatch
from kitlib.records import gate_ok
from tests.gate import fixtures

TOOL = r'C:\x\tool.exe'
WARNING = 'windows opened: 1 - WARNING: a console window opened (the verdict does not count it)'


def window(ours=True):
    return winwatch.Window(winwatch.CONSOLE, 'window', TOOL, pid=4321, exe=TOOL, ours=ours, at=0,
                           mono=0.0)


class FakeMonitor:
    windows_to_report = []

    def __init__(self, ours=None):
        self.error, self.gone, self.windows = None, 0, None

    def start(self):
        return self

    def stop(self):
        if self.windows is None:
            self.windows = list(FakeMonitor.windows_to_report)
        return self.windows


class Windows(fixtures.GateRepoCase):
    def setUp(self):
        super().setUp()
        FakeMonitor.windows_to_report = []
        patcher = mock.patch.object(lockrun.winwatch, 'Monitor', FakeMonitor)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []

    def gate(self, wt):
        return runner.branch_gate(wt, book_kw=self.book_kw, log_dir=self.logs, say=self.said.append)

    def test_no_window_says_zero(self):
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK')
        self.assertIn('windows opened: 0', self.said)
        step = [i for i, line in enumerate(self.said) if line.startswith('PASS  unit')][0]
        self.assertEqual(self.said.index('windows opened: 0'), step + 1)    # after the steps

    def test_a_window_warns_and_the_gate_still_passes(self):
        FakeMonitor.windows_to_report = [window()]
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.verdict, 'GATE OK')
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))
        i = self.said.index(WARNING)
        self.assertIn('pid 4321 tool.exe', self.said[i + 1])
        self.assertIn('[this run]', self.said[i + 1])
        self.assertEqual(out.windows, [window().as_dict()])

    def test_failing_on_a_window_is_one_switch(self):
        FakeMonitor.windows_to_report = [window()]
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(runner, 'WINDOWS_FAIL', True):
            out = self.gate(wt)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.verdict.startswith('GATE FAILED: windows (windows: 1 console window'),
                        out.verdict)
        self.assertIsNone(out.record)
        self.assertIn('windows opened: 1 - FAILED: a console window opened', self.said)
        ended = [self.store.read('ended/' + n) for n in os.listdir(self.store.path('ended'))]
        self.assertEqual([r['code'] for r in ended], [1])      # the lock run records it too

    def test_the_switch_ignores_another_runs_window(self):
        FakeMonitor.windows_to_report = [window(ours=False)]
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        with mock.patch.object(runner, 'WINDOWS_FAIL', True):
            out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('windows opened: 0', self.said)
        self.assertIn("  also seen, another run's: 1", self.said)

    def test_the_merged_tree_gate_says_it_too(self):
        FakeMonitor.windows_to_report = [window()]
        wt, _ = self.branch('feature', {'a.py': 'x = 1\n'})
        out = runner.merged_gate(wt, kconfig.load(wt), self.main, book_kw=self.book_kw,
                                 log_dir=self.logs, say=self.said.append)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn(WARNING, self.said)
        self.assertEqual(out.windows, [window().as_dict()])
