"""The built-in scrub check fails on an ALLOWED entry that no longer matches anything.

This lives apart from tests/scrub/test_builtin.py so that a red-first run, which copies only
this file onto an older tree, exercises that tree's own check.
"""
import unittest
from unittest import mock

from tests.scrub import test_builtin as me


class StaleAllow(unittest.TestCase):

    def test_a_stale_allow_entry_turns_the_gate_check_red(self):
        stale = ('docs/zz-gone.md', 'gender', 'nothing here', 'an invented entry no file matches')
        result = unittest.TestResult()
        with mock.patch.object(me, 'ALLOWED', me.ALLOWED + (stale,)):
            me.BuiltIn('test_no_new_owner_text_in_a_tracked_file').run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn("docs/zz-gone.md gender 'nothing here'", result.failures[0][1])


if __name__ == '__main__':
    unittest.main()
