"""The window script's order and the call that opens it (kitlib/launcher/window.py)."""
import re
import unittest

from kitlib.launcher import window


class ClaudeCommand(unittest.TestCase):
    DEFAULT = ("claude --agent a --model 'm' --effort high --autocompact 5 --add-dir 'd' "
               "--permission-mode auto --remote-control 'n' -n 'n' 'it''s'")

    def test_the_default_command_line_is_unchanged(self):
        self.assertEqual(window.claude_command('n', "it's", 'a', 'm', 'high', 5, add_dir='d'), self.DEFAULT)
        self.assertEqual(window.claude_command('n', 'p', remote_control=True),
                         "claude --permission-mode auto --remote-control 'n' -n 'n' 'p'")

    def test_none_keeps_every_session_kind_byte_identical(self):
        c = window.claude_command
        self.assertEqual(c('n', 'p', 'a', permission_mode=None),
                         "claude --agent a --permission-mode auto --remote-control 'n' -n 'n' 'p'")
        self.assertEqual(c('n', 'p', resume='sid', permission_mode=None),
                         "claude --resume 'sid' --remote-control 'n' -n 'n' 'p'")
        self.assertEqual(c('n', 'p', 'a', resume='sid', fork=True),
                         "claude --resume 'sid' --fork-session --agent a --remote-control 'n' -n 'n' 'p'")

    def test_a_given_mode_replaces_the_default(self):
        got = window.claude_command('n', "it's", 'a', 'm', 'high', 5, add_dir='d', permission_mode='acceptEdits')
        self.assertEqual(got, self.DEFAULT.replace('--permission-mode auto', '--permission-mode acceptEdits'))
        self.assertEqual(got.count('--permission-mode'), 1)
        self.assertEqual(window.claude_command('n', 'p', resume='sid', permission_mode='default'),
                         "claude --resume 'sid' --permission-mode default --remote-control 'n' -n 'n' 'p'")

    def test_an_unknown_mode_raises(self):
        for bad in ('', 'yolo', 'AUTO', 'bypassPermissions', 'dontAsk'):
            with self.assertRaises(ValueError):
                window.claude_command('n', 'p', permission_mode=bad)

    def test_no_remote_control_drops_only_that_pair(self):
        got = window.claude_command('n', "it's", 'a', 'm', 'high', 5, add_dir='d', remote_control=False)
        self.assertEqual(got, self.DEFAULT.replace(" --remote-control 'n'", ''))
        self.assertIn(" -n 'n' ", got)


class ScriptOrder(unittest.TestCase):
    def setUp(self):
        self.text = window.script('alpha-coord-a2', 'coordinator', '/repos/alpha', 'claude --x',
                                  config_dir='/acct2', agent_temp='/agent-temp')

    def at(self, part):
        i = self.text.find(part)
        self.assertNotEqual(i, -1, part)
        return i

    def test_env_load_then_clear_then_persist_then_mark_then_account(self):
        order = [self.at("$Host.UI.RawUI.WindowTitle = 'alpha-coord-a2'"),
                 self.at(window.USER_ENV),
                 self.at(window.CLEAR_CLAUDE),
                 self.at(window.PERSIST),
                 self.at("$env:PM_KIT_ROLE = 'coordinator'"),
                 self.at("$env:PM_KIT_SESSION_NAME = 'alpha-coord-a2'"),
                 self.at("$env:CLAUDE_CONFIG_DIR = '/acct2'"),
                 self.at("$env:TEMP = '/agent-temp'"),
                 self.at("Set-Location '/repos/alpha'"),
                 self.at('claude --x')]
        self.assertEqual(order, sorted(order))
        self.assertTrue(self.text.endswith('claude --x'))

    def test_the_clear_covers_the_child_session_marker_and_spares_the_kit_marks(self):
        pattern = re.search(r"-match '([^']+)'", window.CLEAR_CLAUDE).group(1)
        for name in ('CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE',
                     'ANTHROPIC_BASE_URL', 'claude_code_child_session'):
            self.assertTrue(re.search(pattern, name, re.IGNORECASE), name)    # PowerShell -match ignores case
        for name in ('PM_KIT_ROLE', 'PM_KIT_SESSION_NAME', 'TEMP', 'MY_CLAUDE'):
            self.assertFalse(re.search(pattern, name, re.IGNORECASE), name)
        self.assertIn("'Process'", window.CLEAR_CLAUDE)
        self.assertEqual(window.PERSIST, "$env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = '1'")

    def test_the_user_env_load_skips_path_temp_and_tmp(self):
        self.assertIn("@('Path','TEMP','TMP') -notcontains $k", window.USER_ENV)
        self.assertIn("GetEnvironmentVariables('User')", window.USER_ENV)

    def test_the_default_folder_removes_config_dir_after_the_clear(self):
        text = window.script('n', 'plain', '/r', 'claude')
        self.assertNotIn('$env:CLAUDE_CONFIG_DIR =', text)
        self.assertLess(text.index(window.CLEAR_CLAUDE), text.index('Remove-Item Env:CLAUDE_CONFIG_DIR'))
        self.assertNotIn('$env:TEMP', text)


class FakeRun(object):
    def __init__(self, code=0, stderr=''):
        self.code, self.stderr, self.calls = code, stderr, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type('R', (), {'returncode': self.code, 'stderr': self.stderr, 'stdout': ''})()


class OpenWindow(unittest.TestCase):
    TEXT = "Set-Location '/r'; claude 'it''s'"

    def test_runner_script_runs_conhost_minimized_with_the_encoded_script(self):
        r = window.runner_script(self.TEXT)
        self.assertTrue(r.startswith('$null = (New-Object -ComObject WScript.Shell).Run('))
        self.assertTrue(r.endswith(", 7, $false)"))
        b64 = re.search(r'-EncodedCommand ([A-Za-z0-9+/=]+)', r).group(1)
        self.assertEqual(window.decode(b64), self.TEXT)
        self.assertIn("'conhost.exe powershell.exe -NoExit -EncodedCommand ", r)

    def test_open_window_passes_the_runner_to_powershell(self):
        run = FakeRun()
        window.open_window(self.TEXT, run=run)
        (argv, kw), = run.calls
        self.assertEqual(argv[:4], ['powershell', '-NoProfile', '-NonInteractive', '-Command'])
        self.assertEqual(argv[4], window.runner_script(self.TEXT))
        self.assertEqual((kw['timeout'], kw['capture_output'], kw['text']), (60, True, True))

    def test_a_failed_open_is_a_runtime_error(self):
        with self.assertRaisesRegex(RuntimeError, r'powershell exit 1\): boom'):
            window.open_window(self.TEXT, run=FakeRun(1, 'boom\n'))


if __name__ == '__main__':
    unittest.main()
