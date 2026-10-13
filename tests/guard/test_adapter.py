"""Claude Code's adapter (hosts/claude/adapter): its payloads parsed into kitlib.host.Action and the
Decision rendered back, the guard's path normaliser, and the layer rule that keeps Claude's payload
field names out of kitlib.guard (docs/guard.md "Hosts")."""
import json
import os
import re
import unittest

from hosts.claude.adapter import events
from hosts.claude.adapter.host import ADAPTER, MATCHER
from kitlib import agentname
from kitlib.guard import paths, rules
from kitlib.host import Action, Decision, registry

from .world import bash, event, ps

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.dirname(HERE))
P = agentname.PREFIX


class Parse(unittest.TestCase):
    def test_file_tools_are_edits_with_their_own_path_key(self):
        a = ADAPTER.parse('PreToolUse', event('Write', {'file_path': 'C:/r/a.py', 'content': ''}, 'C:/r'))
        self.assertEqual((a.kind, a.tool, a.path, a.cwd), ('edit', 'Write', 'C:/r/a.py', 'C:/r'))
        self.assertEqual(a.retarget, 'Write(file_path="{path}")')
        n = ADAPTER.parse('PreToolUse', event('NotebookEdit', {'notebook_path': 'C:/r/n.ipynb'}, 'C:/r'))
        self.assertEqual((n.kind, n.path, n.retarget), ('edit', 'C:/r/n.ipynb', 'NotebookEdit(notebook_path="{path}")'))

    def test_shell_tools_carry_their_dialect_and_background(self):
        a = ADAPTER.parse('PreToolUse', bash('git status', 'C:/r'))
        self.assertEqual((a.kind, a.command, a.shell, a.background), ('shell', 'git status', 'bash', False))
        self.assertEqual(ADAPTER.parse('PreToolUse', ps('Get-Item x', 'C:/r')).shell, 'powershell')
        bg = event('Bash', {'command': 'sleep 9', 'run_in_background': True}, 'C:/r')
        self.assertTrue(ADAPTER.parse('PreToolUse', bg).background)
        bg['tool_input']['run_in_background'] = 'true'           # only the JSON true backgrounds
        self.assertFalse(ADAPTER.parse('PreToolUse', bg).background)

    def test_mcp_and_other_tools(self):
        self.assertEqual(ADAPTER.parse('PreToolUse', event('mcp__x__lock_free', {}, 'C:/r')).kind, 'mcp')
        for tool in ('Read', 'Grep', 'Agent', ''):
            self.assertEqual(ADAPTER.parse('PreToolUse', event(tool, {}, 'C:/r')).kind, 'other', tool)
        self.assertEqual(ADAPTER.parse('PreToolUse', None), Action('other'))
        self.assertEqual(ADAPTER.parse('SessionStart', {'cwd': 'C:/r'}), Action('session_start', cwd='C:/r'))
        self.assertEqual(ADAPTER.parse('SubagentStart', {'agent_id': 'a'}).kind, 'subagent_start')

    def test_the_subagent_and_its_role(self):
        a = ADAPTER.parse('PreToolUse', bash('x', 'C:/r', sub=True))
        self.assertEqual((a.agent, a.agent_role, a.role), ('a0335183081908f5a', 'senior-worker', None))
        for kind, want in (('lander', 'lander'), (P + 'lander', 'lander'), (P + P + 'lander', P + 'lander'),
                           ('other:lander', 'other:lander')):
            ev = dict(bash('x', 'C:/r'), agent_id='a1', agent_type=kind)
            self.assertEqual(ADAPTER.parse('PreToolUse', ev).agent_role, want, kind)
        # the main session's agent_type is not read: it names no role
        main = dict(bash('x', 'C:/r'), agent_type='lander')
        self.assertEqual((ADAPTER.parse('PreToolUse', main).agent, ADAPTER.parse('PreToolUse', main).agent_role),
                         (None, None))
        self.assertIsNone(ADAPTER.parse('PreToolUse', dict(main, agent_id='')).agent)

    def test_a_malformed_payload_raises(self):
        for bad in ('x', {'tool_name': 5}, {'tool_name': 'Bash', 'tool_input': ['x']}):
            with self.assertRaises((AttributeError, ValueError, TypeError), msg=bad):
                ADAPTER.parse('PreToolUse', bad)
        # a tool that reads no tool_input does not trip on its shape, as before the split
        self.assertEqual(ADAPTER.parse('PreToolUse', {'tool_name': 'mcp__x__y', 'tool_input': ['x']}).kind, 'mcp')

    def test_agent_reads_the_identity_alone(self):
        self.assertEqual(events.agent({'agent_id': 'a1'}), 'a1')
        self.assertIsNone(events.agent(None))


