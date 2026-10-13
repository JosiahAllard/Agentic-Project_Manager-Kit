"""kitlib.host: the neutral types are frozen and checked, and `ask` degrades one way."""
import dataclasses
import unittest

from kitlib import host
from kitlib.host import Action, Decision, HostCapabilities


def caps(supports_ask):
    return HostCapabilities(('PreToolUse',), supports_ask, True, True, True, 'markdown')


class Types(unittest.TestCase):
    def test_action_kinds_and_verdicts_are_closed_sets(self):
        for kind in host.KINDS:
            self.assertEqual(Action(kind).kind, kind)
        with self.assertRaises(ValueError):
            Action('write')
        for verdict in host.VERDICTS:
            self.assertEqual(Decision(verdict).verdict, verdict)
        with self.assertRaises(ValueError):
            Decision('maybe')

    def test_main_session_has_no_agent_and_values_are_frozen(self):
        a = Action('shell', tool='Bash', command='git status', cwd='C:/r', role='worker')
        self.assertIsNone(a.agent)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.agent = 'x'
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Decision('deny').reason = 'x'

    def test_profile_holds_only_consumed_fields(self):
        self.assertEqual([f.name for f in dataclasses.fields(host.HostProfile)],
                         ['owner_paths', 'worktrees_directory', 'state_directory',
                          'process_names', 'mcp_tool_format', 'session_env',
                          'skills_directory'])


class Degrade(unittest.TestCase):
    def test_ask_becomes_deny_with_its_reason_on_a_host_without_ask(self):
        ask = Decision('ask', 'lock-write', 'the owner approves lock writes', 'tools/x.py')
        self.assertEqual(host.degrade(ask, caps(False)),
                         Decision('deny', 'lock-write', 'the owner approves lock writes', 'tools/x.py'))
        self.assertIs(host.degrade(ask, caps(True)), ask)

    def test_allow_and_deny_pass_unchanged(self):
        for verdict in ('allow', 'deny'):
            d = Decision(verdict, 'r', 'why')
            self.assertIs(host.degrade(d, caps(False)), d)


if __name__ == '__main__':
    unittest.main()
