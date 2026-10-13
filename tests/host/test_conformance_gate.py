"""The conformance suite is in the gate's selection (tests/selection.json, kitlib.tooldeps.selftree):
a change to a host's folder, the host contract, the content every host renders, kit.json or
hosts.json runs tests/host, where every host's conformance subclass lives (docs/new-host.md)."""
import os
import unittest

from kitlib.tooldeps import selftree
from kitlib.tooldeps.deps import ALL

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHANGES = (
    'hosts.json',
    'kit.json',
    'kitlib/host/hookmain.py',
    'kitlib/host/content.py',
    'hosts/claude/adapter/events.py',
    'hosts/claude/shell/hooks/guard.py',
    'hosts/claude/shell/hub/hub_repos.py',
    'hosts/newhost/adapter/events.py',          # a host that does not exist yet
    'content/roles/worker.md',
    'content/skills/land-branch/SKILL.md',
    'tests/host/fakehost/adapter.py',
)


class Selection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = selftree.scan(ROOT)
        cls.cfg = selftree.config(ROOT)

    def test_every_host_change_runs_the_conformance_suite(self):
        for path in CHANGES:
            with self.subTest(path=path):
                picked = selftree.selection([path], self.tree, self.cfg)[0]
                self.assertTrue(picked is ALL or 'host' in picked, picked)

    def test_the_suite_is_collected_only_through_its_subclasses(self):
        modules = selftree.test_modules(ROOT, ['host'])
        self.assertNotIn('tests.host.conformance', modules)
        self.assertIn('tests.host.test_conformance_claude', modules)
        self.assertIn('tests.host.test_conformance_fake', modules)


if __name__ == '__main__':
    unittest.main()
