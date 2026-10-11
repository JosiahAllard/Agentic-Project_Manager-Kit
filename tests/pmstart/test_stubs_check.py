"""kitlib.pmstart.stubs: the session-start stubs check over launcher.repos (fakes injected)."""
import datetime
import os
import tempfile
import unittest

from kitlib import pmstart
from tests.pmstart import keep_off_plugin_export

NOW = datetime.datetime(2026, 10, 6, 9, 0)
FAKES = dict(pm_data=lambda: None, handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"})


def setUpModule():
    keep_off_plugin_export()    # the checklist's plugin_current check reads nothing of this machine


class StubsCheckTest(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.a, self.b = os.path.join(d.name, "a"), os.path.join(d.name, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)

    def test_current_repos_are_silent(self):
        self.assertEqual(pmstart.stubs({"a": self.a, "b": self.b}, check=lambda p: None), [])

    def test_one_line_per_repo_with_problems(self):
        got = pmstart.stubs({"a": self.a, "b": self.b},
                            check=lambda p: "stubs (b): 2 missing/stale: x, y" if p == self.b else None)
        self.assertEqual(got, ["stubs (b): 2 missing/stale: x, y"])

    def test_a_missing_folder_is_a_could_not_check_line(self):
        gone = os.path.join(self.a, "gone")
        self.assertEqual(pmstart.stubs({"g": gone}, check=lambda p: None),
                         ["could not check stubs (g): no repository folder %s" % gone])

    def test_a_check_that_raises_is_isolated_to_its_repo(self):
        def check(p):
            if p == self.a:
                raise OSError("disk gone")
            return "stubs (b): 1 missing/stale: z"
        got = pmstart.stubs({"a": self.a, "b": self.b}, check=check)
        self.assertEqual(got, ["could not check stubs (a): OSError: disk gone",
                               "stubs (b): 1 missing/stale: z"])

    def test_the_real_check_names_the_missing_stubs_of_a_repo(self):
        got = pmstart.stubs({"a": self.a})
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("stubs (a): ") and "missing/stale: backlog" in got[0], got)

    def test_in_the_checklist(self):
        txt = pmstart.checklist({}, NOW, procs=[], **FAKES,
                                stubs={"repos": {"a": self.a}, "check": lambda p: "stubs (a): 1 missing/stale: q"})
        self.assertIn("- stubs (a): 1 missing/stale: q", txt)
        txt = pmstart.checklist({}, NOW, procs=[], **FAKES, stubs={"repos": {"a": self.a}, "check": lambda p: None})
        self.assertNotIn("- stubs (", txt)


if __name__ == "__main__":
    unittest.main()
