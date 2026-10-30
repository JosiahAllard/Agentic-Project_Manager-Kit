"""The kit header (kitlib.host.header), its Claude rendering (ADAPTER.context) and the hook entry that
runs it on SessionStart and SubagentStart (boot/hook.py claude <event>)."""
import json
import os
import subprocess
import sys
import unittest

from hosts.claude.adapter.host import ADAPTER
from kitlib.host import header

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRY = os.path.join(ROOT, 'boot', 'hook.py')


class HeaderText(unittest.TestCase):
    def test_it_names_the_folder_and_what_kit_and_the_placeholders_mean(self):
        text = header.text('C:\\kits\\pm-kit\\')
        self.assertIn('The kit folder is C:/kits/pm-kit.', text)
        self.assertIn('`<kit>` means that folder', text)
        self.assertIn('`python "C:/kits/pm-kit/tools/machine_lock.py" status`', text)
        for placeholder in ('`<worktree>`', '`<repo>`', '`<slug>`'):
            self.assertIn(placeholder, text)
        self.assertIn('placeholder', text)

    def test_it_says_the_tools_run_from_the_kit_with_C_never_from_a_repo_copy(self):
        text = header.text('C:/k')
        self.assertIn('`python <kit>/tools/preflight.py -C <worktree>`', text)
        self.assertIn('never from a copy in the repository', text)
        self.assertNotIn('`python tools/', text)

    def test_it_names_no_host(self):
        self.assertNotRegex(header.text('/k'), r'(?i)claude|codex|plugin_root')

    def test_it_warns_naming_the_key_while_the_machine_config_sets_no_dev_drive(self):
        # docs/guard.md "Write jail", "Outer bound": dev_drive unset leaves the bound off, said at every start
        # relative, or not an existing folder (the Dev Drive unmounted, a wrong letter): the bound is off too
        missing = os.path.join(ROOT, 'no-such-dev-drive-folder')
        for user in ({'dev_drive': None}, {'dev_drive': '  '}, {}, None, {'dev_drive': 'dev'}, {'dev_drive': missing}):
            with self.subTest(user=user):
                text = header.text('C:/k', user=user)
                self.assertIn('\nWarning: the machine config sets no usable dev_drive', text)
                self.assertIn('docs/kit-json.md "Machine config keys"', text)
        text = header.text('C:/k', user={'dev_drive': ROOT})
        self.assertNotIn('dev_drive', text)
        self.assertTrue(text.endswith('never a folder to look for.'), text)

    def test_the_warning_and_the_guard_agree_on_whether_the_bound_is_on(self):
        # the header asks the guard's own question (kitlib.guard.jail.dev_root_of), so it warns
        # exactly when the bound has no Dev Drive
        from kitlib.guard import jail
        for value in (None, '', 'dev', os.path.join(ROOT, 'no-such-dev-drive-folder'), ROOT):
            with self.subTest(value=value):
                user = {'dev_drive': value}
                self.assertEqual(header.dev_drive_note(user) is None, jail.dev_root_of(user) is not None)


class ClaudeContext(unittest.TestCase):
    def test_session_and_subagent_start_carry_additional_context(self):
        for event in ('SessionStart', 'SubagentStart'):
            text, code = ADAPTER.context(event, 'hello')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text), {'hookSpecificOutput': {'hookEventName': event,
                                                                       'additionalContext': 'hello'}})
            self.assertTrue(text.endswith('\n'))
        self.assertEqual(ADAPTER.context('SessionStart', None), ('', 0))
        with self.assertRaises(ValueError):
            ADAPTER.context('PreToolUse', 'x')

    def test_a_tool_event_carries_context_beside_the_rendered_answer(self):
        from kitlib.host import Decision
        text = ADAPTER.tool_context('PreToolUse', 'hello', '')
        self.assertEqual(json.loads(text), {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                                   'additionalContext': 'hello'}})
        self.assertTrue(text.endswith('\n'))
        self.assertEqual(ADAPTER.tool_context('PreToolUse', '', ''), '')
        with self.assertRaises(ValueError):
            ADAPTER.tool_context('SessionStart', 'x', '')
        for verdict in ('deny', 'ask'):         # the decision and the reason stay as render wrote them
            d = Decision(verdict, 'r', 'why Instead: x [kit guard: rule=r role=main]')
            rendered = ADAPTER.render('PreToolUse', d)[0]
            got = json.loads(ADAPTER.tool_context('PreToolUse', 'hello', rendered))['hookSpecificOutput']
            want = json.loads(rendered)['hookSpecificOutput']
            self.assertEqual(got.pop('additionalContext'), 'hello')
            self.assertEqual(got, want)
        warned = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "warn"}}\n'
        self.assertEqual(json.loads(ADAPTER.tool_context('PreToolUse', 'hello', warned)),
                         {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                 'additionalContext': 'warn\nhello'}})


class Entry(unittest.TestCase):
    def run_entry(self, *args, payload=None, **env):
        # No PM_KIT_ROLE either: a SubagentStart with no agent_id reads the role from the mark, and
        # this case wants the plain hook whatever session runs the tests.
        environ = {k: v for k, v in os.environ.items()
                   if k not in ('PM_KIT_ROOT', 'PM_KIT_RELEASE_ID', 'PM_KIT_ROLE')}
        environ.update(env)
        stdin = {'input': json.dumps(payload)} if payload is not None else {'stdin': subprocess.DEVNULL}
        return subprocess.run([sys.executable, ENTRY, 'claude'] + list(args), capture_output=True, text=True,
                              encoding='utf-8', env=environ, timeout=60, **stdin)

    def test_a_dispatched_kit_agent_gets_the_header_then_the_agent_start_block(self):
        # The whole path, nothing mocked: boot/hook.py -> hookmain -> the Claude adapter's start_text
        # -> kitlib.agentstart, on a worktree of a repository built on disk.
        from tests import gitfixture
        repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(repo.cleanup)
        repo.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        head = repo.git('rev-parse', '--short', 'HEAD')
        wt = repo.worktree('y', 'x/y')
        r = self.run_entry('SubagentStart', payload={
            'hook_event_name': 'SubagentStart', 'cwd': wt, 'session_id': 's',
            'agent_id': 'a1', 'agent_type': 'pm-kit:worker'})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'SubagentStart')
        text = out['additionalContext']
        self.assertTrue(text.startswith(header.text(ROOT) + '\nAgent start (worker): '), text)
        self.assertIn('\n- checkout  worktree ', text)
        self.assertIn('| branch x/y @ %s' % head, text)
        self.assertNotIn('could not check', text)

    def test_each_event_gets_the_header_for_this_kit(self):
        for event in ('SessionStart', 'SubagentStart'):
            r = self.run_entry(event)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)['hookSpecificOutput']
            self.assertEqual(out['hookEventName'], event)
            self.assertEqual(out['additionalContext'], header.text(ROOT))

    def test_a_kit_that_will_not_boot_is_said_in_plain_text(self):
        r = self.run_entry('SessionStart', PM_KIT_ROOT='relative')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('PM kit: the kit did not load: BootError:'), r.stdout)


if __name__ == '__main__':
    unittest.main()
