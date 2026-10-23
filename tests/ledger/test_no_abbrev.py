"""tools/ledger.py takes no abbreviated option (allow_abbrev=False on the parser and every
subparser): a permission rule written for one flag cannot be stepped around with a prefix of it
(docs/allow-rules.md "Abbreviations"). Arguments and exit codes only."""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import ledger as ledger_tool                                        # noqa: E402

ADD = ['add', '--repo', 'alpha', '--prov', 'selected', '--q', 'Which?', '--a', 'This',
       '--decides', 'Use this.', '--source', 'chat']


def parse(argv):
    """The parsed namespace, or the exit code of a usage error."""
    with redirect_stderr(io.StringIO()):
        try:
            return ledger_tool.parser().parse_args(argv)
        except SystemExit as e:
            return e.code


class NoAbbrevTest(unittest.TestCase):
    def test_full_names_parse(self):
        self.assertTrue(parse(ADD + ['--commit']).commit)
        self.assertEqual(parse(['retire', 'X', '--class', 'spent', '--why', 'w']).cls, 'spent')

    def test_abbreviations_are_usage_errors(self):
        for argv in (ADD[:-2] + ['--sour', 'chat'],
                     ADD + ['--comm'],
                     ADD + ['--branch'],
                     ['retire', 'X', '--cl', 'spent', '--why', 'w'],
                     ['list', '--pr', 'delegated', '--since', '2026-01-01']):
            with self.subTest(argv=argv):
                self.assertEqual(parse(argv), 2)


if __name__ == '__main__':
    unittest.main()
