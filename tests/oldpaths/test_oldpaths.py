"""The old hub path stays live until the hub link points at claude/hub; delete this package then.
The old hook stubs (hooks/*.py) are gone: settings and the plugin run claude/hooks/ directly."""
import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


class HubCopyTest(unittest.TestCase):
    def test_old_hub_is_byte_identical(self):
        with open(os.path.join(ROOT, "hub", "action-hub.html"), "rb") as a, \
                open(os.path.join(ROOT, "claude", "hub", "action-hub.html"), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
