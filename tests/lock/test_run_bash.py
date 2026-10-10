"""A bare `bash` under the machine lock (run_command, `machine_lock.py run`) runs Git Bash, never
WSL's bash (kitlib.proc.bash); with no Git Bash it is refused before the run books, so it never
waits in the queue for slots it cannot use. Scratch store only."""
import os
import unittest
from unittest import mock

from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm
    from kitlib.proc import bash


class BareBash(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)

    def run_bash(self, script):
        return rm.run_command(["bash", "-c", script], "engine-test", "kit", "bash", cores=1,
                              store=self.store, env=clean_env())

    def test_no_git_bash_is_refused_before_the_run_books(self):
        with mock.patch("kitlib.proc.bash.git_bash", return_value=None):
            self.assertEqual(self.run_bash("exit 0"), 1)
        self.assertTrue(any(s.startswith("could not start the command") and "WSL" in s
                            for s in self.said), self.said)
        ended = self.store.path("ended")
        self.assertFalse(os.path.isdir(ended) and os.listdir(ended), "the refused run was booked")
        self.assertEqual(self.store.history_records(), [])

    def test_a_bare_bash_runs_git_bash(self):
        if bash.git_bash() is None:
            self.skipTest("no Git Bash on this machine")
        marker = os.path.join(self.tmp, "ostype.txt").replace("\\", "/")
        self.assertEqual(self.run_bash('echo "$OSTYPE" > "%s"' % marker), 0, self.said)
        with open(marker) as f:
            self.assertRegex(f.read().strip(), r"^(msys|cygwin)")


if __name__ == "__main__":
    unittest.main()
