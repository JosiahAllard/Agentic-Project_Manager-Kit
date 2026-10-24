"""kitlib.coordinatorstart: the machine config check (kitlib.pmstart.configcheck) in the
coordinator context, right after the standing lines, and after the skip line when there is no
coordinator_start config. Every case runs the real check on a temp home (the package stubs it:
tests/pmstart/__init__.py stub_config_lines)."""
import unittest
from unittest import mock

from kitlib import coordinatorstart
from kitlib.pmstart import configcheck
from tests import home as khome
from tests import pmstart as tpm
from tests.coordinatorstart import test_coordinatorstart as tc


COORD = {"PM_KIT_ROLE": coordinatorstart.ROLE}   # never this shell's own role


def setUpModule():
    p = mock.patch.object(configcheck, 'lines', tpm.REAL_CONFIG_LINES)
    p.start()
    unittest.addModuleCleanup(p.stop)


class MachineConfigTest(unittest.TestCase):
    def setUp(self):
        khome.temp_home(self)

    def test_the_line_follows_the_standing_lines(self):
        text = tc.ContextTest().build({"standing": ["wind down"], "cap": {"alpha": [{"cap": 4}]}})
        self.assertEqual(text.split("\n")[1:3], ["- wind down", "- " + configcheck.NO_POINTER])

    def test_a_failing_check_is_isolated(self):
        text = tc.ContextTest().build({"standing": ["s"]}, config_lines=lambda: 1 / 0)
        self.assertEqual(text.split("\n")[2], "- could not check machine_config: ZeroDivisionError: division by zero")
        self.assertIn("could not check cap", text)

    def test_with_no_config_the_skip_line_still_shows_the_machine_config_line(self):
        text = coordinatorstart.render(env=COORD, path=khome.temp_home(self) + "/none.json")
        lines = text.split("\n")
        self.assertIn("no coordinator_start config", lines[0])
        self.assertEqual(lines[1:], ["- " + configcheck.NO_POINTER])

    def test_with_no_config_a_failing_check_is_one_line(self):
        def boom():
            raise OSError("disk gone")
        text = coordinatorstart.render(env=COORD, path=khome.temp_home(self) + "/none.json", config_lines=boom)
        self.assertEqual(text.split("\n")[1], "- could not check machine_config: OSError: disk gone")

    def test_with_no_config_and_all_well_the_skip_line_stands_alone(self):
        text = coordinatorstart.render(env=COORD, path=khome.temp_home(self) + "/none.json", config_lines=lambda: [])
        self.assertEqual(len(text.split("\n")), 1)


if __name__ == "__main__":
    unittest.main()
