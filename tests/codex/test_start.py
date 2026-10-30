"""hosts.codex.adapter.start: what a Codex start hook adds to the context."""
import io
import json
import os
import tempfile
import unittest

from hosts.codex.adapter import ADAPTER, start
from kitlib.host import header

KIT = 'C:/kit'


class Start(unittest.TestCase):
    def test_every_session_gets_the_kit_header(self):
        for env in ({}, {'PM_KIT_ROLE': 'worker'}):
            for event in ('SessionStart', 'PostCompact'):
                with self.subTest(env=env, event=event):
                    self.assertEqual(start.text(event, KIT, {'cwd': 'C:/r'}, env), header.text(KIT))

    def test_a_child_is_told_it_runs_under_its_parents_mark_then_gets_the_agent_start_block(self):
        # Codex hooks carry no child identity, so every child gets the block, named a subagent
        with unittest.mock.patch.object(start.agentstart, 'text', return_value='BLOCK') as block:
            got = start.text('SubagentStart', KIT, {'cwd': 'C:/r'}, {'PM_KIT_ROLE': 'coordinator'})
        self.assertEqual(got, header.text(KIT) + '\n' + start.CHILD_NOTE + '\nBLOCK')
        block.assert_called_once_with('C:/r', KIT)

    def test_a_block_that_raises_is_one_line_after_the_header_and_the_note(self):
        with unittest.mock.patch.object(start.agentstart, 'text', side_effect=RuntimeError('boom')):
            got = start.text('SubagentStart', KIT, {'cwd': 'C:/r'}, {})
        self.assertEqual(got, header.text(KIT) + '\n' + start.CHILD_NOTE
                         + '\nAgent start: could not check: RuntimeError: boom')

    def test_a_session_start_gets_no_agent_start_block(self):
        with unittest.mock.patch.object(start.agentstart, 'text', return_value='BLOCK') as block:
            for event in ('SessionStart', 'PostCompact'):
                start.text(event, KIT, {'cwd': 'C:/r'}, {'PM_KIT_ROLE': 'worker'})
        block.assert_not_called()

    def test_a_lead_gets_the_shared_start_checks(self):
        seen = []

        def checks(role, cwd, **kw):
            seen.append((role, cwd, kw['state_note']))
            return ('machine: idle',)
        with unittest.mock.patch('kitlib.config.load') as load:
            load.return_value.repo = {'name': 'demo'}
            got = start.text('SessionStart', KIT, {'cwd': 'C:/r'},
                             {'PM_KIT_ROLE': 'coordinator', start.STATE_ENV: 'D:/notes/state.md'}, checks=checks)
        self.assertTrue(got.endswith('\nShared start checks (coordinator):\n- machine: idle'))
        self.assertEqual(seen, [('coordinator', 'C:/r', 'D:/notes/state.md')])

    def test_a_check_that_cannot_run_is_reported(self):
        got = start.text('SessionStart', KIT, {'cwd': 'relative'}, {'PM_KIT_ROLE': 'pm'})
        self.assertIn('could not check: ValueError', got)

    def test_the_hook_output_is_codex_context(self):
        out = start.output(ADAPTER, 'PostCompact', KIT, io.StringIO('{"cwd": "C:/r"}'), {})
        self.assertEqual(json.loads(out), {'hookSpecificOutput': {'hookEventName': 'PostCompact',
                                                                  'additionalContext': header.text(KIT)}})
        self.assertEqual(json.loads(start.output(ADAPTER, 'SessionStart', KIT, io.StringIO('{bad'), {}))
                         ['hookSpecificOutput']['additionalContext'], header.text(KIT))


class ExportLayout(unittest.TestCase):
    """Codex installs the whole export: the hook's plugin root is the kit, its skills are under
    content/skills/, and there is no skills/ copy to compare, so the start text has no copy line."""

    def test_a_start_from_the_export_layout_has_no_plugin_copy_line(self):
        with tempfile.TemporaryDirectory() as kit:
            os.makedirs(os.path.join(kit, 'content', 'skills', 'a'))
            hook = os.path.join(kit, 'boot', 'hook.py')
            with unittest.mock.patch('sys.argv', [hook, 'codex', 'SessionStart']):
                got = start.text('SessionStart', kit, {}, {})
            self.assertEqual(got, header.text(kit))
            self.assertNotIn('plugin copy', got)


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
