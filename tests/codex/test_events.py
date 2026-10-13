"""hosts.codex.adapter.events: Codex's hook payloads parsed into kitlib.host.Action (ported from the
Codex branch's tests/codex/test_actions.py)."""
import os
import unittest

from hosts.codex.adapter.events import CodexAction, parse, patch_targets
from kitlib.host import Action

CWD = 'C:/r/.codex/worktrees/w'


def event(tool, data, **extra):
    ev = dict(hook_event_name='PreToolUse', tool_name=tool, tool_input=data, cwd=CWD)
    ev.update(extra)
    return ev


def bash(command, shell='powershell.exe', **data):
    return event('Bash', dict(data, command=command, shell=shell))


class Patch(unittest.TestCase):
    def test_every_target_including_both_rename_ends(self):
        patch = ('*** Begin Patch\n*** Add File: a.py\n+new\n*** Delete File: b.py\n'
                 '*** Update File: c.py\n*** Move to: d.py\n@@\n-old\n+new\n*** End Patch')
        self.assertEqual(patch_targets(patch), (('Add', 'a.py'), ('Delete', 'b.py'), ('Update', 'c.py'), ('Move', 'd.py')))
        got = parse('PreToolUse', event('apply_patch', {'command': patch}))
        self.assertIsInstance(got, tuple)
        self.assertEqual([a.path for a in got], ['a.py', 'b.py', 'c.py', 'd.py'])
        self.assertEqual({a.kind for a in got}, {'edit'})
        self.assertEqual(got[3].retarget, 'apply_patch "*** Move to: {path}"')

    def test_added_patch_like_text_is_content(self):
        patch = '*** Begin Patch\n*** Add File: a.txt\n+*** Delete File: b.txt\n*** End Patch'
        self.assertEqual(patch_targets(patch), (('Add', 'a.txt'),))

    def test_bad_patch_shapes_are_not_partially_allowed(self):
        for patch in ('', '*** Begin Patch\n*** End Patch', '*** Begin Patch\n*** Add File: \n*** End Patch',
                      '*** Begin Patch\n*** Delete File: a\n+bad\n*** End Patch',
                      '*** Begin Patch\n*** Add File: a\n+ok\n*** Mystery: b\n*** End Patch',
                      '*** Begin Patch\n*** Update File: a\n@@\n+x\n*** Move to: b\n*** End Patch',
                      '*** Begin Patch\n*** Update File: a\n*** Move to: b\n*** Move to: c\n*** End Patch',
                      '*** Begin Patch\n*** Add File: a\x00\n+ok\n*** End Patch', None):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                parse('PreToolUse', event('apply_patch', {'command': patch}))

    def test_raw_patch_input_is_supported(self):
        got = parse('PreToolUse', event('apply_patch', '*** Begin Patch\n*** Delete File: a\n*** End Patch'))
        self.assertEqual([a.path for a in got], ['a'])


