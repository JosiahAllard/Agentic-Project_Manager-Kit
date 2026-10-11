"""kitlib.pmstart's machine-facing helpers with their system calls faked: the process listing, the
repository name lookup, the pm_data settings read and kit_sync's cannot-read lines."""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib import pmdata, pmstart
from kitlib.git import cmd as gitcmd, refs, repo

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ran(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class ListProcessesTest(unittest.TestCase):

    def call(self, result):
        with mock.patch.object(pmstart.subprocess, "run", return_value=result) as run:
            out = pmstart.list_processes()
        args, kw = run.call_args
        self.assertEqual(args[0][:3], ["powershell", "-NoProfile", "-NonInteractive"])
        self.assertEqual(kw["timeout"], pmstart.TIMEOUT)
        return out

    def test_a_list_is_returned_as_is_and_one_process_becomes_a_list(self):
        self.assertEqual(self.call(ran(out='[{"ProcessId": 1}, {"ProcessId": 2}]')),
                         [{"ProcessId": 1}, {"ProcessId": 2}])
        self.assertEqual(self.call(ran(out='{"ProcessId": 1}')), [{"ProcessId": 1}])
        self.assertEqual(self.call(ran(out="")), [])

    def test_a_failed_powershell_raises_with_its_exit_code_and_stderr(self):
        with self.assertRaises(RuntimeError) as c:
            self.call(ran(rc=3, err="  access denied \n"))
        self.assertEqual(str(c.exception), "powershell exit 3: access denied")


class ToplevelTest(unittest.TestCase):

    def test_names_the_repository_holding_the_path(self):
        with mock.patch.object(gitcmd, "subprocess_run", return_value=ran(out="C:/x/MyRepo\n")):
            self.assertEqual(pmstart._toplevel(KIT), "MyRepo")

    def test_outside_a_repository_or_without_git_gives_none(self):
        with mock.patch.object(gitcmd, "subprocess_run", return_value=ran(rc=128, out="C:/x/MyRepo\n")):
            self.assertIsNone(pmstart._toplevel(KIT))
        for exc in (OSError("no git"), subprocess.TimeoutExpired("git", 4)):
            with mock.patch.object(gitcmd, "subprocess_run", side_effect=exc):
                self.assertIsNone(pmstart._toplevel(KIT))


class PmDataSettingsTest(unittest.TestCase):

    def test_reads_the_machine_settings(self):
        with mock.patch.object(pmdata, "machine_settings", return_value={"path": "p"}):
            self.assertEqual(pmstart.pm_data_settings(), {"path": "p"})


class KitSyncCannotReadTest(unittest.TestCase):
    """Each git read kit_sync makes, failing, gives one 'kit not updated' line naming what failed."""

    SETTINGS = {"path": "D:/data"}

    def setUp(self):
        for target, name, value in ((repo, "main_checkout_toplevel", "D:/kit"),
                                    (refs, "current_branch", ("main", "")),
                                    (refs, "merge_in_progress", (False, "")),
                                    (repo, "git_path", ("D:/kit/.git/none", "")),
                                    (refs, "is_clean", (True, "")),
                                    (gitcmd, "run", ("", ""))):
            p = mock.patch.object(target, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("kitlib.records.landing.LandingLock")
        p.start().return_value.read.return_value = None
        self.addCleanup(p.stop)

    def test_each_failed_read(self):
        cases = ((repo, "main_checkout_toplevel", None, "no git checkout found for D:/data"),
                 (refs, "current_branch", (None, "bad"), "cannot read the kit's branch: bad"),
                 (refs, "merge_in_progress", (None, "bad"),
                  "cannot tell whether a merge is in progress: bad"),
                 (repo, "git_path", (None, "bad"), "cannot look for a cherry-pick is in progress: bad"),
                 (refs, "is_clean", (None, "bad"), "cannot read the kit's status: bad"))
        for target, name, value, text in cases:
            with self.subTest(name), mock.patch.object(target, name, return_value=value):
                self.assertEqual(pmstart.kit_sync(self.SETTINGS), ["kit not updated: " + text])

    def test_failed_ancestry_reads(self):
        with mock.patch.object(refs, "is_ancestor", return_value=(None, "bad")):
            self.assertEqual(pmstart.kit_sync(self.SETTINGS), ["kit not updated: bad"])
        with mock.patch.object(refs, "is_ancestor", side_effect=[(False, ""), (None, "worse")]):
            self.assertEqual(pmstart.kit_sync(self.SETTINGS), ["kit not updated: worse"])


if __name__ == "__main__":
    unittest.main()
