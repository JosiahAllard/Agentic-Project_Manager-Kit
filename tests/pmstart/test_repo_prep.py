"""kitlib.pmstart.repo_prep: the PM session start runs the first-use repo prep (kitlib.git.prep;
docs/new-worktree.md "First-use prep") on each launcher.repos repository. The prep is a fake here;
tests/git/test_prep.py tests the prep itself."""
import datetime
import os
import tempfile
import unittest

from kitlib import pmstart
from tests.home import temp_home
from tests.pmstart import HANDOFF_FAKE, keep_off_hub_site, keep_off_launcher_repos

NOW = datetime.datetime(2026, 10, 13, 9, 0)
FAKES = dict(pm_data=lambda: None, handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, stubs={"repos": {}},
             watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0})


def setUpModule():
    keep_off_launcher_repos()
    keep_off_hub_site()         # the checklist's hub check reads no machine config, token or server


class RepoPrep(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.mkdtemp(prefix="kit-prep-a-")
        self.b = tempfile.mkdtemp(prefix="kit-prep-b-")
        for d in (self.a, self.b):
            self.addCleanup(os.rmdir, d)
        self.asked = []

    def prep(self, path):
        self.asked.append(path)
        return ["repo prep: %s: did it" % path] if path == self.a else []

    def test_each_launcher_repo_is_prepped_and_only_its_lines_print(self):
        lines = pmstart.repo_prep({"b": self.b, "a": self.a}, prep=self.prep)
        self.assertEqual(self.asked, [self.a, self.b])
        self.assertEqual(lines, ["repo prep: %s: did it" % self.a])

    def test_a_missing_folder_is_its_own_line(self):
        gone = os.path.join(self.a, "gone")
        lines = pmstart.repo_prep({"x": gone}, prep=self.prep)
        self.assertEqual(lines, ["could not prep x: no repository folder %s" % gone])
        self.assertEqual(self.asked, [])

    def test_the_checklist_carries_the_prep_lines(self):
        temp_home(self)
        txt = pmstart.checklist({}, NOW, procs=[], repo_prep={"repos": {"a": self.a}, "prep": self.prep},
                                **FAKES)
        self.assertIn("- repo prep: %s: did it" % self.a, txt)

    def test_a_prep_that_raises_is_a_could_not_check_line(self):
        temp_home(self)

        def boom(path):
            raise RuntimeError("no")
        txt = pmstart.checklist({}, NOW, procs=[], repo_prep={"repos": {"a": self.a}, "prep": boom}, **FAKES)
        self.assertIn("could not check repo_prep: RuntimeError: no", txt)


if __name__ == "__main__":
    unittest.main()
