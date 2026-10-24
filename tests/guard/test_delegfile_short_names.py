"""The owner-delegations rule matches a wildcard part against the 8.3 short name too
(delegfile.glob_reaches through rules.glob_part), as
FindFirstFile does: `OWNER-~?.JSO` names owner-delegations.json. Run on a volume that gives short
names (world.short_name_root); skipped when no temp folder has them."""
import os
import unittest

from kitlib.guard import delegfile

from . import world as W
from .claudehost import decide
from .world import bash, ps


class PointerTreeCopy(unittest.TestCase):

    def setUp(self):
        root = W.short_name_root()
        if root is None:
            self.skipTest('no temp folder\'s volume gives short (8.3) names')
        self.w = W.World(root=root)
        self.addCleanup(self.w.cleanup)
        self.copy = os.path.join(self.w.data, '.pm-kit', 'owner-delegations.json')
        os.makedirs(os.path.dirname(self.copy))
        with open(self.copy, 'w', encoding='utf-8') as fh:
            fh.write('{"schema": 1, "operations": {}}\n')
        if not W.short_name(self.copy):
            self.skipTest('the volume gave the list no short (8.3) name')

    def decide(self, payload):
        return decide(payload, self.w.ctx(pointer=lambda: self.w.data))

    def test_a_short_name_wildcard_reaches_the_list(self):
        for payload in (bash('rm .pm-kit/OWNER-~?.JSO', self.w.data, True),
                        bash('rm .pm-kit/owner-~*', self.w.data, True),
                        ps('Remove-Item .pm-kit\\OWNER-~?.JSO', self.w.data, True)):
            with self.subTest(payload['tool_input']):
                d = self.decide(payload)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', delegfile.RULE), d.reason)

    def test_a_short_name_wildcard_that_names_another_file_is_not_this_rule(self):
        other = os.path.join(self.w.data, '.pm-kit', 'another-long-name.json')
        open(other, 'w').close()
        d = self.decide(bash('rm .pm-kit/ANOTHE~?.JSO', self.w.data, True))
        self.assertTrue(d is None or d.rule != delegfile.RULE, d and d.reason)


if __name__ == '__main__':
    unittest.main()
