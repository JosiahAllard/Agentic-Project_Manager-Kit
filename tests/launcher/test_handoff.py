"""The hand-off note a successor is told to read (docs/launcher.md "Hand-off")."""
import unittest

from kitlib.launcher import handoff, start
from kitlib.launcher.settings import LaunchError
from tests.launcher.fakes import FakeHost, Home

AGENT = {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'state': 'entry-state.md', 'account': 1,
         'name': 'alpha-coord-a1'}
PLAIN = {'repo': 'alpha', 'agent': False, 'stem': 'alpha-help', 'state': 'entry-state.md', 'brief': 'b.md',
         'account': 1, 'name': 'alpha-help-a1'}


class SuccessorPrompt(unittest.TestCase):
    def test_a_given_state_replaces_the_entrys(self):
        for entry in (AGENT, PLAIN):
            text = handoff.successor_prompt(entry, 'old-a1', 'new-a2', state='Z/state-r2.md')
            self.assertIn('state-r2.md', text)
            self.assertNotIn('entry-state.md', text)

    def test_without_state_the_entrys_is_named(self):
        for entry in (AGENT, PLAIN):
            text = handoff.successor_prompt(entry, 'old-a1', 'new-a2')
            self.assertIn('entry-state.md', text)


class PmName(unittest.TestCase):
    def test_the_successor_is_told_to_check_in_with_the_pm_session_by_both_live_names(self):
        for entry in (AGENT, PLAIN):
            text = handoff.successor_prompt(entry, 'old-a1', 'new-a2')
            self.assertIn("'Project Manager' in the desktop app, or 'pm-session' when the launcher resumed it", text)

    def test_an_entry_started_without_a_prompt_names_both_too(self):
        h = Home(self, accounts=('1', '2'))
        h.write_state('sessions.json', {'alpha': dict(AGENT)})
        host = FakeHost(h.root)
        start.start_entry(h.settings, host, 'alpha', account='2')
        text, = host.opened
        self.assertIn("''Project Manager'' in the desktop app, or ''pm-session'' when the launcher resumed it", text)


class HandoffState(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, accounts=('1', '2'))
        self.h.write_state('sessions.json', {'alpha': dict(AGENT)})
        self.h.write_state('coordinators.json', {'alpha': 1})
        self.host = FakeHost(self.h.root)

    def test_the_state_reaches_the_successors_first_message(self):
        handoff.handoff(self.h.settings, self.host, 'alpha', '2', whatif=True, state='Z/state-r2.md')
        self.assertEqual(self.host.opened, [])
        out = handoff.handoff(self.h.settings, self.host, 'alpha', '2', state='Z/state-r2.md')
        text, = self.host.opened
        self.assertIn('state-r2.md', text)
        self.assertNotIn('entry-state.md', text)
        self.assertEqual(out[0], '== alpha : alpha-coord-a1 (account 1) -> alpha-coord-a2 (account 2)')

    def test_state_with_all_is_refused(self):
        with self.assertRaisesRegex(LaunchError, '--state takes one --session'):
            handoff.handoff(self.h.settings, self.host, all_=True, account='2', state='Z/state-r2.md')
        self.assertEqual(self.host.opened, [])


if __name__ == '__main__':
    unittest.main()
