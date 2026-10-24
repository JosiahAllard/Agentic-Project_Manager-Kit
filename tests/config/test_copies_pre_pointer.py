"""kitlib.config.copies: a copy whose user.py predates the PM repo pointer reader reads the
machine config from the home folder, so its SCHEMA decides nothing: it is HOME_READER, never
ready when in use and stale when not. Every copy is a temp tree."""
import os
import unittest

from kitlib.config import copies
from tests.config.test_copies import NEW, CopiesCase

PRE = NEW + '''
def user_config(home=None):
    return {}
'''
POST = PRE + "\nLOCAL_FILE_NAME = 'local.json'\n"


class PrePointerTest(CopiesCase):
    def test_this_checkouts_own_user_py_reads_the_pointer(self):
        kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertFalse(copies.pre_pointer(kit))

    def test_the_reader_without_the_pointer_file_name_is_pre_pointer(self):
        self.assertTrue(copies.pre_pointer(self.copy(os.path.join(self.top, 'a'), PRE)))
        self.assertFalse(copies.pre_pointer(self.copy(os.path.join(self.top, 'b'), POST)))
        self.assertFalse(copies.pre_pointer(self.copy(os.path.join(self.top, 'c'), NEW)))

    def test_a_pre_pointer_copy_in_use_is_a_home_reader_and_not_ready(self):
        path = self.copy(os.path.join(self.root, 'a' * 12), PRE)
        report = copies.check([copies.Copy(path, copies.PIN, True)], ['hub'])
        self.assertEqual(report.lines, [copies.HOME_READER % (path, copies.PIN)])
        self.assertFalse(report.ready)
        self.assertEqual(report.stale, 0)

    def test_a_pre_pointer_copy_not_in_use_is_stale(self):
        cur = self.copy(os.path.join(self.root, 'b' * 12), POST)
        old = self.copy(os.path.join(self.root, 'a' * 12), PRE)
        report = copies.check([copies.Copy(cur, copies.CURRENT, True),
                               copies.Copy(old, copies.EXPORT, False)], ['hub'])
        self.assertEqual(report.lines, [copies.KNOWS % (cur, copies.CURRENT),
                                        copies.HOME_READER % (old, copies.EXPORT)])
        self.assertTrue(report.ready)
        self.assertEqual(report.stale, 1)

    def test_a_post_pointer_copy_is_checked_by_its_schema(self):
        path = self.copy(os.path.join(self.root, 'a' * 12), POST)
        report = copies.check([copies.Copy(path, copies.PIN, True)], ['ledger_dir'])
        self.assertEqual(report.lines, [copies.REFUSES % (path, copies.PIN, 'ledger_dir')])

    def test_an_unreadable_copy_is_unknown_not_a_home_reader(self):
        path = self.copy(os.path.join(self.root, 'a' * 12), 'def (')
        (line,) = copies.check([copies.Copy(path, copies.PIN, True)], ['hub']).lines
        self.assertTrue(line.startswith(copies.UNKNOWN % (path, copies.PIN, '')), line)


if __name__ == '__main__':
    unittest.main()
