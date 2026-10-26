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


def _skills(root, skills):
    for name, body in skills.items():
        os.makedirs(os.path.join(root, 'skills', name))
        with open(os.path.join(root, 'skills', name, 'SKILL.md'), 'w') as fh:
            fh.write(body)


class Stale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kit = os.path.join(self.tmp.name, 'kit')
        _skills(os.path.join(self.kit, 'hosts', 'codex', 'shell'), {'a': 'A', 'b': 'B', 'c': 'C'})
        self.plugin = os.path.join(self.tmp.name, 'cache')

    def test_a_copy_behind_the_kit_is_named_in_the_session_start_text(self):
        _skills(self.plugin, {'a': 'A', 'b': 'old', 'gone': 'G'})
        got = start.text('SessionStart', self.kit, {}, {}, plugin_root=self.plugin)
        self.assertIn('missing: c; extra: gone; changed: b', got)
        self.assertIn("see hosts/codex/README.md 'Known limits'", got)

    def test_a_long_list_is_cut_to_three_names(self):
        _skills(self.plugin, {'x%d' % i: 'x' for i in range(5)})
        self.assertIn('extra: x0, x1, x2 +2 more', start.stale_line(self.plugin, self.kit))

    def test_an_unreadable_plugin_root_is_one_could_not_check_line_never_a_raise(self):
        os.makedirs(self.plugin)
        got = start.text('SessionStart', self.kit, {}, {}, plugin_root=self.plugin)
        self.assertIn('Codex plugin copy: could not check:', got)

    def test_a_matching_copy_and_the_checkouts_own_shell_are_silent(self):
        _skills(self.plugin, {'a': 'A', 'b': 'B', 'c': 'C'})
        self.assertEqual(start.stale_line(self.plugin, self.kit), '')
        real = os.path.join(os.path.dirname(os.path.abspath(start.__file__)), '..', 'shell')
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(start.__file__)))))
        self.assertEqual(start.stale_line(real, root), '')

    def test_the_plugin_root_comes_from_the_hook_entry_path(self):
        self.assertEqual(start.hook_plugin_root([os.path.join(self.plugin, 'boot', 'hook.py')]), self.plugin)
        self.assertIsNone(start.hook_plugin_root(['x/test_start.py']))


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
