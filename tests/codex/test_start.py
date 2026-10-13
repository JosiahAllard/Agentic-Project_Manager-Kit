"""hosts.codex.adapter.start: what a Codex start hook adds to the context (ported from the Codex
branch's session_adapter tests in tests/codex/test_entries.py)."""
import io
import json
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

    def test_a_child_is_told_it_runs_under_its_parents_mark(self):
        got = start.text('SubagentStart', KIT, {'cwd': 'C:/r'}, {'PM_KIT_ROLE': 'coordinator'})
        self.assertEqual(got, header.text(KIT) + '\n' + start.CHILD_NOTE)

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


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
