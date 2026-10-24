"""The DOS wildcards `<`, `>` and `"`: FindFirstFile reads them as
wildcards, so a path with one is read both ways, as a wildcard and as the literal name, by the
owner-delegations rule (delegfile.word_reaches) and the hub-secrets rule (rules.hub_texts), as
the ledger rule already does (rules.LEDGER_GLOB_CHARS). Each reading only adds a refusal."""
import os
import unittest

from kitlib.guard import delegfile

from . import world as W
from .claudehost import decide
from .test_rules_hub import Hub
from .world import bash, ps

SUB = ({}, True)


class DelegationListInThePointerTree(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        copy = os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json')
        os.makedirs(os.path.dirname(copy))
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write('{"schema": 1, "operations": {}}\n')

    def decide(self, payload):
        return decide(payload, self.w.ctx(pointer=lambda: self.w.data))

    def test_a_dos_wildcard_folder_reaches_the_list(self):
        for text in ('rm -r .pm-ki\\<', 'rm -r .pm-ki\\</x/..', 'rm -r .pm-ki\\>', 'rm -r ".pm-ki\\""',
                     'rm .pm-kit/owner-delegations.jso\\<'):
            with self.subTest(text):
                d = self.decide(bash(text, self.w.data, True))
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', delegfile.RULE), d.reason)
        d = self.decide(ps('Remove-Item -Recurse ".pm-ki<"', self.w.data, True))
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', delegfile.RULE), d.reason)

    def test_the_same_star_form_keeps_its_rule(self):
        d = self.decide(bash('rm -r .pm-ki*', self.w.data, True))
        self.assertEqual(d and d.rule, delegfile.RULE, d and d.reason)

    def test_a_dos_wildcard_that_cannot_match_the_list_is_not_this_rule(self):
        for text in ('rm -r docs\\<', 'rm .pm-kit/c\\<'):
            with self.subTest(text):
                d = self.decide(bash(text, self.w.data, True))
                self.assertTrue(d is None or d.rule != delegfile.RULE, d and d.reason)


class HubTokenFolder(Hub):

    def test_a_dos_wildcard_after_the_token_folder_is_in_it(self):
        home = W.fwd(self.w.home)
        for shell, line in ((bash, 'cat ~/.pm-kit/hub/o\\</../../x'),
                            (bash, 'cat %s/.pm-kit/hub/o\\<' % home),
                            (bash, 'cat "%s/.pm-kit/hub/o>"' % home),
                            (ps, 'gc "%s\\.pm-kit\\hub\\o<\\..\\..\\x"' % self.w.home)):
            with self.subTest(line=line):
                self.denied(lambda sub, shell=shell, line=line: shell(line, self.w.wt, sub), 'hub-secrets')

    def test_the_same_star_form_is_refused(self):
        self.denied(lambda sub: bash('cat ~/.pm-kit/hub/o*/../../x', self.w.wt, sub), 'hub-secrets')


if __name__ == '__main__':
    unittest.main()
