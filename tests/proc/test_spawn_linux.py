"""kitlib.proc.spawn off Windows: run, call and popen hand subprocess the caller's call unchanged,
and nothing asks kernel32 for a console. kitlib.platform.NAME is patched to 'linux' and kernel32 is
made to fail, so these run on any machine; RealStartTest starts a real python on the running
platform."""
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.proc import console, job, spawn

NEW_GROUP = 0x200


def no_kernel32():
    raise AssertionError('kernel32 asked off Windows')


class OffWindowsTest(unittest.TestCase):

    def setUp(self):
        for patch in (mock.patch('kitlib.platform.NAME', 'linux'),
                      mock.patch.object(console, '_k32', side_effect=no_kernel32)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_has_console_is_true_without_kernel32(self):
        self.assertIs(console.has_console(), True)

    def test_no_window_flags_adds_nothing_even_with_no_console(self):
        for flags in (0, NEW_GROUP):
            with self.subTest(flags=flags):
                self.assertEqual(job.no_window_flags(flags), flags)
                self.assertEqual(job.no_window_flags(flags, console=False), flags)

    def test_each_wrapper_hands_subprocess_the_call_unchanged(self):
        for name, attr in (('run', 'run'), ('call', 'call'), ('popen', 'Popen')):
            with self.subTest(name), mock.patch.object(spawn.subprocess, attr) as fake:
                spawn_fn = getattr(spawn, name)
                spawn_fn(['systemctl', '--user', 'list-units'], capture_output=True, text=True)
                self.assertEqual(fake.call_args, mock.call(
                    ['systemctl', '--user', 'list-units'], capture_output=True, text=True))


class RealStartTest(unittest.TestCase):
    """A real child on the running platform through spawn.run: on Linux it starts at all only when
    no creationflags but 0 reach subprocess."""

    def test_spawn_run_starts_a_real_child(self):
        r = spawn.run([sys.executable, '-c', 'print("ok")'], capture_output=True, text=True,
                      timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'ok'))


if __name__ == '__main__':
    unittest.main()
