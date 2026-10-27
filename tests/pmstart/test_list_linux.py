"""kitlib.pmstart on Linux (docs/linux-support-plan.md slice 11): list_processes reads /proc
(kitlib.platform.linux.process.rows) instead of PowerShell, and session_images is the running
platform's process_names over every host. kitlib.platform.NAME is patched to 'linux', so this runs
on Windows."""
import unittest
from unittest import mock

from kitlib import pmstart
from kitlib.platform.linux import process as linux_process

ROW = {'Name': 'claude', 'CommandLine': 'claude -n x', 'ExecutablePath': '/usr/bin/claude',
       'ProcessId': 7, 'ParentProcessId': 1, 'Created': '2026-10-27T10:00:00+00:00'}


class Linux(unittest.TestCase):
    def setUp(self):
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)

    def test_list_processes_reads_proc_and_runs_no_powershell(self):
        with mock.patch.object(linux_process, 'rows', return_value=[ROW]) as rows, \
                mock.patch.object(pmstart.spawn, 'run') as run:
            self.assertEqual(pmstart.list_processes(), [ROW])
        rows.assert_called_once_with()
        run.assert_not_called()

    def test_live_session_names_skip_a_node_that_runs_no_hosts_cli(self):
        rows = [dict(ROW, Name='node', CommandLine='node app.js --remote-control X'),
                dict(ROW, Name='node', CommandLine='node /usr/local/bin/claude --remote-control Y'),
                dict(ROW, CommandLine='claude --remote-control Z'),
                dict(ROW, Name='node', CommandLine='node /usr/lib/node_modules/@openai/codex/bin/codex.js '
                                                   '--remote-control W')]
        self.assertEqual(pmstart.live_session_names(rows), {'Y', 'Z', 'W'})

    def test_session_images_are_the_linux_process_names(self):
        self.assertEqual(pmstart.session_images(), frozenset({'claude', 'node', 'codex'}))


class Windows(unittest.TestCase):
    def test_session_images_on_windows_are_the_session_processes(self):
        with mock.patch('kitlib.platform.NAME', 'windows'):
            self.assertEqual(pmstart.session_images(), frozenset({'claude.exe', 'codex.exe'}))


if __name__ == '__main__':
    unittest.main()
