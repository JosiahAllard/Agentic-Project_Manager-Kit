"""tools/launch.py takes no abbreviated option (allow_abbrev=False on the parser and every
subparser): a permission rule that denies `--no-agent*` or `--force*` cannot be stepped around with
`--no-ag` or `--forc` (docs/allow-rules.md "Abbreviations"). Arguments and exit codes only."""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import launch                                                       # noqa: E402


def parse(argv):
    """The parsed namespace, or the exit code of a usage error."""
    with redirect_stderr(io.StringIO()):
        try:
            return launch.parser().parse_args(argv)
        except SystemExit as e:
            return e.code


class NoAbbrevTest(unittest.TestCase):
    def test_full_names_parse(self):
        self.assertTrue(parse(['start', '--repo', 'x', '--no-agent']).no_agent)
        self.assertEqual(parse(['start', '--repo', 'x', '--prompt', 'p']).prompt, 'p')
        self.assertTrue(parse(['deep', '--repo-path', 'd', '--requester', 'r', '--slug', 's',
                               '--force']).force)
        self.assertTrue(parse(['consultant', '--repo-path', 'd', '--replace']).replace)

    def test_abbreviations_are_usage_errors(self):
        for argv in (['start', '--repo', 'x', '--no-ag'],
                     ['start', '--repo', 'x', '--prom', 'p'],
                     ['start', '--rep', 'x'],
                     ['deep', '--repo-path', 'd', '--requester', 'r', '--slug', 's', '--forc'],
                     ['consultant', '--repo-path', 'd', '--repl'],
                     ['stop', '--na', 'x']):
            with self.subTest(argv=argv):
                self.assertEqual(parse(argv), launch.USAGE)


if __name__ == '__main__':
    unittest.main()
