"""tools/delegated.py: its arguments and exit codes (docs/delegations.md "Running"). The runner
itself is tests/delegations/test_run.py; here it is stood in for, and the home is a temp folder."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.delegations import run as drun
from tools import delegated

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

    def write_list(self, ops=(OP,)):
        folder = os.path.join(self.home, kuser.DIR_NAME)
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
        code, _, err, calls = self.main('run', 'D-001', '--arg', 'alpha', '--trigger', 'R-0001')
        self.assertEqual(code, 2)
        self.assertIn('--arg takes NAME=VALUE', err)
        self.assertEqual(calls, [])

    def test_no_trigger_is_a_usage_error(self):
        self.assertEqual(self.main('run', 'D-001', '--arg', 'name=alpha')[0], 2)

    def test_an_abbreviated_flag_is_a_usage_error(self):
        self.write_list()
        code, _, _, calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trig', 'R-0001')
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_no_delegation_list_is_refused(self):
        code, _, err, calls = self.main('run', 'D-001', '--trigger', 'R-0001')
        self.assertEqual(code, 1)
        self.assertIn('nothing is delegated', err)
        self.assertEqual(calls, [])

    def test_an_operation_not_listed_is_refused(self):
        self.write_list()
        code, _, err, calls = self.main('run', 'D-007', '--trigger', 'R-0001')
        self.assertEqual(code, 1)
        self.assertIn('D-007 is not in the delegation list', err)
        self.assertEqual(calls, [])

    def test_a_refused_run_exits_1(self):
        self.write_list()
        code, out, err, _ = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'R-0001',
                                      refuse='R-0001 is retired')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('refused: R-0001 is retired', err)

    def test_a_ruling_trigger_gets_no_hub(self):
        self.write_list()
        res = drun.Result('R-0002', ['C:\\bin\\tool.exe', 'alpha'], 0, None)
        code, out, _, calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'R-0001',
                                        result=res)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('D-001', [('name', 'alpha')], 'R-0001', None)])
        self.assertIn('logged R-0002; ran: C:\\bin\\tool.exe alpha', out)
        self.assertIn('done: exit code 0', out)

    def test_a_question_trigger_gets_the_hub(self):
        self.write_list()
        res = drun.Result('R-0002', ['tool.exe'], 0, None)
        calls = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'Q-1001', result=res)[3]
        self.assertEqual(calls[0][3], 'HUB')

    def test_a_failed_command_exits_4(self):
        self.write_list()
        res = drun.Result('R-0002', ['tool.exe'], 3, None)
        code, _, err, _ = self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'R-0001',
                                    result=res)
        self.assertEqual(code, delegated.FAILED)
        self.assertIn('failed: exit code 3', err)

    def test_a_command_that_cannot_start_exits_4(self):
        self.write_list()
        res = drun.Result('R-0002', ['tool.exe'], None, 'could not start tool.exe: no')
        self.assertEqual(self.main('run', 'D-001', '--arg', 'name=alpha', '--trigger', 'R-0001',
                                   result=res)[0], delegated.FAILED)


if __name__ == '__main__':
    unittest.main()
