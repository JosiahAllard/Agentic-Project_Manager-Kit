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

    def test_it_names_no_host(self):
        self.assertNotRegex(header.text('/k'), r'(?i)claude|codex|plugin_root')


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


class Entry(unittest.TestCase):
    def run_entry(self, *args, **env):
        environ = {k: v for k, v in os.environ.items() if k not in ('PM_KIT_ROOT', 'PM_KIT_RELEASE_ID')}
        environ.update(env)
        return subprocess.run([sys.executable, ENTRY, 'claude'] + list(args), capture_output=True, text=True,
                              encoding='utf-8', env=environ, timeout=60, stdin=subprocess.DEVNULL)

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
