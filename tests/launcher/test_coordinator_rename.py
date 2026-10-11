"""The conductor-to-coordinator rename, part 1: coordinators.json with a refusal for the old name,
handoff --stem, the one-release guard against an old-stem session, and the autostart task rename."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kitlib.config import user as kuser
from kitlib.launcher import autostart, handoff, lookup, reboot, start, state, stop
from kitlib.launcher.settings import LaunchError
from tests.launcher.fakes import FakeHost, Home, role_of, session

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

OLD = 'conductors.json'
ALPHA = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-conductor', 'state': 's.md', 'account': 1,
         'name': 'alpha-conductor-a1'}
PM = {'pm_session_id': 'pm-sid-1', 'pm_account': 2}


class StateFile(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def test_the_new_name_is_read(self):
        self.h.write_state('coordinators.json', {'alpha': 2})
        self.assertEqual(state.read_coordinators(self.h.settings), {'alpha': 2})

    def test_neither_file_is_none(self):
        self.assertIsNone(state.read_coordinators(self.h.settings))

    def test_the_old_name_alone_is_refused_and_never_read(self):
        self.h.write_state(OLD, {'alpha': 2})
        with self.assertRaisesRegex(state.RenameNeeded, 'rename .*conductors.json to coordinators.json'):
            state.read_coordinators(self.h.settings)

    def test_both_files_read_the_new_one(self):
        self.h.write_state(OLD, {'alpha': 1})
        self.h.write_state('coordinators.json', {'alpha': 2})
        self.assertEqual(state.read_coordinators(self.h.settings), {'alpha': 2})


class HandoffStem(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2'))
        self.h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        self.h.write_state('coordinators.json', {'alpha': 1})
        self.old_run = session(10, 'alpha-conductor-a1', agent='conductor')
        self.host = FakeHost(self.h.root, [self.old_run])

    def test_a_new_stem_names_the_successor_and_is_written_after_the_start(self):
        out = handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem='alpha-coordinator')
        self.assertIn('== alpha : alpha-conductor-a1 (account 1) -> alpha-coordinator-a2 (account 2)', out)
        text, = self.host.opened
        self.assertIn("-n 'alpha-coordinator-a2'", text)
        self.assertEqual(role_of(text), 'coordinator')
        written = self.h.read_state('sessions.json')['alpha']
        self.assertEqual((written['stem'], written['name'], written['account']),
                         ('alpha-coordinator', 'alpha-coordinator-a2', 2))
        self.assertEqual(self.h.read_state('coordinators.json'), {'alpha': 2})

    def test_a_recycle_under_a_new_stem_has_no_generation_and_prints_the_stop_line(self):
        out = handoff.handoff(self.h.settings, self.host, 'alpha', recycle=True, stem='alpha-coordinator')
        self.assertEqual(self.h.read_state('sessions.json')['alpha']['name'], 'alpha-coordinator-a1')
        self.assertIn("stop --name 'alpha-conductor-a1' --successor 'alpha-coordinator-a1'", out[-1])
        self.assertEqual(handoff.successor_name(ALPHA, '1', True, 'alpha-conductor'), 'alpha-conductor-a1-r2')

    def test_a_failed_start_writes_nothing(self):
        before = (self.h.read_state('sessions.json'), self.h.read_state('coordinators.json'))

        def boom(text):
            raise RuntimeError('opening the window failed')
        self.host.open_window = boom
        with self.assertRaises(RuntimeError):
            handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem='alpha-coordinator')
        self.assertEqual((self.h.read_state('sessions.json'), self.h.read_state('coordinators.json')), before)

    def test_what_if_says_the_stem_and_writes_nothing(self):
        before = self.h.read_state('sessions.json')
        out = handoff.handoff(self.h.settings, self.host, 'alpha', '2', whatif=True, stem='alpha-coordinator')
        self.assertIn('Would set sessions.json alpha : account=2 name=alpha-coordinator-a2 stem=alpha-coordinator', out)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.h.read_state('sessions.json'), before)

    def test_refusals_start_and_write_nothing(self):
        before = self.h.read_state('sessions.json')
        with self.assertRaisesRegex(LaunchError, '--stem takes one --session'):
            handoff.handoff(self.h.settings, self.host, None, '2', all_=True, stem='x-coordinator')
        for bad in ('', 'has space', '-lead', 'a/b', 'x' * 65):
            with self.assertRaisesRegex(LaunchError, '--stem'):
                handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem=bad)
        os.remove(os.path.join(self.h.state, 'coordinators.json'))
        self.h.write_state(OLD, {'alpha': 1})
        with self.assertRaisesRegex(state.RenameNeeded, 'coordinators.json'):
            handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem='alpha-coordinator')
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.h.read_state('sessions.json'), before)
        self.assertEqual(self.h.read_state(OLD), {'alpha': 1})


class LegacyGuard(unittest.TestCase):
    """One-release guard: a session under the old conductor stem blocks start --session and
    after-reboot of the renamed entry (removed after the switch, W2b)."""

    def setUp(self):
        self.h = Home(self)
        self.renamed = dict(ALPHA, stem='alpha-coordinator', name='alpha-coordinator-a1')
        self.h.write_state('sessions.json', {'alpha': self.renamed})

    def test_legacy_stems(self):
        self.assertEqual(lookup.legacy_stems('alpha', self.renamed), ['alpha-conductor'])
        self.assertEqual(lookup.legacy_stems('alpha', ALPHA), [])          # its own stem: running_stem covers it
        lane = {'repo': 'alpha', 'agent': True, 'stem': 'lane-coordinator'}
        self.assertEqual(lookup.legacy_stems('lane', lane), ['lane-conductor'])
        self.assertEqual(lookup.legacy_stems('Kit', {'repo': 'Kit', 'agent': True, 'stem': 'kit-lead'}),
                         ['kit-conductor'])
        self.assertEqual(lookup.legacy_stems('help', {'repo': 'alpha', 'agent': False, 'stem': 'alpha-help'}), [])

    def test_start_session_refuses_while_the_old_stem_runs(self):
        host = FakeHost(self.h.root, [session(10, 'alpha-conductor-a2-r3', agent='conductor')])
        with self.assertRaisesRegex(LaunchError, 'old stem alpha-conductor .*one-release guard'):
            start.start_entry(self.h.settings, host, 'alpha')
        self.assertEqual(host.opened, [])

    def test_start_session_goes_ahead_without_it(self):
        host = FakeHost(self.h.root, [session(10, 'beta-conductor-a1', agent='conductor')])
        start.start_entry(self.h.settings, host, 'alpha')
        text, = host.opened
        self.assertIn("-n 'alpha-coordinator-a1'", text)

    def test_after_reboot_skips_the_entry_while_the_old_stem_runs(self):
        host = FakeHost(self.h.root, [session(10, 'alpha-conductor-a1', agent='conductor')])
        out = reboot.run(self.h.settings, host)
        self.assertIn('alpha already running under the old stem alpha-conductor, skipped', out)
        self.assertEqual(host.opened, [])

    def test_handoff_starts_beside_the_old_session(self):
        self.h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        host = FakeHost(self.h.root, [session(10, 'alpha-conductor-a1', agent='conductor')])
        handoff.handoff(self.h.settings, host, 'alpha', '2', stem='alpha-coordinator')
        self.assertEqual(len(host.opened), 1)


class OldStateFileElsewhere(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.h.write_state(OLD, dict(PM, pm_folder=self.h.root))

    def test_after_reboot_logs_the_rename_and_still_starts_the_sessions(self):
        self.h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        host = FakeHost(self.h.root)
        out = reboot.run(self.h.settings, host)
        self.assertTrue(any(line.startswith('FAILED to read coordinators.json : rename ') for line in out), out)
        self.assertEqual([role_of(t) for t in host.opened], ['coordinator'])   # no PM resume

    def test_stop_refuses_rather_than_lose_the_pm_session_id(self):
        host = FakeHost(self.h.root, [session(10, 'x-a1')])
        with self.assertRaisesRegex(stop.Refused, 'rename .*conductors.json to coordinators.json'):
            stop.stop(self.h.settings, host, 'x-a1')
        self.assertEqual(host.killed, [])

    def test_stop_cli_says_refused_and_exits_2(self):
        os.makedirs(kuser.config_dir(self.h.root))
        with open(kuser.config_file(self.h.root), 'w', encoding='utf-8') as f:
            json.dump({'launcher': self.h.obj}, f)
        host = FakeHost(self.h.root, [session(10, 'x-a1')])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = launch.main(['stop', '--name', 'x-a1'], host)
        self.assertEqual(code, 2, err.getvalue())
        self.assertRegex(out.getvalue(), r'REFUSED: rename .*conductors.json to coordinators.json')
        self.assertEqual(host.killed, [])


class Autostart(unittest.TestCase):
    def test_install_registers_the_new_name_then_unregisters_the_old(self):
        out = autostart.install('/kit/tools/launch.py', python='/py/python.exe', whatif=True)
        script = out[1]
        self.assertEqual(autostart.TASK_NAME, 'Claude coordinators after reboot')
        reg = script.index("Register-ScheduledTask -TaskName 'Claude coordinators after reboot'")
        unreg = script.index("Unregister-ScheduledTask -TaskName 'Claude conductors after reboot' -Confirm:$false")
        self.assertLess(reg, unreg)                   # a failed register leaves the old task in place

    def test_the_install_line_names_both(self):
        calls = []

        class R(object):
            returncode, stderr = 0, ''

        def run(*a, **k):
            calls.append(a[0])
            return R()
        out = autostart.install('/kit/tools/launch.py', python='/py/python.exe', run=run)
        self.assertEqual(len(calls), 1)
        self.assertIn("Installed 'Claude coordinators after reboot' and unregistered 'Claude conductors after reboot'",
                      out[0])


class Cli(unittest.TestCase):
    def test_handoff_stem_reaches_the_library(self):
        h = Home(self)
        os.makedirs(kuser.config_dir(h.root))
        with open(kuser.config_file(h.root), 'w', encoding='utf-8') as f:
            json.dump({'launcher': h.obj}, f)
        h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        host = FakeHost(h.root)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = launch.main(['handoff', '--session', 'alpha', '--account', '2', '--stem', 'alpha-coordinator'],
                               host)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(h.read_state('sessions.json')['alpha']['stem'], 'alpha-coordinator')
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = launch.main(['handoff', '--all', '--account', '1', '--stem', 'x'], host)
        self.assertEqual(code, 1)
        self.assertIn('--stem takes one --session', err.getvalue())


if __name__ == '__main__':
    unittest.main()
