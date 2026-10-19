"""Codex hook payloads judged end to end: hosts.codex.adapter's parse and screen, then kitlib.guard with
the role policy on, as kitlib.host.hookmain runs them (the role policy every host shares is tests/guard/test_rolepolicy.py).

Codex's hooks carry no subagent identity, so a session is held by its PM_KIT_ROLE mark alone (the
role-only policy, hosts.json capabilities). The Codex branch held every marked call as a subagent;
what that changes is listed in hosts/codex/README.md, "Not verified"."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from hosts.codex.adapter import ADAPTER
from kitlib import kitroot
from kitlib.guard import decide
from kitlib.host import Decision, hookmain
from tests.guard import world as W
from tests.guard.test_rolepolicy import kit


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World(kit=kit({}))
        self.addCleanup(self.w.cleanup)

    def event(self, tool, data, cwd=None):
        if tool == 'Bash' and isinstance(data, dict):
            data = dict(data, shell=data.get('shell', 'powershell.exe'))
        return {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': data, 'cwd': cwd or self.w.wt}

    def patch(self, *paths):
        body = ''.join('*** Add File: %s\n+x\n' % p for p in paths)
        return self.event('apply_patch', {'command': '*** Begin Patch\n' + body + '*** End Patch'})

    def judge(self, payload, role='worker'):
        """The Decision hookmain reaches on `payload` in a session marked `role` (None: the owner's)."""
        env = {'PM_KIT_ROLE': role} if role else {}
        guard = SimpleNamespace(decide=lambda a, environ=None, profile=None:
                                decide(a, self.w.ctx(environ=environ, kit=kitroot.root())))
        actions = ADAPTER.parse('PreToolUse', payload)
        return hookmain._judge(ADAPTER, guard, actions if isinstance(actions, tuple) else (actions,), env, None)

    def denied(self, payload, rule, role='worker'):
        d = self.judge(payload, role)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, payload, role='worker'):
        d = self.judge(payload, role)
        self.assertEqual(d.verdict, 'allow', d.reason)


class MarkedSessions(Base):
    """Codex's hooks carry no subagent identity, so hookmain holds a marked session as an
    unidentified agent of its mark's role (kitlib.host.hookmain): the main-checkout rule and the
    write jail hold it as they hold a subagent elsewhere. The owner's unmarked session is unchanged."""
    ROLES = ('worker', 'senior-worker', 'reviewer', 'test-runner', 'lander', 'pm', 'coordinator')

    def test_root_session_patch_to_main_is_denied(self):
        self.denied(self.patch(os.path.join(self.w.main, 'worker.py')), 'main-checkout')

    def test_pm_workflow_does_not_gain_main_edit_privilege(self):
        self.denied(self.patch(os.path.join(self.w.main, 'a.py')), 'main-checkout', role='pm')

    def test_multi_file_patch_is_denied_if_any_target_is_in_main(self):
        self.denied(self.patch('worker.py', os.path.join(self.w.main, 'a.py')), 'main-checkout')

    def test_a_rename_into_main_is_denied(self):
        dest = os.path.join(self.w.main, 'stolen.py')
        patch = '*** Begin Patch\n*** Update File: worker.py\n*** Move to: %s\n@@\n-a\n+b\n*** End Patch' % dest
        self.denied(self.event('apply_patch', {'command': patch}), 'main-checkout')

    def test_every_marked_role_is_held_off_main_and_outside_its_checkout(self):
        for role in self.ROLES:
            with self.subTest(role=role):
                for path, rule in ((os.path.join(self.w.main, 'src.py'), 'main-checkout'),
                                   (os.path.join(self.w.main, 'AGENTS.md'), 'main-checkout'),
                                   (os.path.join(self.w.plain, 'x.py'), 'write-jail')):
                    d = self.denied(self.event('Write', {'file_path': path, 'content': 'x'}), rule, role=role)
                    self.assertIn('role=subagent', d.reason)

    def test_the_owners_unmarked_session_is_unchanged(self):
        for path in (os.path.join(self.w.main, 'src.py'), os.path.join(self.w.plain, 'x.py')):
            self.allowed(self.event('Write', {'file_path': path, 'content': 'x'}), role=None)


