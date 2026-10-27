"""The brief commands the kit runs ask for a short job (kitlib.proc.job.Job(short=True)): on Linux
they then share one carrier scope per process instead of a scope each
(kitlib.platform.linux.contain). A fake Job records what each caller asked for and refuses the
start, so nothing runs."""
import unittest
from unittest import mock

from kitlib.git import cmd
from kitlib.machinesetup import extras
from kitlib.proc import job
from kitlib.records import inflight


class Refusing:
    asked = []

    def __init__(self, *args, **kw):
        Refusing.asked.append(kw)

    def start(self, *args, **kw):
        raise OSError('the fake job starts nothing')

    def close(self):
        pass


class ShortCallers(unittest.TestCase):
    def setUp(self):
        Refusing.asked = []
        patch = mock.patch.object(job, 'Job', Refusing)
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, call, **want):
        with self.assertRaises(OSError):
            call()
        self.assertEqual(len(Refusing.asked), 1)
        asked = Refusing.asked[0]
        self.assertIs(asked.get('short'), True)
        self.assertEqual(asked.get('kill_on_close', True), want['kill_on_close'])   # its default

    def test_a_git_call_is_short_and_released_on_close(self):
        self.check(lambda: cmd._run(['git', '--version']), kill_on_close=False)

    def test_an_inflight_look_is_short_and_killed_on_close(self):
        self.check(lambda: inflight.run(['git', '--version'], '.', 5), kill_on_close=True)

    def test_a_plugin_runner_command_is_short_and_released_on_close(self):
        self.check(lambda: extras.run_tree(['claude', '--version']), kill_on_close=False)


if __name__ == '__main__':
    unittest.main()
