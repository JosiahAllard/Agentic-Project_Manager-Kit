"""kitlib.platform.linux.process.rows (docs/linux-support-plan.md slice 11): every /proc process as
kitlib.pmstart.list_processes's dicts. Fake runs on any platform against a /proc tree in a temp
folder (PROC and STAT patched; every pid folder has its stat, so no signal is sent to a real pid);
Live runs on Linux against the real /proc."""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.platform.linux import process as P
from tests.platforms import posix_only

BTIME = 1700000000


def stat(pid, comm, ppid, starttime, state='S'):
    rest = [state, str(ppid)] + ['0'] * 17 + [str(starttime)] + ['0'] * 10
    return ('%d (%s) %s\n' % (pid, comm, ' '.join(rest))).encode()


class Fake(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='proc-rows-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.proc = os.path.join(self.root, 'proc')
        os.makedirs(self.proc)
        self.stat = os.path.join(self.root, 'stat')
        with open(self.stat, 'w', encoding='ascii') as f:
            f.write('cpu  1 2 3\nbtime %d\nprocesses 9\n' % BTIME)
        for patch in (mock.patch.object(P, 'PROC', self.proc), mock.patch.object(P, 'STAT', self.stat),
                      mock.patch.object(P, '_clk_tck', lambda: 100)):
            patch.start()
            self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.proc, 'self'))

    def add(self, pid, comm, ppid, starttime, args=None, stat_bytes=None):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, 'stat'), 'wb') as f:
            f.write(stat_bytes if stat_bytes is not None else stat(pid, comm, ppid, starttime))
        if args is not None:
            with open(os.path.join(d, 'cmdline'), 'wb') as f:
                f.write(b''.join(a.encode() + b'\0' for a in args))

    def readlink(self, path):
        pid = os.path.basename(os.path.dirname(path))
        if pid == '20':
            raise PermissionError(path)
        return '/usr/bin/x%s' % pid

    def test_each_process_is_a_list_processes_row(self):
        self.add(10, 'claude', 1, 250, ['claude', '--remote-control', 'x a', '-n', 'x'])
        self.add(20, 'tmux: server', 1, 100, [])
        got = sorted(P.rows(readlink=self.readlink), key=lambda r: r['ProcessId'])
        created = datetime.datetime.fromtimestamp(BTIME + 2.5).astimezone().isoformat()
        self.assertEqual(got[0], {'Name': 'claude', 'CommandLine': 'claude --remote-control x a -n x',
                                  'ExecutablePath': '/usr/bin/x10', 'ProcessId': 10,
                                  'ParentProcessId': 1, 'Created': created})
        self.assertEqual(got[1]['Name'], 'tmux: server')
        self.assertEqual(got[1]['CommandLine'], '')
        self.assertIsNone(got[1]['ExecutablePath'])
        self.assertEqual(len(got), 2)
        self.assertEqual(pmstart._created(got[0]), BTIME + 2.5)

    def test_an_unreadable_or_vanished_process_is_left_out(self):
        self.add(10, 'ok', 1, 5, ['ok'])
        self.add(11, 'nocmd', 1, 5)                         # its cmdline went away
        self.add(12, 'bad', 1, 5, ['bad'], stat_bytes=b'garbage')
        self.assertEqual([r['ProcessId'] for r in P.rows(readlink=self.readlink)], [10])

    def test_no_btime_gives_no_created(self):
        with open(self.stat, 'w', encoding='ascii') as f:
            f.write('cpu 1\n')
        self.add(10, 'ok', 1, 5, ['ok'])
        self.assertIsNone(P.rows(readlink=self.readlink)[0]['Created'])

    def test_no_proc_raises(self):
        with mock.patch.object(P, 'PROC', os.path.join(self.root, 'none')):
            with self.assertRaises(OSError):
                P.rows()


@posix_only('reads the real /proc')
class Live(unittest.TestCase):
    def test_this_process_is_listed_with_its_parent_and_start(self):
        me = [r for r in P.rows() if r['ProcessId'] == os.getpid()]
        self.assertEqual(len(me), 1)
        row = me[0]
        self.assertEqual(row['ParentProcessId'], os.getppid())
        self.assertTrue(row['CommandLine'])
        self.assertEqual(os.path.realpath(row['ExecutablePath']), os.path.realpath(sys.executable))
        started = pmstart._created(row)
        now = datetime.datetime.now().timestamp()
        self.assertTrue(now - 86400 < started <= now + 2, (started, now))


if __name__ == '__main__':
    unittest.main()
