"""Tests for kitlib.launcher.host.Host.git_ignored: its git gets no console window when the
launcher runs with no console (a scheduled task), and keeps its answers."""
import os
import subprocess
import unittest
from unittest import mock

from kitlib.launcher import host as hostmod
from tests.platforms import windows_only


@windows_only("Windows only")
class GitIgnoredTest(unittest.TestCase):
    def call(self, console, rc=0):
        from kitlib.proc import job
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw, argv=argv)
            return subprocess.CompletedProcess(argv, rc, '', '')
        with mock.patch.object(job, "has_console", return_value=console), \
                mock.patch.object(hostmod.subprocess, "run", fake_run):
            answer = hostmod.Host(home="h", environ={}).git_ignored("repo", "p")
        return answer, seen

    def test_no_console_passes_no_window(self):
        from kitlib.proc import win32
        answer, seen = self.call(console=False)
        self.assertEqual(seen["creationflags"], win32.CREATE_NO_WINDOW)
        self.assertEqual(seen["argv"], ['git', '-C', 'repo', 'check-ignore', '-q', 'p'])
        self.assertIs(answer, True)

    def test_console_parent_unchanged(self):
        answer, seen = self.call(console=True, rc=1)
        self.assertEqual(seen["creationflags"], 0)
        self.assertIs(answer, False)


if __name__ == "__main__":
    unittest.main()
