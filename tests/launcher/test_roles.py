"""Every session kind the launcher opens carries a PM_KIT_ROLE mark (kitlib.sessionmark): each test
opens the window through a fake host and reads the mark out of the window's script."""
import os
import unittest

from kitlib import sessionmark
from kitlib.launcher import reboot, start, window
from kitlib.lockmcp import PLUGIN_NAME
from tests.launcher.fakes import FakeHost, Home, role_of

P = PLUGIN_NAME + ':'


class RoleFor(unittest.TestCase):
    def test_the_mapping(self):
        self.assertEqual(window.role_for(None), 'plain')
        self.assertEqual(window.role_for('coordinator'), 'coordinator')
        self.assertEqual(window.role_for('conductor'), 'conductor')     # the retired old name: its own name
        self.assertEqual(window.role_for('lead', 'lead'), 'coordinator')
        self.assertEqual(window.role_for('deep-worker'), 'deep-worker')
        self.assertEqual(window.role_for('consultant'), 'consultant')
        self.assertEqual(window.role_for('analyst'), 'analyst')

    def test_the_plugin_prefixed_agent_gets_the_bare_agents_mark(self):
        # docs/guard.md "Agent names": the mark never carries the prefix
        self.assertEqual(window.role_for(P + 'deep-worker'), 'deep-worker')
        self.assertEqual(window.role_for(P + 'consultant'), 'consultant')
        self.assertEqual(window.role_for(P + 'conductor'), 'conductor')
        self.assertEqual(window.role_for(P + 'analyst'), 'analyst')
        self.assertEqual(window.role_for(P + 'coordinator', P + 'coordinator'), 'coordinator')
        self.assertEqual(window.role_for('lead', P + 'lead'), 'coordinator')
        self.assertEqual(window.role_for(P + 'lead', 'lead'), 'coordinator')
        # another plugin's prefix is not read off
        self.assertEqual(window.role_for('other:deep-worker'), 'other:deep-worker')

    def test_the_env_name_is_the_session_mark_and_a_script_needs_a_role(self):
        self.assertEqual(window.ROLE_ENV, sessionmark.ENV)
        with self.assertRaises(ValueError):
            window.script('n', '', '/r', 'claude')
        text = window.script("it's", 'plain', '/r', 'claude')
        self.assertEqual(role_of(text), 'plain')
        self.assertIn("$env:PM_KIT_SESSION_NAME = 'it''s'", text)


class EveryKind(unittest.TestCase):
    def setUp(self):
        self.h = Home(self, repos=('alpha',))
        self.host = FakeHost(self.h.root)

    def only_window(self):
        self.assertEqual(len(self.host.opened), 1)
        return self.host.opened[0]

    def test_start_runs_the_coordinator_agent_as_coordinator(self):
        out = start.start(self.h.settings, self.host, repo='alpha', account='1')
        text = self.only_window()
        self.assertEqual(role_of(text), 'coordinator')
        self.assertIn('--agent pm-kit:coordinator', text)
        self.assertIn('alpha-coordinator-a1', out[0])

    def test_a_renamed_coordinator_agent_is_still_coordinator(self):
        self.h.obj['coordinator_agent'] = 'conductor'
        s = type(self.h.settings)(self.h.obj, self.h.root)
        start.start(s, self.host, repo='alpha', account='1')
        text = self.only_window()
        self.assertEqual(role_of(text), 'coordinator')
        self.assertIn('--agent conductor', text)

    def test_start_with_another_agent_marks_that_agent(self):
        start.start(self.h.settings, self.host, repo='alpha', account='2', agent='analyst')
        self.assertEqual(role_of(self.only_window()), 'analyst')

    def test_start_with_a_plugin_prefixed_agent_marks_the_bare_agent(self):
        out = start.start(self.h.settings, self.host, repo='alpha', account='2', agent=P + 'analyst')
        text = self.only_window()
        self.assertEqual(role_of(text), 'analyst')
        self.assertIn('--agent %sanalyst ' % P, text)       # started as given
        self.assertIn('alpha-analyst-a2', out[0])            # named without the prefix

    def test_start_without_an_agent_is_plain(self):
        start.start(self.h.settings, self.host, repo='alpha', account='1', no_agent=True)
        text = self.only_window()
        self.assertEqual(role_of(text), 'plain')
        self.assertNotIn('--agent', text)

    def test_what_if_prints_the_marked_script_and_opens_nothing(self):
        out = start.start(self.h.settings, self.host, repo='alpha', account='1', whatif=True)
        self.assertEqual(self.host.opened, [])
        self.assertEqual(role_of('\n'.join(out)), 'coordinator')

    def test_deep_is_deep_worker(self):
        start.deep(self.h.settings, self.host, self.h.repos['alpha'], 'alpha-coordinator-a1', 'race-fix',
                   brief_text='find it', account='1', brief_dir=os.path.join(self.h.root, 'briefs'))
        text = self.only_window()
        self.assertEqual(role_of(text), 'deep-worker')
        self.assertIn('--agent pm-kit:deep-worker', text)

    def test_consultant_is_consultant(self):
        start.consultant(self.h.settings, self.host, self.h.repos['alpha'], account='2')
        text = self.only_window()
        self.assertEqual(role_of(text), 'consultant')
        self.assertIn("-n 'consultant-alpha'", text)

    def test_after_reboot_marks_plain_entries_and_the_pm_and_leaves_coordinators_to_the_hub(self):
        self.h.write_state('sessions.json', {
            '_note': 'skipped',
            'alpha': {'repo': 'alpha', 'agent': True, 'stem': 'alpha-coord', 'state': 's.md', 'account': 1,
                      'name': 'alpha-coord-a1'},
            'helper': {'repo': 'alpha', 'agent': False, 'stem': 'alpha-helper', 'brief': 'b.md', 'state': 's.md',
                       'account': 1, 'name': 'alpha-helper-a1'}})
        self.h.write_state('coordinators.json', {'alpha': 1, 'pm_session_id': 'abc-123', 'pm_folder': self.h.root,
                                               'pm_account': 2})
        reboot.run(self.h.settings, self.host)
        roles = sorted(role_of(t) for t in self.host.opened)
        self.assertEqual(roles, ['plain', 'pm'])
        pm = [t for t in self.host.opened if role_of(t) == 'pm'][0]
        self.assertIn("--resume 'abc-123'", pm)
        self.assertEqual(self.host.slept, [reboot.WAIT_SECONDS])

    def test_the_autostart_test_session_is_plain(self):
        reboot.run(self.h.settings, self.host, test=True)
        text = self.only_window()
        self.assertEqual(role_of(text), 'plain')
        self.assertIn("-n 'autostart-test'", text)

    def test_a_start_from_a_sessions_entry_with_a_named_agent(self):
        self.h.write_state('sessions.json', {
            'rev': {'repo': 'alpha', 'agent': 'analyst', 'stem': 'alpha-rev', 'state': 's.md', 'account': 2,
                    'name': 'alpha-rev-a2'}})
        out = start.start_entry(self.h.settings, self.host, 'rev')
        self.assertEqual(role_of(self.only_window()), 'analyst')
        self.assertIn('alpha-rev-a2', out[0])


if __name__ == '__main__':
    unittest.main()
