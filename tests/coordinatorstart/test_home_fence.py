"""docs/test-tools.md "Home fence": the coordinator start's machine check, given no kit, runs the
kit's own lock tool (kitlib.kitroot.lock_tool) and looks up no home: under the fenced home it
still runs, and the path it runs is not under that home."""
import os
import unittest

from kitlib import coordinatorstart, kitroot
from tests.home import fence_decoy


class MachineHomeFenceTest(unittest.TestCase):
    def test_no_kit_runs_the_kits_own_tool_not_one_under_the_home(self):
        home = fence_decoy(self)
        ran = []

        def run(argv):
            ran.append(argv)
            return '{}'
        with self.assertRaises(ValueError):        # '{}' is no snapshot: refused, not read as idle
            coordinatorstart.machine({'repo': 'x'}, run=run)
        self.assertEqual(len(ran), 1, ran)
        self.assertEqual(ran[0][1], kitroot.lock_tool())
        self.assertEqual(ran[0][-2:], ['status', '--json'])
        self.assertFalse(os.path.normcase(ran[0][1]).startswith(os.path.normcase(home) + os.sep), ran[0])


if __name__ == '__main__':
    unittest.main()
