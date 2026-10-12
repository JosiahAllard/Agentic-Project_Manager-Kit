"""docs/test-tools.md "Home fence": the coordinator start's machine check refuses the fenced home
when no home is given, before it runs the machine lock from under it."""
import unittest

from kitlib import coordinatorstart
from kitlib.config import user as kuser
from tests.home import fence_decoy


class MachineHomeFenceTest(unittest.TestCase):
    def test_no_home_is_refused(self):
        fence_decoy(self)
        ran = []

        def run(argv):
            ran.append(argv)
            return '{}'
        with self.assertRaises(kuser.HomeFenced):
            coordinatorstart.machine({'repo': 'x'}, run=run)
        self.assertEqual(ran, [])


if __name__ == '__main__':
    unittest.main()
