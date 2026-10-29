"""Renaming a coordinator's stem: handoff --stem and coordinators.json. The old coordinator name's one-release guards (the old state file name, the old stem, the old
task name) are gone; nothing here reads or refuses them."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

from kitlib.launcher import autostart, handoff, lookup, reboot, start, state
from kitlib.launcher.settings import LaunchError
from tests.home import machine_config
from tests.launcher.fakes import FakeHost, Home, role_of, session

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402

ALPHA = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-lead', 'state': 's.md', 'account': 1,
         'name': 'alpha-lead-a1'}


class StateFile(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def test_the_file_is_read(self):
        self.h.write_state('coordinators.json', {'alpha': 2})
        self.assertEqual(state.read_coordinators(self.h.settings), {'alpha': 2})

    def test_no_file_is_none(self):
        self.assertIsNone(state.read_coordinators(self.h.settings))

    def test_no_rename_refusal_is_left(self):
        self.assertFalse(hasattr(state, 'RenameNeeded'))
        self.assertFalse(hasattr(state, 'OLD_COORDINATORS'))
        self.assertFalse(hasattr(lookup, 'legacy_stems'))
        self.assertFalse(hasattr(autostart, 'OLD_TASK_NAME'))


class HandoffStem(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2'))
        self.h.write_state('sessions.json', {'alpha': dict(ALPHA)})
        self.h.write_state('coordinators.json', {'alpha': 1})
        self.old_run = session(10, 'alpha-lead-a1', agent='coordinator')
        self.host = FakeHost(self.h.root, [self.old_run])

    def test_a_new_stem_names_the_successor_and_is_written_after_the_start(self):
        out = handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem='alpha-coordinator')
        self.assertIn('== alpha : alpha-lead-a1 (account 1) -> alpha-coordinator-a2 (account 2)', out)
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
        self.assertIn("stop --name 'alpha-lead-a1' --successor 'alpha-coordinator-a1'", out[-1])
        self.assertEqual(handoff.successor_name(ALPHA, '1', True, 'alpha-lead'), 'alpha-lead-a1-r2')

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
        self.assertEqual(self.host.opened, [])
        self.assertEqual(self.h.read_state('sessions.json'), before)

    def test_handoff_starts_beside_the_old_session(self):
        handoff.handoff(self.h.settings, self.host, 'alpha', '2', stem='alpha-coordinator')
        self.assertEqual(len(self.host.opened), 1)


class RenamedStem(unittest.TestCase):
    """After handoff --stem, only the entry's own stem holds start --session and after-reboot: a
    session under a different stem is another session. after-reboot starts plain entries only."""

    def setUp(self):
        self.h = Home(self)
        self.renamed = dict(ALPHA, stem='alpha-coordinator', name='alpha-coordinator-a1')
        self.h.write_state('sessions.json', {'alpha': self.renamed})

    def test_start_session_refuses_while_its_own_stem_runs(self):
        host = FakeHost(self.h.root, [session(10, 'alpha-coordinator-a2-r3', agent='coordinator')])
        with self.assertRaisesRegex(LaunchError, 'already running'):
            start.start_entry(self.h.settings, host, 'alpha')
        self.assertEqual(host.opened, [])

    def test_start_session_goes_ahead_beside_another_stem(self):
        host = FakeHost(self.h.root, [session(10, 'alpha-lead-a1', agent='coordinator')])
        start.start_entry(self.h.settings, host, 'alpha')
        text, = host.opened
        self.assertIn("-n 'alpha-coordinator-a1'", text)

    def test_after_reboot_starts_beside_another_stem(self):
        self.h.write_state('sessions.json', {'alpha': dict(self.renamed, agent=False)})
        host = FakeHost(self.h.root, [session(10, 'alpha-lead-a1')])
        out = reboot.run(self.h.settings, host)
        self.assertIn('started alpha-coordinator-a1 (alpha) on account 1', out)
        self.assertFalse(any(line.startswith('alpha ') for line in out), out)
        self.assertEqual([role_of(t) for t in host.opened], ['plain'])


class Cli(unittest.TestCase):
    def test_handoff_stem_reaches_the_library(self):
        h = Home(self)
        machine_config(h.root, {'launcher': h.obj}, case=self)
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
