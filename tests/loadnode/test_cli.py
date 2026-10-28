"""tools/node.py: its arguments and exit codes. The registry is a temp PM repository's; a runner
stands in for ssh."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from kitlib.loadnode import registry
from tests import home as thome
from tests.loadnode.test_registry import node, text
from tests.loadnode.test_remote import Runner, ok_reply
from tools import node as node_cli


class Cli(unittest.TestCase):

    def setUp(self):
        self.home = thome.temp_home(self)
        self.folder = thome.machine_config(self.home, {})
        self.file = os.path.join(self.folder, registry.FILE_NAME)
        p = mock.patch.object(registry, 'ignored', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def write(self, *nodes):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write(text(*nodes))

    def run_cli(self, argv, runner=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = node_cli.main(argv, runner=runner)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_list_needs_no_network(self):
        self.write(node(), node(name='b2', identity_file='~/.ssh/k'))
        code, out, _ = self.run_cli(['list'], runner=mock.Mock(side_effect=AssertionError))
        self.assertEqual(code, 0)
        self.assertIn('box1  pmnode@192.168.1.50:22', out)
        self.assertIn('~/.ssh/k (MISSING)', out)

    def test_status_every_node_answers_is_0(self):
        self.write(node(), node(name='b2'))
        code, out, _ = self.run_cli(['status'], Runner(out=ok_reply()))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)

    def test_status_one_node_failing_is_1_and_every_node_is_listed(self):
        self.write(node(), node(name='b2'))

        def runner(argv, timeout_s):
            return (255, b'', b'ssh: connect: No route to host\n') if 'HostKeyAlias=pmkit-node-b2' in argv \
                else (0, ok_reply(), b'')
        code, out, _ = self.run_cli(['status'], runner)
        self.assertEqual(code, 1)
        self.assertIn('box1', out)
        self.assertIn('b2: unreachable: ssh: connect: No route to host', out)

    def test_status_of_named_nodes_and_json(self):
        self.write(node(), node(name='b2'))
        run = Runner(out=ok_reply())
        code, out, _ = self.run_cli(['status', 'b2', '--json', '--timeout', '5'], run)
        self.assertEqual(code, 0)
        got = json.loads(out)
        self.assertEqual(list(got), ['b2'])
        self.assertEqual((got['b2']['state'], run.timeout), ('ok', 5))

    def test_an_unknown_name_is_usage(self):
        self.write(node())
        code, _, err = self.run_cli(['status', 'nope'], Runner())
        self.assertEqual(code, 2)
        self.assertIn('no node named nope', err)

    def test_a_bad_or_missing_registry_is_2(self):
        code, _, err = self.run_cli(['list'])
        self.assertEqual(code, 2)
        self.assertIn('does not exist', err)
        self.write(node(address='-oProxyCommand=x'))
        code, _, err = self.run_cli(['status'], Runner())
        self.assertEqual(code, 2)
        self.assertIn('REFUSED', err)

    def test_a_registry_git_does_not_ignore_is_2(self):
        self.write(node())
        with mock.patch.object(registry, 'ignored', return_value='not ignored by git'):
            code, _, err = self.run_cli(['list'])
        self.assertEqual(code, 2)
        self.assertIn('not ignored', err)

    def test_usage_errors_are_2(self):
        for argv in ([], ['run'], ['status', '--timeout', '0'], ['status', '--timeout', 'x'],
                     ['list', 'extra'], ['status', '--jso']):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv)[0], 2)

    def test_an_empty_registry(self):
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write('{"version": 1, "nodes": []}')
        code, out, _ = self.run_cli(['status'], Runner())
        self.assertEqual((code, 'no load nodes' in out), (0, True))


if __name__ == '__main__':
    unittest.main()
