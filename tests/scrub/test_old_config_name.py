"""The retired machine config folder name (kitlib.config.migration.OLD_DIR_NAME): its literal
stands on one line of kitlib/config/migration.py (tests/scrub/test_generic.py KIT_NAMES_ALLOWED),
and the constant is used only by the modules that handle the move, listed here, so retiring
the fallback finds every place to clean."""
import re
import unittest

from kitlib.config import migration
from tests.scrub.test_scrub import read_text, tracked_files

USERS = (
    'kitlib/config/migration.py',     # defines it
    'kitlib/config/user.py',          # locate(): the read fallback
    'kitlib/machinesetup/__init__.py',  # not a Claude account folder
    'kitlib/guard/jail.py',           # no mark root on either config folder
    'docs/kit-json.md',               # "Moving the machine config folder" names it
    'docs/machine-setup.md',          # the folders machine_setup skips
)
NAME = re.compile(r'\bOLD_DIR_NAME\b')


class OldConfigNameTest(unittest.TestCase):

    def test_the_constant_is_used_only_where_the_move_is_handled(self):
        self.assertTrue(migration.OLD_DIR_NAME)
        found = []
        for rel in tracked_files():
            if rel.startswith('tests/') or rel in USERS:
                continue
            text = read_text(rel)
            if text and NAME.search(text):
                found.append(rel)
        self.assertEqual(found, [], 'OLD_DIR_NAME used outside the move\'s modules (add the module to '
                                    'USERS with its reason, or derive the path from kitlib.config.user.locate)')


if __name__ == '__main__':
    unittest.main()
