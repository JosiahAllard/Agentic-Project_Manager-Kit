"""tools/hub.py: a usage error ends with one example line for the verb called, the hub's refusal of
a --record ends with the form it takes, and every example is a command the parser takes
(KIT-USAGE-EXAMPLES)."""
import argparse
import contextlib
import io
import shlex
import unittest
from unittest import mock

from kitlib.hubsite import client
from tests.hubsite.test_hub_cli import HUB


def leaves(parser):
    """Every sub-parser below `parser` (each verb and sub-verb), as its prog -> parser."""
    found = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found[sub.prog] = sub
                found.update(leaves(sub))
    return found


def run(argv):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            code = HUB.main(argv)
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


class Examples(unittest.TestCase):

    def test_every_verb_has_an_example_and_it_parses(self):
        p = HUB.parser()
        progs = leaves(p)
        self.assertEqual(sorted(progs), sorted(HUB.EXAMPLES))
        for prog, example in HUB.EXAMPLES.items():
            argv = shlex.split(example)
            self.assertEqual(argv[0], 'hub.py')
            args = p.parse_args(argv[1:])
            self.assertEqual(argv[1:len(prog.split())], prog.split()[1:], example)
            self.assertEqual(args.verb, prog.split()[1], example)

    def test_a_usage_error_ends_with_the_verbs_example(self):
        for argv, prog in ((['questions', 'add'], 'hub.py questions add'),
                           (['questions', 'show'], 'hub.py questions show'),
                           (['wait', '--timeout', 'x'], 'hub.py wait'),
                           (['answer', 'Q-1'], 'hub.py answer')):
            code, err = run(argv)
            self.assertEqual(code, 2, (argv, err))
            self.assertTrue(err.rstrip().endswith('example: %s' % HUB.EXAMPLES[prog]), (argv, err))

    def test_a_malformed_record_ends_with_the_questions_add_example(self):
        code, err = run(['questions', 'add', '--title', 't', '--record', 'EX-1'])
        self.assertEqual(code, 2, err)
        self.assertIn('<pm-folder>:<ID>', err.rstrip().splitlines()[-1])

    def test_the_hubs_refusal_of_a_record_ends_with_the_example(self):
        refused = client.Refused(404, {'error': {'code': 'not_found', 'field': 'record',
                                                  'message': 'record names repo pm'}})
        with mock.patch.dict(HUB.VERBS, {'questions': mock.Mock(side_effect=refused)}):
            code, err = run(['questions', 'add', '--title', 't', '--record', 'pm:EX-1'])
        self.assertEqual(code, client.REFUSED, err)
        self.assertIn('REFUSED not_found: record names repo pm', err)
        self.assertTrue(err.rstrip().endswith('example: %s' % HUB.EXAMPLES['hub.py questions add']),
                        err)

    def test_a_refusal_of_another_field_has_no_example(self):
        refused = client.Refused(400, {'error': {'code': 'invalid', 'field': 'title',
                                                  'message': 'bad'}})
        with mock.patch.dict(HUB.VERBS, {'questions': mock.Mock(side_effect=refused)}):
            code, err = run(['questions', 'add', '--title', 't'])
        self.assertNotIn('example:', err)


if __name__ == '__main__':
    unittest.main()