class Render(unittest.TestCase):
    def test_allow_prints_nothing_and_a_refusal_prints_hook_json(self):
        self.assertEqual(ADAPTER.render('PreToolUse', Decision('allow')), ('', 0))
        for verdict in ('deny', 'ask'):
            text, code = ADAPTER.render('PreToolUse', Decision(verdict, 'r', 'why Instead: x [t]', 'x'))
            self.assertEqual(code, 0)
            self.assertTrue(text.endswith('\n'))
            self.assertEqual(json.loads(text), {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': verdict,
                'permissionDecisionReason': 'why Instead: x [t]'}})

    def test_the_profile_is_hosts_jsons(self):
        self.assertEqual(ADAPTER.profile, registry.host('claude').profile)
        self.assertTrue(ADAPTER.capabilities.supports_ask)
        self.assertEqual(rules.host_mcp_prefix(ADAPTER.profile), 'mcp__plugin_pm-kit_machine-lock__')
        self.assertEqual(rules.host_mcp_separator(ADAPTER.profile), '__')

    def test_the_settings_entry_matches_the_guarded_tools(self):
        entry = ADAPTER.settings_entry('C:/k/hosts/claude/shell/hooks/guard.py', executable='py')
        self.assertEqual(entry, {'PreToolUse': [{'matcher': MATCHER, 'hooks': [
            {'type': 'command', 'command': 'py', 'args': ['C:/k/hosts/claude/shell/hooks/guard.py']}]}]})
        for tool in list(events.FILE_TOOLS) + list(events.SHELL_TOOLS) + ['mcp__a__b']:
            self.assertTrue(re.fullmatch(MATCHER, tool), tool)


class Paths(unittest.TestCase):
    def test_norm_is_todays_normpath_and_normcase(self):
        for p in ('a/b/../c', 'A//B', './x', 'C:\\Users\\X\\..\\y'):
            self.assertEqual(paths.norm(p), os.path.normcase(os.path.normpath(p)))
            self.assertEqual(paths.norm(p, fold=False), os.path.normpath(p))
            self.assertEqual(paths.norm(p, absolute=True), os.path.normcase(os.path.abspath(p)))
            self.assertEqual(paths.norm(p, real=True), os.path.normcase(os.path.realpath(p)))

    def test_the_guard_folds_paths_only_through_norm(self):
        # docs/guard.md "Hosts": the later OS work changes one function. (place.py's realpath
        # resolves links to see whether there are any; it folds nothing, and keeps its own error.)
        found = []
        for name in sorted(os.listdir(os.path.join(KIT, 'kitlib', 'guard'))):
            if name.endswith('.py') and name != 'paths.py':
                with open(os.path.join(KIT, 'kitlib', 'guard', name), encoding='utf-8') as f:
                    for n, line in enumerate(f, 1):
                        code = line.split('#', 1)[0]
                        if re.search(r'os\.path\.(normcase|normpath)\(', code):
                            found.append('%s:%d' % (name, n))
        self.assertEqual(found, [])


class Layer(unittest.TestCase):
    def test_no_claude_payload_field_is_read_in_kitlib_guard(self):
        found = []
        for name in sorted(os.listdir(os.path.join(KIT, 'kitlib', 'guard'))):
            if name.endswith('.py'):
                with open(os.path.join(KIT, 'kitlib', 'guard', name), encoding='utf-8') as f:
                    text = f.read()
                for field in ('agent_id', 'agent_type', 'tool_input', 'tool_name', 'hookSpecificOutput',
                              'permissionDecision', "'Bash'", "'PowerShell'", "'Edit'", "'mcp__'"):
                    if field in text:
                        found.append('%s: %s' % (name, field))
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