class Shell(unittest.TestCase):
    def test_the_dialect_comes_from_the_named_interpreter(self):
        self.assertEqual(parse('PreToolUse', bash('pwd', 'bash.exe')).shell, 'bash')
        self.assertEqual(parse('PreToolUse', bash('pwd', 'C:\\Program Files\\Git\\bin\\sh.exe')).shell, 'bash')
        self.assertEqual(parse('PreToolUse', bash('Get-Location', 'pwsh')).shell, 'powershell')
        self.assertEqual(parse('PreToolUse', event('PowerShell', {'command': 'Get-Location'})).shell, 'powershell')

    def test_a_missing_or_unknown_interpreter_carries_a_refusal(self):
        for ev, shell in ((event('Bash', {'command': 'pwd'}), 'powershell'), (bash('dir', 'cmd.exe'), 'powershell'),
                          (bash('x', 3), None), (event('shell_command', {'command': 'Get-Location'}), 'powershell')):
            with self.subTest(ev=ev):
                a = parse('PreToolUse', ev)
                self.assertIsInstance(a, CodexAction)
                self.assertEqual((a.kind, a.shell), ('shell', shell))
                self.assertTrue(a.refusal)
        self.assertNotIsInstance(parse('PreToolUse', bash('pwd', 'bash.exe')), CodexAction)

    def test_the_command_and_a_workdir(self):
        a = parse('PreToolUse', event('exec_command', {'cmd': 'Get-Location', 'shell': 'pwsh.exe', 'workdir': 'sub'}))
        self.assertEqual(a, Action('shell', tool='exec_command', command='Get-Location', cwd=CWD, shell='powershell',
                                   workdir=os.path.normpath(os.path.join(CWD, 'sub'))))
        b = parse('PreToolUse', bash('Get-Location', workdir='D:/elsewhere'))
        self.assertEqual(b.workdir, 'D:/elsewhere')
        self.assertIsNone(parse('PreToolUse', bash('Get-Location')).workdir)

    def test_interactive_input_carries_a_refusal(self):
        for data in ({'cmd': 'powershell', 'tty': True}, {'cmd': 'pwd', 'interactive': True},
                     {'cmd': 'pwd', 'stdin': 'git push'}):
            with self.subTest(data=data):
                a = parse('PreToolUse', event('exec_command', dict(data, shell='pwsh.exe')))
                self.assertEqual((a.kind, a.shell, a.command), ('shell', 'powershell', data['cmd']))
                self.assertTrue(a.refusal)
        for data in ({'cmd': ''}, {'cmd': 3}, {'cmd': [3]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse('PreToolUse', event('exec_command', dict(data, shell='pwsh.exe')))
        sent = parse('PreToolUse', event('write_stdin', {'session_id': 1, 'chars': 'git push\n'}))
        self.assertEqual((sent.kind, bool(sent.refusal)), ('other', True))
        with self.assertRaises(ValueError):
            parse('PreToolUse', event('write_stdin', {'session_id': 1, 'extra': 1}))
        self.assertEqual(parse('PreToolUse', event('write_stdin', {'session_id': 1, 'chars': ''})).kind, 'other')


class Kinds(unittest.TestCase):
    def test_file_tools_mcp_and_others(self):
        w = parse('PreToolUse', event('Write', {'file_path': 'a.py', 'content': ''}))
        self.assertEqual((w.kind, w.path, w.retarget), ('edit', 'a.py', 'Write(file_path="{path}")'))
        self.assertEqual(parse('PreToolUse', event('mcp__machine-lock__lock_free', {})).kind, 'mcp')
        for tool in ('Read', 'update_plan', 'spawn_agent', 'unknown_writer'):
            self.assertEqual(parse('PreToolUse', event(tool, {})), Action('other', tool=tool, cwd=CWD), tool)

    def test_starts(self):
        self.assertEqual(parse('SessionStart', {'hook_event_name': 'SessionStart', 'cwd': CWD}),
                         Action('session_start', cwd=CWD))
        self.assertEqual(parse('PostCompact', {'cwd': CWD}).kind, 'session_start')
        self.assertEqual(parse('SubagentStart', {'cwd': CWD}).kind, 'subagent_start')

    def test_a_payload_carries_no_identity_whatever_it_claims(self):
        a = parse('PreToolUse', bash('Get-Location', agent_type='lander', agent_id='child', role='owner'))
        self.assertEqual((a.agent, a.agent_role, a.role), (None, None, None))

    def test_bad_events_are_refused(self):
        bad = [None, {}, event('Write', {}), event('PowerShell', {'command': 3}), event('', {}),
               dict(bash('pwd'), cwd='relative'), dict(bash('pwd'), hook_event_name='PostToolUse')]
        for ev in bad:
            with self.subTest(ev=ev), self.assertRaises(ValueError):
                parse('PreToolUse', ev)
        with self.assertRaises(ValueError):
            parse('Stop', {'cwd': CWD})


if __name__ == '__main__':
    unittest.main()
