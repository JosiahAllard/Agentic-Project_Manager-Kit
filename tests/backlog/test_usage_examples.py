"""A usage error from tools/backlog.py ends with one example line for the verb called
(KIT-USAGE-EXAMPLES), and every example is a command the parser takes."""
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from kitlib.backlog import record as rmod
from tests.backlog.helpers import StoreCase, kit_json_ref
from tests.gitfixture import fixture_env, rmtree

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
spec = importlib.util.spec_from_file_location('backlog_cli_under_test', TOOL)
CLI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CLI)


class Examples(unittest.TestCase):

    def test_every_verb_has_an_example_and_it_parses(self):
        parser = CLI.build_parser()
        verbs = parser._subparsers._group_actions[0].choices
        self.assertEqual(sorted(verbs), sorted(CLI.EXAMPLES))
        for verb, example in CLI.EXAMPLES.items():
            argv = shlex.split(example)
            self.assertEqual(argv[0], 'backlog.py', verb)
            args = parser.parse_args(argv[1:])
            self.assertEqual(args.verb, verb, example)
            if verb == 'block':
                self.assertIsNone(rmod.block_citation_error(args.on, args.reason, args.owner_ask))


class Errors(StoreCase):

    def run_tool(self, *args):
        home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})
        p = subprocess.run([sys.executable, TOOL, '-C', self.repo.root] + list(args),
                           capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_a_parser_error_ends_with_the_verbs_example(self):
        rc, out = self.run_tool('add', 'FOO')
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.rstrip().endswith('example: %s' % CLI.EXAMPLES['add']), out)

    def test_block_on_owner_without_a_reason_ends_with_the_example(self):
        rc, out = self.run_tool('block', 'FOO', '--on', 'owner')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)
        self.assertTrue(out.rstrip().endswith('example: %s' % CLI.EXAMPLES['block']), out)

    def test_a_summary_that_is_too_long_ends_with_the_example(self):
        kit_json_ref(self.repo)
        rc, out = self.run_tool('add', 'FOO', '--summary', 'x' * 130, '--scope', 'tools/x.py',
                                '--found-on', 'none', '--why-not-folded', 'a test')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE:', out)
        self.assertTrue(out.rstrip().endswith('example: %s' % CLI.EXAMPLES['add']), out)

    def test_a_refusal_that_is_not_a_usage_error_has_no_example(self):
        kit_json_ref(self.repo)
        rc, out = self.run_tool('show', 'NOPE')
        self.assertEqual(rc, 1, out)
        self.assertNotIn('example:', out)


if __name__ == '__main__':
    unittest.main()
