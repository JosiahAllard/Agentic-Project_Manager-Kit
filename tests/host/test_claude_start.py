"""hosts.claude.adapter.start: the text Claude Code's start hooks add, as ADAPTER.start_text gives it
(tools/agent_view.py "At start"): the header, then the coordinator rules or the PM checklist at a
SessionStart of a session marked with that role, calling what hooks/coordinator_session_start.py
and hooks/pm_session_start.py call."""
import os
import unittest
from unittest import mock

from kitlib import coordinatorstart, pmstart
from kitlib.config import user
from kitlib.host import header

from hosts.claude.adapter import start
from hosts.claude.adapter.host import ADAPTER

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEAD = header.text(KIT)


class Start(unittest.TestCase):
    def test_the_adapter_hands_out_the_module(self):
        self.assertEqual(ADAPTER.start_text('SubagentStart', KIT, {}, {}), HEAD)

    def test_an_unmarked_session_and_any_subagent_get_the_header_alone(self):
        self.assertEqual(start.text('SessionStart', KIT, {'cwd': KIT}, {}), HEAD)
        self.assertEqual(start.text('SubagentStart', KIT, {'cwd': KIT}, {'PM_KIT_ROLE': 'pm'}), HEAD)

    def test_a_coordinator_session_gets_the_coordinator_rules(self):
        env = {'PM_KIT_ROLE': 'coordinator'}
        with mock.patch.object(coordinatorstart, 'render', return_value='RULES') as render:
            self.assertEqual(start.text('SessionStart', KIT, {'cwd': KIT}, env), HEAD + '\nRULES')
        render.assert_called_once_with(env=env, cwd=KIT)

    def test_a_pm_session_gets_the_checklist(self):
        with mock.patch.object(pmstart, 'load_config', return_value={'x': 1}), \
                mock.patch.object(pmstart, 'checklist', return_value='LIST') as checklist:
            self.assertEqual(start.text('SessionStart', KIT, {}, {'PM_KIT_ROLE': 'pm'}), HEAD + '\nLIST')
        checklist.assert_called_once_with({'x': 1})
        with mock.patch.object(pmstart, 'load_config', return_value=None), \
                mock.patch.object(user, 'display', return_value='CONFIG'):
            self.assertIn('checklist skipped', start.text('SessionStart', KIT, {}, {'PM_KIT_ROLE': 'pm'}))

    def test_a_failing_check_is_a_line(self):
        with mock.patch.object(pmstart, 'load_config', side_effect=OSError('gone')):
            self.assertEqual(start.text('SessionStart', KIT, {}, {'PM_KIT_ROLE': 'pm'}),
                             HEAD + '\ncould not check: OSError: gone')


if __name__ == '__main__':
    unittest.main()
