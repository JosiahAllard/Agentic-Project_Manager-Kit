"""kitlib.autotasks.tick.launch on Linux (docs/linux-support-plan.md slice 11): the task runs in its
own session (start_new_session), with no Windows creation flags, so it outlives the tick that
started it. kitlib.platform.NAME is patched to 'linux' and Popen is a fake, so this runs on Windows
and starts nothing."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from kitlib.autotasks import tick as T


class Popened(object):
    pid = 4321


class Linux(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='autotasks-linux-')
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_a_task_starts_in_its_own_session_with_no_creation_flags(self):
        got = []

        def popen(command, **kw):
            got.append((command, kw))
            return Popened()
        log = os.path.join(self.folder, 'x.log')
        with mock.patch('kitlib.platform.NAME', 'linux'), mock.patch.object(T.spawn, 'popen', popen):
            self.assertEqual(T.launch(['tool', 'arg'], log, self.folder), 4321)
        (command, kw), = got
        self.assertEqual(command, ['tool', 'arg'])
        self.assertTrue(kw['start_new_session'])
        self.assertNotIn('creationflags', kw)
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)
        self.assertEqual(kw['stderr'], subprocess.STDOUT)
        self.assertEqual(kw['cwd'], self.folder)
        self.assertTrue(kw['close_fds'])
        self.assertTrue(os.path.isfile(log))


if __name__ == '__main__':
    unittest.main()
