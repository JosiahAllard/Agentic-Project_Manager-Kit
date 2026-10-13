"""Text checks on hosts/claude/shell/hub/action-hub.html for wiring that cannot run under node.

They guard against deleting the wiring; the behaviour is checked by eye (docs/action-hub.md "Alert the PM").
"""
import re
import unittest

from tests.hubtopm.test_hubtopm import page


class WiringTest(unittest.TestCase):
    def test_no_snapshot_error_handler_is_empty(self):
        src = page()
        self.assertGreater(src.count(".onSnapshot("), 5)
        self.assertNotIn("}, () => {});", src)
        self.assertNotRegex(src, r"onSnapshot\([^;]*\(\) => \{\}\)")

    def test_every_snapshot_names_a_failure_handler(self):
        src = page()
        n = src.count(".onSnapshot(")
        self.assertEqual(len(re.findall(r"snapFail\(\"\w+\"", src)), n)

    def test_every_good_snapshot_clears_its_banner_entry(self):
        src = page()
        for name in re.findall(r"db\.collection\(\"(\w+)\"\)\.onSnapshot\(snap => \{ snapOk\(\"(\w+)\"\);", src):
            self.assertEqual(name[0], name[1])
        self.assertEqual(len(re.findall(r"onSnapshot\(snap => \{ snapOk\(", src)), src.count(".onSnapshot("))
        self.assertIn("delete loadErr[name]", src)

    def test_owner_actions_schedule_an_alert(self):
        self.assertGreaterEqual(page().count("ownerActed();"), 4)

    def test_false_no_session_promise_is_gone(self):
        self.assertNotIn("checks the hub at its next turn anyway", page())
        self.assertNotIn("it reads them at its next turn", page())