class NoEditModes(Base):
    """A marked read, test or lander session does not edit its own checkout, whatever the
    repository's role-policy switch (the source's guard_adapter held them so); its role file's
    `writes` (the consultant's notes file) stay open."""

    def setUp(self):
        self.w = W.World(kit=kit({}, on=None))
        self.addCleanup(self.w.cleanup)

    def test_test_read_and_lander_roles_do_not_edit_their_checkout(self):
        for role in ('test-runner', 'reviewer', 'lander', 'consultant'):
            with self.subTest(role=role):
                self.denied(self.event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': 'x'}),
                            'host-role-edit', role=role)
                self.denied(self.patch('b.py'), 'host-role-edit', role=role)

    def test_write_roles_the_writes_list_and_temp_stay_open(self):
        self.allowed(self.event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': 'x'}), role='worker')
        # the consultant's notes file is in agent temp (no role file names a `writes` path now)
        notes = os.path.join(self.w.temp, 'repo-consultant', 'notes.md')
        self.allowed(self.event('Write', {'file_path': notes, 'content': 'x'}), role='consultant')
        self.allowed(self.event('Write', {'file_path': os.path.join(self.w.temp, 'a.py'), 'content': 'x'}),
                     role='test-runner')


class PayloadShapes(Base):
    """A call the guard cannot fully judge (no named dialect, cmd.exe, a TTY, input fed to a running
    process) is refused by Codex's policy in a marked session, with the policy's reason; the owner's
    unmarked session is judged with the default dialect (PowerShell, Codex's on Windows) or allowed,
    never refused for its shape."""
    SHAPES = (('Bash', {'command': 'git status'}), ('exec_command', {'cmd': 'git status'}),
              ('exec_command', {'cmd': 'git status', 'shell': 'C:/Windows/System32/cmd.exe'}),
              ('exec_command', {'cmd': 'python', 'shell': 'bash', 'tty': True}),
              ('exec_command', {'cmd': 'cat', 'shell': 'bash', 'stdin': 'y'}),
              ('write_stdin', {'session_id': 1, 'chars': 'y\n'}),
              ('shell', {'command': ['bash', '-lc', 'ls']}))

    def raw(self, tool, data):
        return {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': data, 'cwd': self.w.wt}

    def test_the_owner_is_never_refused_for_a_shape(self):
        for tool, data in self.SHAPES:
            with self.subTest(tool=tool, data=data):
                self.allowed(self.raw(tool, data), role=None)

    def test_a_marked_session_is_refused_by_policy(self):
        for tool, data in self.SHAPES[:-1]:
            with self.subTest(tool=tool, data=data):
                d = self.denied(self.raw(tool, data), 'host-shell')
                self.assertNotIn('guard failed', d.reason)
        self.allowed(self.raw('shell', {'command': ['bash', '-lc', 'ls']}))

    def test_the_owner_is_still_judged_in_the_default_dialect(self):
        self.denied(self.raw('Bash', {'command': 'Get-ChildItem $env:USERPROFILE -Recurse'}), 'root-search', role=None)

    # cmd.exe text the guard must still see a reset --hard in. PowerShell would read the first three
    # as a comment after `#` or `<#`; the guard reads the text as cmd.exe does.
    CMD_RESETS = (
        'echo # & git reset --hard', 'echo #&git reset --hard', 'echo <# & git reset --hard & echo #>',
        'git reset --hard & echo x', 'echo x & git reset --hard', 'echo x&&git reset --hard', 'echo x|git reset --hard',
        'g^it reset --hard', 'git reset ^--hard', '"git" reset --hard', 'git reset "--hard"', 'rem x & git reset --hard',
        "echo ' & git reset --hard & echo '", 'echo $x & git reset --hard', 'echo { & git reset --hard & echo }',
        'echo `" & git reset --hard', 'echo --% & git reset --hard', 'echo "x" & git reset --hard',
        'echo ^" & git reset --hard', 'echo "a & git reset --hard', 'echo @( & git reset --hard',
        '%COMSPEC% /c git reset --hard', 'echo ; & git reset --hard',
        '(git reset --hard)', 'git reset --hard 2>nul', 'cmd /c git reset --hard', 'echo `& git reset --hard',
        'echo ( & git reset --hard', 'echo $(x) & git reset --hard', 'echo x , & git reset --hard',
        "echo ' & git reset --hard", "echo \u2019 & git reset --hard", "echo '' & git reset --hard",
    )
    # Text cmd.exe runs no reset from (`;` is no separator to it), and two the shared guard does not
    # model in cmd.exe's dialect on any host (a caret joining words, a command named by %VAR%):
    # allowed, as the default host's `cmd /c` allows them.
    CMD_PASSES = ('echo x; git reset --hard', 'git^ reset --hard', 'set G=git& call %G% reset --hard')

    def test_the_owners_cmd_exe_call_is_judged_as_cmd_c(self):
        for command, rule in (('git reset --hard', 'git-reset-hard'), ('git push --force origin main', 'git-push-main'),
                              ('git commit -am x', 'git-commit-all'), ('git clean -fdx', 'git-clean'),
                              ('git checkout -- .', 'git-checkout'),
                              ("echo '' & git push --force origin main", 'git-push-main')):
            with self.subTest(command=command):
                payload = self.event('exec_command', {'cmd': command, 'shell': 'C:/Windows/System32/cmd.exe'},
                                     cwd=self.w.main)
                self.denied(payload, rule, role=None)
        for command in self.CMD_RESETS:
            with self.subTest(command=command):
                self.denied(self.event('exec_command', {'cmd': command, 'shell': 'cmd.exe'}, cwd=self.w.main),
                            'git-reset-hard', role=None)
        for command in self.CMD_PASSES:
            with self.subTest(command=command):
                self.allowed(self.event('exec_command', {'cmd': command, 'shell': 'cmd.exe'}, cwd=self.w.main),
                             role=None)
        a = ADAPTER.parse('PreToolUse', self.raw('exec_command', {'cmd': "dir 'x'", 'shell': 'cmd.exe'}))
        self.assertEqual((a.shell, a.command), ('powershell', "cmd /c 'dir ''x'''"))

    def test_a_legacy_argv_and_an_input_patch_parse(self):
        a = ADAPTER.parse('PreToolUse', self.raw('shell', {'command': ['bash', '-lc', 'ls -la']}))
        self.assertEqual((a.command, a.shell), ('ls -la', 'bash'))
        got = ADAPTER.parse('PreToolUse', self.raw('apply_patch', {'input': '*** Begin Patch\n*** Add File: a\n+x\n*** End Patch'}))
        self.assertEqual([x.path for x in got], ['a'])


class Patches(Base):
    def test_a_patch_in_the_own_worktree_is_allowed_and_writes_nothing(self):
        self.allowed(self.patch('worker.py'))
        self.assertFalse(os.path.exists(os.path.join(self.w.wt, 'worker.py')))

    def test_a_patch_is_denied_when_any_target_is(self):
        self.allowed(self.patch('a.py'), role='coordinator')
        d = self.denied(self.patch('a.py', os.path.join(self.w.plain, 'other.py')), 'write-jail', role='coordinator')
        self.assertIn('other.py', d.reason)

    def test_a_rename_destination_is_judged(self):
        dest = os.path.join(self.w.plain, 'stolen.py')
        patch = '*** Begin Patch\n*** Update File: worker.py\n*** Move to: %s\n@@\n-a\n+b\n*** End Patch' % dest
        self.denied(self.event('apply_patch', {'command': patch}), 'write-jail', role='coordinator')

    def test_read_test_and_lander_roles_do_not_patch_their_checkout(self):
        for role in ('reviewer', 'test-runner', 'lander'):
            with self.subTest(role=role):
                self.denied(self.patch('worker.py'), 'host-role-edit', role=role)

    def test_host_configuration_is_read_only_to_a_reader(self):
        for path in ('AGENTS.md', '.codex/config.toml', '.codex/agents/worker.toml'):
            with self.subTest(path=path):
                self.assertEqual(self.judge(self.patch(path), role='reviewer').verdict, 'deny')

    def test_a_malformed_patch_fails_closed(self):
        with self.assertRaises(ValueError):
            ADAPTER.parse('PreToolUse', self.event('apply_patch', {'command': 'bad'}))


class Shell(Base):
    def test_shared_rules_judge_every_session(self):
        for role in ('worker', None):
            with self.subTest(role=role):
                self.denied(self.event('Bash', {'command': 'find $USERPROFILE -name x', 'shell': 'bash.exe'}),
                            'root-search', role=role)
                self.denied(self.event('Bash', {'command': 'godot --headless'}), 'heavy', role=role)

    def test_a_held_lock_allows_a_heavy_run(self):
        self.w.held = True
        self.allowed(self.event('Bash', {'command': 'godot --headless'}))

    def test_shell_patch_transport_is_refused_in_a_marked_session(self):
        cmd = "apply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: AGENTS.md\n*** End Patch\nEOF"
        self.denied(self.event('Bash', {'command': cmd, 'shell': 'bash.exe'}), 'host-shell-patch')
        self.denied(self.event('Bash', {'command': 'apply_patch.exe x'}), 'host-shell-patch')
        self.assertNotEqual(self.judge(self.event('Bash', {'command': cmd, 'shell': 'bash.exe'}), role=None).rule,
                            'host-shell-patch', 'the owner\'s own session is held by the shared rules only')

    def test_interactive_interpreters_are_refused(self):
        for command in ('powershell.exe', 'python.exe -i', 'python.exe -', 'bash.exe -l', 'pwsh.exe -NoLogo',
                        'powershell -noe -c 1', 'bash -c bash', 'cmd /c cmd'):
            with self.subTest(command=command):
                self.denied(self.event('Bash', {'command': command}), 'role-shell')
        self.allowed(self.event('Bash', {'command': "powershell -NoProfile -c 'Get-Location'"}))

    def test_a_workdir_outside_the_checkout_is_refused(self):
        self.denied(self.event('Bash', {'command': 'Get-Location', 'workdir': self.w.data}), 'role-workdir')
        self.denied(self.event('Bash', {'command': 'Get-Location', 'workdir': 'Q:\\outside'}), 'role-workdir')
        self.allowed(self.event('Bash', {'command': 'Get-Location', 'workdir': os.path.join(self.w.wt, 'sub')}))


class Tools(Base):
    def test_unknown_tools_are_refused_only_in_a_marked_session(self):
        for tool in ('spawn_agent', 'send_message', 'unknown_writer'):
            with self.subTest(tool=tool):
                self.denied(self.event(tool, {}), 'host-tool')
                self.allowed(self.event(tool, {}), role=None)
        for tool in ('Read', 'update_plan', 'request_user_input', 'view_image'):
            self.allowed(self.event(tool, {}))

    def test_mcp_is_the_locks_read_tools_in_a_marked_session(self):
        self.allowed(self.event('mcp__machine-lock__lock_snapshot', {}))
        self.denied(self.event('mcp__other__do_things', {}), 'host-tool')
        self.denied(self.event('mcp__github__lock_snapshot', {}), 'host-tool')   # another server's same name
        self.allowed(self.event('mcp__other__do_things', {}), role=None)

    def test_a_lock_owner_tool_is_refused_to_a_marked_session(self):
        d = self.judge(self.event('mcp__machine-lock__lock_hold_create', {'spec': {}}))
        self.assertEqual(d.verdict, 'deny', d.reason)


class Hook(Base):
    """The verdict as the hook writes it (hookmain.run with the Codex adapter's render)."""

    def run_hook(self, payload, decide_with):
        import io
        import json
        out = io.StringIO()
        with mock.patch('kitlib.guard.decide', decide_with):
            code = hookmain.run(ADAPTER, 'PreToolUse', io.StringIO(json.dumps(payload)), out, environ={},
                                entry='C:/kit/run.py')
        return code, json.loads(out.getvalue())['hookSpecificOutput'] if out.getvalue() else None

    def test_an_ask_is_written_as_a_deny(self):
        code, out = self.run_hook(self.patch('worker.py'), lambda *a, **k: Decision('ask', 'review', 'PM review needed'))
        self.assertEqual((code, out['permissionDecision'], out['permissionDecisionReason']), (0, 'deny', 'PM review needed'))

    def test_a_guard_failure_is_an_explicit_deny(self):
        def boom(*a, **k):
            raise RuntimeError('broken')
        code, out = self.run_hook(self.patch('worker.py'), boom)
        self.assertEqual((code, out['permissionDecision']), (0, 'deny'))
        self.assertIn('RuntimeError: broken', out['permissionDecisionReason'])


if __name__ == '__main__':
    unittest.main()
