"""Finding a session by its Remote Control name, and stopping one with its window."""
import os
import unittest

from kitlib.launcher import lookup, stop, window
from tests.launcher.fakes import FakeHost, Home, session

STORE = 'C:' + chr(92) + 'Program Files' + chr(92) + 'WindowsApps' + chr(92) + 'claude.exe'


class Lookup(unittest.TestCase):
    def test_rc_name_parses_every_quoting(self):
        self.assertEqual(lookup.rc_name("claude --remote-control 'a b' -n x"), 'a b')
        self.assertEqual(lookup.rc_name('claude --remote-control "q" -n x'), 'q')
        self.assertEqual(lookup.rc_name('claude --remote-control=bare-a1 -n x'), 'bare-a1')
        self.assertIsNone(lookup.rc_name('claude --resume x'))
        self.assertIsNone(lookup.rc_name(None))

    def test_exact_whole_name_only_and_never_the_store_app(self):
        procs = [session(1, 'x-a1'), session(2, 'x-a1-r2'),
                 dict(session(3, 'x-a1'), ExecutablePath=STORE), dict(session(4, 'x-a1'), Name='node.exe')]
        live = lookup.claude_sessions(procs)
        self.assertEqual([p['ProcessId'] for p in lookup.find_exact(live, 'x-a1')], [1])
        self.assertEqual(lookup.pids(lookup.find_exact(live, 'x-a1-r2')), '2')
        self.assertTrue(lookup.running_stem(procs, 'x-'))
        self.assertFalse(lookup.running_stem(procs, 'y-'))


def rc_off(pid, name, parent=1):
    """A session launched with `start --no-remote-control`: found by -n."""
    return dict(session(pid, name, parent=parent),
                CommandLine="claude --agent coordinator --permission-mode auto -n '%s' 'x'" % name)


def windowed(name, pid=10, win=20, con=30, rc=True):
    """A session pid, its powershell window (the script the launcher writes) and conhost."""
    text = window.script(name, 'coordinator', '/r', window.claude_command(name, 'go', 'coordinator',
                                                                         remote_control=rc))
    return [rc_off(pid, name, win) if not rc else session(pid, name, parent=win),
            {'Name': 'powershell.exe', 'ProcessId': win, 'ParentProcessId': con,
             'CommandLine': 'powershell.exe -NoExit -EncodedCommand ' + window.encode(text)},
            {'Name': 'conhost.exe', 'ProcessId': con, 'ParentProcessId': 4, 'CommandLine': 'conhost.exe x'}]


class RcOff(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def test_lookup_finds_an_rc_off_session_by_name_and_only_whole_names(self):
        procs = [rc_off(1, 'x-a1'), rc_off(2, 'x-a1-r2')]
        live = lookup.claude_sessions(procs)
        self.assertEqual([p['ProcessId'] for p in lookup.find_exact(live, 'x-a1')], [1])
        self.assertTrue(lookup.running_stem(procs, 'x-'))

    def test_an_unrelated_claude_matches_no_name(self):
        other = dict(session(5, 'x'), CommandLine='claude --resume abc --permission-mode auto')
        self.assertIsNone(lookup.rc_name(other['CommandLine']))
        self.assertEqual(lookup.find_exact(lookup.claude_sessions([other]), 'x'), [])
        self.assertEqual(lookup.find_exact(lookup.claude_sessions([other]), 'abc'), [])
        host = FakeHost(self.h.root, [other])
        with self.assertRaises(stop.Refused):
            stop.stop(self.h.settings, host, 'x')
        self.assertEqual(host.killed, [])

    def test_stop_stops_an_rc_off_session_and_its_window(self):
        host = FakeHost(self.h.root, windowed('x-a1', rc=False))
        out = stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed, [(10, 1010), (20, 1020), (30, 1030)])
        self.assertIn("stopped 'x-a1' claude PID 10, window PID 20, conhost PID 30", out[0])

    def test_rc_wins_over_n_when_both_are_given(self):
        self.assertEqual(lookup.rc_name("claude --remote-control 'a' -n 'b'"), 'a')


