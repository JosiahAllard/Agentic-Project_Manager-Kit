"""tools/delegated.py: its arguments and exit codes (docs/delegations.md "Running"). The runner
itself is tests/delegations/test_run.py; here it is stood in for, and the home is a temp folder."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.delegations import run as drun
from tests.home import pm_repo
from tools import delegated

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'delegated.py')

OP = {'id': 'D-001', 'what': 'say a name', 'repo': 'kit', 'shell': 'PowerShell',
      'command': 'tool {name}', 'args': [{'name': 'name', 'pattern': '[a-z]+'}],
      'preconditions': [], 'reversible': True, 'undo': 'nothing to undo',
      'trigger': ['owner-session', 'hub-answer'], 'elevated': False}


class Cli(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-delegated-cli-')
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.object(kuser, 'home_dir', return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.pm = pm_repo(self)

    def write_list(self, ops=(OP,)):
        folder = os.path.join(self.pm, kuser.DIR_NAME)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'owner-delegations.json'), 'w', encoding='utf-8') as fh:
            json.dump({'schema': 1, 'operations': list(ops)}, fh)

    def main(self, *argv, result=None, refuse=None):
        out, err = io.StringIO(), io.StringIO()
        calls = []

        def fake(op, pairs, trigger, cfg, hub=None):
            calls.append((op.id, pairs, trigger, hub))
            if refuse:
                raise drun.RunRefused(refuse)
            return result
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(drun, 'run', side_effect=fake), \
                mock.patch.object(kuser, 'user_config', return_value={}), \
                mock.patch.object(delegated, 'hub_client', return_value='HUB'):
            try:
                code = delegated.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), calls

    def test_an_arg_without_a_value_is_a_usage_error(self):
        code, _, err, calls = self.main('run', 'D-001', '--arg', 'alpha', '--trigger', 'H-kit-0001')
        self.assertEqual(code, 2)
        self.assertIn('--arg takes NAME=VALUE', err)
        self.assertEqual(calls, [])

    def test_no_trigger_is_a_usage_error(self):
        self.assertEqual(self.main('run', 'D-001', '--arg', 'name=alpha')[0], 2)

    def test_an_abbreviated_flag_is_a_usage_error(self):
        self.write_list()
        code, _, _, calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trig', 'H-kit-0001')
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_no_delegation_list_is_refused(self):
        code, _, err, calls = self.main('run', 'D-001', '--trigger', 'H-kit-0001')
        self.assertEqual(code, 1)
        self.assertIn('nothing is delegated', err)
        self.assertEqual(calls, [])

    def test_an_operation_not_listed_is_refused(self):
        self.write_list()
        code, _, err, calls = self.main('run', 'D-007', '--trigger', 'H-kit-0001')
        self.assertEqual(code, 1)
        self.assertIn('D-007 is not in the delegation list', err)
        self.assertEqual(calls, [])

    def test_a_refused_run_exits_1(self):
        self.write_list()
        code, out, err, _ = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'H-kit-0001',
                                      refuse='H-kit-0001 is retired')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('refused: H-kit-0001 is retired', err)

    def test_a_ruling_trigger_gets_no_hub(self):
        self.write_list()
        res = drun.Result('H-kit-0002', ['C:\\bin\\tool.exe', 'alpha'], 0, None)
        code, out, _, calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'H-kit-0001',
                                        result=res)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('D-001', [('name', 'alpha')], 'H-kit-0001', None)])
        self.assertIn('logged H-kit-0002; ran: C:\\bin\\tool.exe alpha', out)
        self.assertIn('done: exit code 0', out)

    def test_a_question_trigger_gets_the_hub(self):
        self.write_list()
        res = drun.Result('H-kit-0002', ['tool.exe'], 0, None)
        calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'Q-1001', result=res)[3]
        self.assertEqual(calls[0][3], 'HUB')

    def test_a_failed_command_exits_4(self):
        self.write_list()
        res = drun.Result('H-kit-0002', ['tool.exe'], 3, None)
        code, _, err, _ = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'H-kit-0001',
                                    result=res)
        self.assertEqual(code, delegated.FAILED)
        self.assertIn('failed: exit code 3', err)

    def test_a_command_that_cannot_start_exits_4(self):
        self.write_list()
        res = drun.Result('H-kit-0002', ['tool.exe'], None, 'could not start tool.exe: no')
        self.assertEqual(self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'H-kit-0001',
                                   result=res)[0], delegated.FAILED)


class PythonVars(unittest.TestCase):
    """Run by path, the tool refuses a PYTHON* variable before it imports argparse: a PYTHONPATH
    folder holding argparse.py would run first. Under -I the interpreter ignores them."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-delegated-pyvars-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.marker = os.path.join(self.dir, 'shadow-ran')
        with open(os.path.join(self.dir, 'argparse.py'), 'w', encoding='utf-8') as fh:
            fh.write('open(%r, "w").close()\nraise SystemExit(7)\n' % self.marker)

    def tool(self, *flags):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('PYTHON')}
        env['PYTHONPATH'] = self.dir
        return subprocess.run([sys.executable] + list(flags) + [TOOL], env=env, cwd=self.dir,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def test_a_python_variable_is_refused_before_argparse_is_imported(self):
        done = self.tool()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn('refused: PYTHONPATH set', done.stderr)
        self.assertIn('python -I', done.stderr)
        self.assertFalse(os.path.exists(self.marker), 'the shadow argparse ran')

    def test_under_isolation_the_variables_are_ignored(self):
        done = self.tool('-I')
        self.assertEqual(done.returncode, 2, done.stderr)  # no subcommand: argparse's usage error
        self.assertFalse(os.path.exists(self.marker), 'the shadow argparse ran')

    def test_python_vars_names_them_unless_the_interpreter_ignores_them(self):
        env = {'PYTHONPATH': 'x', 'PythonHome': 'y', 'PATH': 'z'}
        self.assertEqual(delegated.python_vars(env, mock.Mock(ignore_environment=0)),
                         ['PYTHONPATH', 'PythonHome'])
        self.assertEqual(delegated.python_vars(env, mock.Mock(ignore_environment=1)), [])


if __name__ == '__main__':
    unittest.main()
