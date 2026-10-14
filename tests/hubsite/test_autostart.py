"""kitlib.hubsite.autostart and `hub.py setup --autostart [--whatif]`: the scheduled task that
starts the hub server at sign-in. No test registers a task: the PowerShell runner is injected, and
the CLI tests replace install."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib.hubsite import autostart, serve
from kitlib.proc import spawn
from tests.hubsite.test_hub_cli import HUB, CliCase

PY = 'C:\\Py\\python.exe'
HUB_PY = 'C:\\kit\\tools\\hub.py'
WORKTREE_HUB = ('/kit/.claude/worktrees/x/tools/hub.py',
                'D:\\kit\\.Claude\\Worktrees\\x\\tools\\hub.py')


def present(path):
    return True


def absent(path):
    return False


class Recorder(object):
    def __init__(self, code=0, stderr=''):
        self.calls, self.code, self.stderr = [], code, stderr

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=self.code, stdout='', stderr=self.stderr)


class TaskScript(unittest.TestCase):
    def test_the_task_runs_serve_through_the_carrier_with_pythonw_at_logon_not_elevated(self):
        text = autostart.task_script(PY, HUB_PY)
        self.assertIn("-Execute 'C:\\Py\\pythonw.exe'", text)
        carrier = os.path.join('C:\\kit\\tools', spawn.CARRIER)
        self.assertIn("-Argument '\"%s\" \"C:\\Py\\python.exe\" \"%s\" serve'" % (carrier, HUB_PY),
                      text)
        for part in ('-AtLogOn', '-LogonType Interactive', '-RunLevel Limited',
                     "-TaskName 'PM kit hub server'", '-Force', '-StartWhenAvailable'):
            self.assertIn(part, text)
        self.assertNotIn('Highest', text)
        self.assertNotIn('Unregister', text)

    def test_a_pythonw_given_still_runs_the_console_python_through_the_carrier(self):
        text = autostart.task_script('C:\\Py\\pythonw.exe', HUB_PY)
        self.assertIn("-Execute 'C:\\Py\\pythonw.exe'", text)
        self.assertIn('"C:\\Py\\python.exe" "%s" serve' % HUB_PY, text)

    def test_the_task_name_names_no_host(self):
        for word in ('claude', 'codex', 'anthropic', 'openai'):
            self.assertNotIn(word, autostart.TASK_NAME.lower())


class Install(unittest.TestCase):
    def test_whatif_prints_the_script_and_runs_nothing(self):
        run = Recorder()
        lines = autostart.install(HUB_PY, python=PY, whatif=True, run=run, isfile=present)
        self.assertEqual(lines[0], 'Would run in PowerShell:')
        self.assertEqual(lines[1], '  ' + autostart.task_script(PY, HUB_PY))
        self.assertEqual(len(lines), 2)
        self.assertEqual(run.calls, [])

    def test_whatif_from_a_worktree_or_without_pythonw_says_why_a_real_install_would_refuse(self):
        run = Recorder()
        lines = autostart.install(WORKTREE_HUB[1], python=PY, whatif=True, run=run, isfile=absent)
        self.assertEqual(lines[0], 'Would run in PowerShell:')
        said = lines[2:]
        self.assertEqual(len(said), 2, lines)
        self.assertTrue(all(l.startswith('setup --autostart would refuse: ') for l in said))
        self.assertIn('not a worktree', said[0])
        self.assertIn('no pythonw.exe beside %s' % PY, said[1])
        self.assertEqual(run.calls, [])

    def test_install_runs_the_script_in_powershell_once(self):
        run = Recorder()
        lines = autostart.install(HUB_PY, python=PY, run=run, isfile=present)
        self.assertEqual(len(run.calls), 1)
        argv, kw = run.calls[0]
        self.assertEqual(argv, ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                                autostart.task_script(PY, HUB_PY)])
        self.assertEqual(kw.get('timeout'), 120)
        self.assertIn(autostart.TASK_NAME, lines[0])
        self.assertIn('Unregister-ScheduledTask', lines[0])

    def test_install_from_a_worktree_is_refused_and_runs_nothing(self):
        for path in WORKTREE_HUB:
            run = Recorder()
            with self.assertRaises(autostart.AutostartError) as cm:
                autostart.install(path, python=PY, run=run, isfile=present)
            self.assertIn('not a worktree', str(cm.exception))
            self.assertIn('nothing registered', str(cm.exception))
            self.assertEqual(run.calls, [], path)

    def test_install_without_pythonw_is_refused_and_runs_nothing(self):
        run = Recorder()
        with self.assertRaises(autostart.AutostartError) as cm:
            autostart.install(HUB_PY, python=PY, run=run, isfile=absent)
        self.assertIn('C:\\Py\\pythonw.exe', str(cm.exception))
        self.assertEqual(run.calls, [])

    def test_a_failed_registration_is_an_autostart_error_with_powershell_s_reason(self):
        run = Recorder(code=1, stderr='Access is denied.')
        with self.assertRaises(autostart.AutostartError) as cm:
            autostart.install(HUB_PY, python=PY, run=run, isfile=present)
        self.assertIn('powershell exit 1', str(cm.exception))
        self.assertIn('Access is denied.', str(cm.exception))

    def test_the_error_is_a_serve_error_so_hub_py_prints_it_as_refused(self):
        self.assertTrue(issubclass(autostart.AutostartError, serve.ServeError))


class SetupAutostartCli(CliCase):
    """hub.py setup with --autostart: install is replaced, so nothing is registered."""

    def setUp(self):
        super().setUp()
        self.config({'hub': {'store': self.store_dir}})     # setup --autostart needs a store

    def patched(self, refusals=()):
        calls = []

        def install(hub_py, whatif=False, **kw):
            calls.append((hub_py, whatif))
            return ['Would run in PowerShell:', '  script'] if whatif else ['Installed it']
        for name, value in (('install', install), ('refusals', lambda hub_py: list(refusals))):
            p = mock.patch.object(HUB.hub_autostart, name, value)
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_whatif_prints_the_task_and_writes_no_token_file(self):
        calls = self.patched()
        code, out, err = self.run_cli('setup', '--autostart', '--whatif')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out, 'Would run in PowerShell:\n  script\n')
        self.assertEqual(calls, [(os.path.abspath(HUB.__file__), True)])
        self.assertFalse(os.path.exists(self.tokens))

    def test_the_real_whatif_from_this_checkout_prints_the_task(self):
        # install(run=spawn.run) binds spawn.run at import, so patching autostart.spawn.run would
        # not reach it; spawn.run looks subprocess.run up at call time, so patch that.
        with mock.patch('subprocess.run') as run:
            code, out, err = self.run_cli('setup', '--autostart', '--whatif')
        self.assertEqual((code, err), (0, ''))
        self.assertIn("-TaskName 'PM kit hub server'", out)
        self.assertIn('%s" serve' % os.path.abspath(HUB.__file__), out)
        run.assert_not_called()
        self.assertFalse(os.path.exists(self.tokens))

    def test_no_store_folder_refuses_autostart_and_whatif_before_registering_anything(self):
        self.config({})
        calls = self.patched()
        for argv in (('setup', '--autostart'), ('setup', '--autostart', '--whatif')):
            with mock.patch('subprocess.run') as run:
                code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (1, ''), argv)
            self.assertIn('REFUSED: no store folder is set', err)
            self.assertIn('hub.store', err)
            run.assert_not_called()
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.tokens))

    def test_autostart_makes_the_tokens_then_installs(self):
        calls = self.patched()
        code, out, err = self.run_cli('setup', '--autostart')
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(os.path.isdir(self.tokens))
        self.assertTrue(out.endswith('Installed it\n'), out)
        self.assertEqual(calls, [(os.path.abspath(HUB.__file__), False)])

    def test_a_refusal_is_exit_1_before_any_token_file(self):
        calls = self.patched(refusals=['install it from the main kit checkout, not a worktree'])
        code, out, err = self.run_cli('setup', '--autostart')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('REFUSED: install it from the main kit checkout', err)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.tokens))

    def test_plain_setup_installs_nothing_and_whatif_alone_is_a_usage_error(self):
        calls = self.patched()
        self.assertEqual(self.run_cli('setup')[0], 0)
        self.assertEqual(calls, [])
        code, out, err = self.run_cli('setup', '--whatif')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('USAGE: --whatif needs --autostart', err)


if __name__ == '__main__':
    unittest.main()