class Stop(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def host(self, procs, **kw):
        return FakeHost(self.h.root, procs, **kw)

    def test_stops_the_session_its_window_and_conhost_by_pid_and_creation_time_and_logs(self):
        host = self.host(windowed('x-a1') + [session(40, 'x-a2')])
        out = stop.stop(self.h.settings, host, 'x-a1', successor='x-a2')
        self.assertEqual(host.killed, [(10, 1010), (20, 1020), (30, 1030)])
        self.assertIn("stopped 'x-a1' claude PID 10, window PID 20, conhost PID 30; successor 'x-a2'", out[0])
        with open(os.path.join(self.h.state, 'stop.log'), encoding='utf-8') as f:
            self.assertIn(out[0], f.read())

    def test_dry_run_stops_nothing(self):
        host = self.host(windowed('x-a1'))
        out = stop.stop(self.h.settings, host, 'x-a1', dry_run=True)
        self.assertEqual(host.killed, [])
        self.assertEqual(out[-1], 'DryRun: nothing stopped.')
        self.assertIn('window PID 20 (powershell.exe, same name) and its conhost.exe PID 30', out[1])

    def test_a_window_without_the_name_is_left_open(self):
        procs = windowed('x-a1')
        procs[1]['CommandLine'] = 'powershell.exe -NoExit'
        host = self.host(procs)
        stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed, [(10, 1010)])

    def test_a_reused_pid_is_not_stopped(self):
        host = self.host(windowed('x-a1'))
        host.command_line = lambda pid, ct: "claude --remote-control 'other'"
        with self.assertRaisesRegex(stop.LaunchError, 'no longer the session'):
            stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed, [])

    def refused(self, pattern, name, procs, **kw):
        host = self.host(procs, own_pid=kw.pop('own_pid', 99999))
        with self.assertRaisesRegex(stop.Refused, pattern):
            stop.stop(self.h.settings, host, name, **kw)
        self.assertEqual(host.killed, [])

    def test_refusals(self):
        self.refused('names the PM session', 'pm-session', windowed('pm-session'))
        self.refused('names the PM session', 'x-a1', windowed('x-a1'), successor='Project Manager')
        self.refused('no surrounding spaces', ' x-a1', windowed('x-a1'))
        self.refused('--successor equals --name', 'x-a1', windowed('x-a1'), successor='x-a1')
        self.refused("successor 'x-a2' is not running", 'x-a1', windowed('x-a1'), successor='x-a2')
        self.refused("no claude.exe with --remote-control exactly 'x-a'", 'x-a', windowed('x-a1'))
        self.refused("2 claude.exe match 'x-a1'", 'x-a1', windowed('x-a1') + [session(11, 'x-a1')])
        self.refused('PID 12 is not a claude.exe', 'x-a1', windowed('x-a1') + [session(11, 'x-a1')], pid=12)
        self.refused("own ancestor", 'x-a1', windowed('x-a1') + [{'Name': 'bash.exe', 'ProcessId': 50,
                                                                  'ParentProcessId': 10}], own_pid=50)

    def test_the_pm_name_match_ignores_case_as_powershell_match_did(self):
        self.refused('names the PM session', 'PM-Session', windowed('PM-Session'))
        self.refused('names the PM session', 'x-a1', windowed('x-a1'), successor='old project manager')

    def break_the_log(self):
        os.rmdir(self.h.state)
        open(self.h.state, 'w').close()                  # the state folder is a file: the log write fails

    def test_a_failed_stop_log_write_goes_to_warn_and_the_library_prints_nothing(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        self.break_the_log()
        host = self.host(windowed('x-a1'))
        said, printed = [], io.StringIO()
        with redirect_stderr(printed), redirect_stdout(printed):
            out = stop.stop(self.h.settings, host, 'x-a1', warn=said.append)
        self.assertEqual(len(host.killed), 3)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("stopped 'x-a1'"))
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith('could not write stop.log: '), said)
        self.assertEqual(printed.getvalue(), '')

    def test_without_warn_a_failed_log_write_is_the_last_line(self):
        self.break_the_log()
        out = stop.stop(self.h.settings, self.host(windowed('x-a1')), 'x-a1')
        self.assertEqual(len(out), 2)
        self.assertTrue(out[1].startswith('could not write stop.log: '), out)

    def test_pid_picks_one_of_several(self):
        host = self.host(windowed('x-a1') + [session(11, 'x-a1', parent=0)])
        out = stop.stop(self.h.settings, host, 'x-a1', pid=11)
        self.assertEqual(host.killed, [(11, 1011)])
        self.assertTrue(out[0].endswith('(picked by --pid)'))

    def test_the_pm_session_id_from_coordinators_json_is_refused_and_a_placeholder_ignored(self):
        procs = windowed('x-a1')
        procs[0]['CommandLine'] += ' --resume sid-1'
        self.h.write_state('coordinators.json', {'pm_session_id': 'sid-1'})
        self.refused('PID 10 is the PM session', 'x-a1', procs)
        self.h.write_state('coordinators.json', {'pm_session_id': '<the PM session id>'})
        host = self.host(procs)
        stop.stop(self.h.settings, host, 'x-a1', dry_run=True)

    def test_a_pm_session_id_that_is_not_a_string_counts_as_unset(self):
        for value in (None, 12345, ['sid-1']):
            self.h.write_state('coordinators.json', {'pm_session_id': value})
            host = self.host(windowed('x-a1'))
            out = stop.stop(self.h.settings, host, 'x-a1')
            self.assertEqual(host.killed, [(10, 1010), (20, 1020), (30, 1030)], value)
            self.assertTrue(out[0].startswith("stopped 'x-a1'"), out)


if __name__ == '__main__':
    unittest.main()
