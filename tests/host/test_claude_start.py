"""ADAPTER.start_text and ADAPTER.start_parts on Claude Code: the text each start hook adds, by its
part (what boot/hook.py writes and tools/agent_view.py "At start" shows), and the parts a role runs:
the plugin's hooks.json (the header, the coordinator rules) for every session, and the PM checklist
from the PM's own settings."""
import os
import unittest
from unittest import mock

from kitlib import coordinatorstart, pmstart
from kitlib.config import user
from kitlib.host import header

from hosts.claude.adapter import content
from hosts.claude.adapter.host import ADAPTER

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEAD = header.text(KIT)


class Start(unittest.TestCase):
    def test_the_plain_hook_is_the_header_alone(self):
        for event in ADAPTER.CONTEXT_EVENTS:
            self.assertEqual(ADAPTER.start_text(event, KIT, {'cwd': KIT}, {'PM_KIT_ROLE': 'pm'}), HEAD)

    def test_the_coordinator_part_is_the_rules_in_a_coordinator_session(self):
        env = {'PM_KIT_ROLE': 'coordinator'}
        with mock.patch.object(coordinatorstart, 'render', return_value='RULES') as render:
            self.assertEqual(ADAPTER.start_text('SessionStart', KIT, {'cwd': KIT}, env, 'coordinator'), 'RULES')
        render.assert_called_once_with(env=env, cwd=KIT)
        self.assertEqual(ADAPTER.start_text('SessionStart', KIT, {'cwd': KIT}, {}, 'coordinator'), '')

    def test_the_pm_part_is_the_checklist(self):
        with mock.patch.object(pmstart, 'load_config', return_value={'x': 1}), \
                mock.patch.object(pmstart, 'checklist', return_value='LIST') as checklist:
            self.assertEqual(ADAPTER.start_text('SessionStart', KIT, {}, {}, 'pm'), 'LIST')
        checklist.assert_called_once_with({'x': 1})
        with mock.patch.object(pmstart, 'load_config', return_value=None), \
                mock.patch.object(user, 'display', return_value='CONFIG'):
            self.assertIn('checklist skipped', ADAPTER.start_text('SessionStart', KIT, {}, {}, 'pm'))

    def test_a_failing_check_is_a_line(self):
        with mock.patch.object(pmstart, 'load_config', side_effect=OSError('gone')):
            self.assertEqual(ADAPTER.start_text('SessionStart', KIT, {}, {}, 'pm'),
                             'PM session start: checklist failed: OSError: gone')

    def test_the_parts_a_role_runs(self):
        plugin = tuple(part for event, _, part in content.HOOKS if event == 'SessionStart')
        self.assertEqual(plugin, (None, 'coordinator'))
        self.assertEqual(ADAPTER.start_parts('SessionStart', 'pm'), plugin + ('pm',))
        self.assertEqual(ADAPTER.start_parts('SessionStart', 'coordinator'), plugin)
        self.assertEqual(ADAPTER.start_parts('SubagentStart', 'pm'), (None,))


if __name__ == '__main__':
    unittest.main()
