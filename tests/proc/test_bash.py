"""Tests for kitlib.proc.bash: a bare bash runs Git Bash, never WSL's, or is refused clearly."""
import os
import shutil
import subprocess
import tempfile
import unittest

from kitlib.proc import bash
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.proc import job

GIT = r"C:\Tools\Git\usr\bin\bash.exe"


def found():
    return GIT


def none():
    return None


class Resolve(unittest.TestCase):
    def test_a_bare_list_head_is_replaced(self):
        for head in ("bash", "bash.exe", "BASH", "Bash.EXE"):
            self.assertEqual(bash.resolve([head, "-c", "x"], finder=found), [GIT, "-c", "x"])
        self.assertEqual(bash.resolve(("bash",), finder=found), [GIT])

    def test_anything_else_is_left_alone(self):
        for cmd in (["python", "-V"], [r"D:\msys\bash.exe", "-c", "x"], ["bashful"], [],
                    "python -V", "bashful x", r"D:\msys\bash.exe -c x", '"D:\\b\\bash.exe" x'):
            self.assertIs(bash.resolve(cmd, finder=none), cmd)

    def test_a_bare_shell_string_head_is_replaced(self):
        self.assertEqual(bash.resolve('bash -c "echo hi"', True, finder=found),
                         '"%s" -c "echo hi"' % GIT)
        self.assertEqual(bash.resolve('  "bash.exe" -lc x', True, finder=found),
                         '  "%s" -lc x' % GIT)
        self.assertEqual(bash.resolve("bash", True, finder=found), '"%s"' % GIT)

    def test_no_git_bash_is_a_clear_refusal(self):
        for cmd in (["bash", "-c", "x"], "bash -c x"):
            with self.assertRaises(bash.BareBashError) as e:
                bash.resolve(cmd, finder=none)
            self.assertIsInstance(e.exception, OSError)
            self.assertIn("WSL", str(e.exception))
            self.assertIn("full path", str(e.exception))


class GitBash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        return p

    def env(self, *dirs):
        return {"PATH": os.pathsep.join(os.path.join(self.tmp, d) for d in dirs),
                "SystemRoot": os.path.join(self.tmp, "Win")}

    def no_git(self, name, path=None):
        return None

    def test_the_first_bash_on_path_that_is_not_wsls_wins(self):
        self.make("Win", "System32", "bash.exe")
        self.make("WindowsApps", "bash.exe")
        want = self.make("Git", "usr", "bin", "bash.exe")
        self.make("Other", "bash.exe")
        env = self.env(os.path.join("Win", "System32"), "WindowsApps",
                       os.path.join("Git", "usr", "bin"), "Other")
        self.assertEqual(bash.git_bash(env, which=self.no_git), want)

    def test_else_the_bash_beside_git(self):
        self.make("Win", "System32", "bash.exe")
        git = self.make("Git", "cmd", "git.exe")
        want = self.make("Git", "bin", "bash.exe")
        env = self.env(os.path.join("Win", "System32"), os.path.join("Git", "cmd"))
        self.assertEqual(bash.git_bash(env, which=lambda n, path=None: git), want)

    def test_else_usr_bin_beside_a_mingw_git(self):
        git = self.make("Git", "mingw64", "bin", "git.exe")
        want = self.make("Git", "usr", "bin", "bash.exe")
        self.assertEqual(bash.git_bash(self.env(), which=lambda n, path=None: git), want)

    def test_only_wsl_is_none(self):
        self.make("Win", "System32", "bash.exe")
        env = self.env(os.path.join("Win", "System32"))
        self.assertIsNone(bash.git_bash(env, which=self.no_git))


@windows_only("Windows job objects")
class InAJob(unittest.TestCase):
    def test_a_bare_bash_in_a_job_runs_git_bash(self):
        if bash.git_bash() is None:
            self.skipTest("no Git Bash on this machine")
        j = job.Job()
        try:
            p = j.start(["bash", "-c", "echo $OSTYPE"], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT)
            out, _ = p.communicate(timeout=60)
        finally:
            j.end(kill=True)
        text = out.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, text)
        self.assertRegex(text.strip(), r"^(msys|cygwin)", text)


if __name__ == "__main__":
    unittest.main()
