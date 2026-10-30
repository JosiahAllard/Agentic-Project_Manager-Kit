"""`machine_lock.py run -- ./gradlew` on Windows (kitlib.lock.run script_command): a name with no
extension resolves to a .bat or .cmd as the shell would (over PATHEXT, beside a name with a folder
part; a bare name on PATH only, never the current folder, and never in place of an .exe
CreateProcess finds), a bare x.bat is the file CreateProcess would start (the current folder
included), and a .bat or .cmd, resolved or named, starts through an explicit cmd.exe
line (SystemRoot's) the kit quotes itself, in the run's job. An argument cmd.exe would expand or
split (`%`, `"`, a line break) is refused before the run books. Scratch store only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.platforms import windows_only

from . import test_cli
from .helpers import StoreCase, clean_env

if os.name == "nt":
    from kitlib.lock import run as rm


def make_scripts(folder, bat_lines):
    """folder/gradlew (a shell script Windows cannot start) and folder/gradlew.bat beside it."""
    with open(os.path.join(folder, "gradlew"), "w", newline="\n") as f:
        f.write("#!/bin/sh\necho sh\n")
    with open(os.path.join(folder, "gradlew.bat"), "w", newline="\r\n") as f:
        f.write("\n".join(bat_lines) + "\n")


def in_cwd(case, folder):
    """case runs in folder, with Windows looking in the current folder for a bare name
    (NoDefaultCurrentDirectoryInExePath unset), until it ends."""
    here = os.getcwd()
    os.chdir(folder)
    case.addCleanup(os.chdir, here)
    patch = mock.patch.dict(os.environ, NoDefaultCurrentDirectoryInExePath="")
    patch.start()
    case.addCleanup(patch.stop)


@windows_only("a .bat or .cmd and PATHEXT are Windows'")
class ScriptCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lock-resolve-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        make_scripts(self.tmp, ["@exit /b 0"])
        self.bat = os.path.join(self.tmp, "gradlew.bat")

    def head(self):
        return '"%s" /d /v:off /s /c ""%s"' % (rm.comspec(), self.bat)

    def test_a_folder_name_resolves_to_the_bat_beside_it(self):
        line = rm.script_command([os.path.join(self.tmp, "gradlew"), "build"])
        self.assertEqual(line.lower(), (self.head() + ' build"').lower())

    def test_a_bare_name_resolves_on_path(self):
        line = rm.script_command(["gradlew", "build"], dict(clean_env(), PATH=self.tmp))
        self.assertEqual(line.lower(), (self.head() + ' build"').lower())

    def test_a_bat_named_by_its_full_path_gets_the_same_line(self):
        self.assertEqual(rm.script_command([self.bat, "build"]), self.head() + ' build"')

    def test_an_exe_or_no_match_is_left_as_given(self):
        for cmd in (["python", "-c", "pass"], ["no-such-tool-xyz", "a"],
                    [os.path.join(self.tmp, "nothing-here")], "gradlew build"):
            self.assertIs(rm.script_command(cmd), cmd)

    def test_arguments_are_quoted_for_cmd(self):
        line = rm.script_command([self.bat, "plain", "a&b", "x y", "", "(c)", "C:\\d x\\",
                                  "v\x0bt", "f\x0cf"])
        self.assertEqual(line, self.head() + ' plain "a&b" "x y" "" "(c)" "C:\\d x\\\\" '
                               '"v\x0bt" "f\x0cf""')

    def test_an_argument_cmd_would_expand_or_split_is_refused(self):
        for arg, shown in (("%OS%", "`%`"), ('a"b', '`"`'), ("a\nb", "a line break"),
                           ("a\rb", "a line break")):
            with self.assertRaises(rm.ScriptArgError) as cm:
                rm.script_command([os.path.join(self.tmp, "gradlew"), "ok", arg])
            self.assertIsInstance(cm.exception, OSError)
            self.assertIn("argument 2 of gradlew.bat holds %s" % shown, str(cm.exception))

    def test_a_bare_name_keeps_an_exe_createprocess_finds(self):
        shims, real = (os.path.join(self.tmp, d) for d in ("shims", "real"))
        os.mkdir(shims)
        os.mkdir(real)
        for name in ("tool.bat", "python.bat"):
            open(os.path.join(shims, name), "w").close()
        open(os.path.join(real, "tool.exe"), "w").close()
        env = dict(clean_env(), PATH=os.pathsep.join((shims, real)))
        for cmd in (["tool", "%x%"], ["python", "-c", "print('%x%')"]):
            self.assertIs(rm.script_command(cmd, env), cmd)

    def in_tmp(self):
        """The current folder is self.tmp; returns an env whose PATH is an empty folder."""
        empty = os.path.join(self.tmp, "empty")
        os.mkdir(empty)
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        return dict(clean_env(), PATH=empty)

    def test_an_extensionless_bare_name_is_never_looked_for_in_the_current_folder(self):
        env = self.in_tmp()
        for nodefault in ("", "1"):
            with mock.patch.dict(os.environ, NoDefaultCurrentDirectoryInExePath=nodefault):
                cmd = ["gradlew", "a"]
                self.assertIs(rm.script_command(cmd, env), cmd)

    def test_a_bare_bat_is_the_file_createprocess_would_start(self):
        """Red before: a bare x.bat CreateProcess finds in its own folders (the current one
        here) or on this process's PATH was left as given, so CreateProcess's own cmd.exe start
        read its arguments; a trailing dot or space hid the .bat the same way."""
        env = self.in_tmp()
        with mock.patch.dict(os.environ, NoDefaultCurrentDirectoryInExePath=""):
            for name in ("gradlew.bat", "GRADLEW.BAT", "gradlew.bat.", "gradlew.bat . "):
                with self.subTest(name=name):
                    self.assertEqual(str(rm.script_command([name, "a"], env)).lower(),
                                     (self.head() + ' a"').lower())
        with mock.patch.dict(os.environ, NoDefaultCurrentDirectoryInExePath="1"):
            cmd = ["gradlew.bat", "a"]      # CreateProcess skips the current folder: as given
            self.assertIs(rm.script_command(cmd, env), cmd)
            with mock.patch.dict(os.environ, PATH=self.tmp):    # its PATH, not the child's
                self.assertEqual(str(rm.script_command(cmd, env)).lower(),
                                 (self.head() + ' a"').lower())

    def test_a_trailing_dot_or_space_after_a_folder_part_is_dropped(self):
        for name in (self.bat + ".", self.bat + " "):
            with self.subTest(name=name):
                self.assertEqual(rm.script_command([name, "a"]), self.head() + ' a"')

    def test_cmd_is_systemroots_and_nothing_else(self):
        self.assertEqual(os.path.normcase(rm.comspec()), os.path.normcase(
            os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")))
        with mock.patch.dict(os.environ, ComSpec=os.path.join(self.tmp, "other.exe")):
            self.assertTrue(rm.script_command([self.bat]).startswith('"%s" ' % rm.comspec()))
        unset = {k: v for k, v in os.environ.items() if k.upper() != "SYSTEMROOT"}
        for patched in (unset, dict(os.environ, SystemRoot=self.tmp)):
            with mock.patch.dict(os.environ, patched, clear=True):
                with self.assertRaises(rm.ScriptArgError) as cm:
                    rm.script_command([self.bat, "build"])
                self.assertEqual(str(cm.exception), "cmd.exe not found under SystemRoot")

    def test_other_platforms_are_left_as_given(self):
        cmd = [os.path.join(self.tmp, "gradlew"), "%OS%"]
        with mock.patch("kitlib.platform.NAME", "linux"):
            self.assertIs(rm.script_command(cmd), cmd)


@windows_only("a .bat or .cmd and PATHEXT are Windows'")
class RunScript(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)
        patch = mock.patch.object(rm, "WATCH_WINDOWS", False)
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = os.path.join(self.tmp, "proj")
        os.mkdir(self.dir)
        self.out = os.path.join(self.tmp, "out.txt")

    def run_cmd(self, cmd):
        return rm.run_command(cmd, "engine-test", "kit", "resolve", cores=1, store=self.store,
                              env=clean_env())

    def test_an_extensionless_script_runs_its_bat_with_literal_arguments(self):
        make_scripts(self.dir, ["@echo off", '>"%s" echo %%*' % self.out,
                                '>>"%s" echo run=%%MACHINE_LOCK_RUN%%' % self.out, "exit /b 3"])
        inj = os.path.join(self.tmp, "inj.txt")     # what an & read as cmd syntax would create
        self.assertFalse(set(' %"').intersection(inj), inj)
        code = self.run_cmd([os.path.join(self.dir, "gradlew"), "a&type nul>" + inj, "x y"])
        self.assertEqual(code, 3, self.said)
        with open(self.out) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], '"a&type nul>%s" "x y"' % inj)
        self.assertFalse(os.path.exists(inj), "the & ran as cmd syntax")
        self.assertRegex(lines[1], r"^run=r-\d{8}T\d{6}Z-[0-9a-f]{6}:\d+:\d+$")

    def test_the_bat_runs_in_the_runs_job(self):
        # a delay with no network (ping exits 1 where loopback ICMP is blocked)
        make_scripts(self.dir, ['@"%s" -c "import time; time.sleep(2)"' % sys.executable])
        s = rm.book("engine-test", "kit", "resolve", cores=1, store=self.store, env=clean_env())
        with s:
            p = s.start(rm.script_command([os.path.join(self.dir, "gradlew")]),
                        stdout=subprocess.DEVNULL)
            self.assertEqual(s.job.contains(p.pid), "in")
            self.assertEqual(s.wait(p), (0, "done"))

    def test_a_full_bat_path_with_a_percent_is_refused_before_the_run_books(self):
        make_scripts(self.dir, ["@exit /b 0"])
        self.assertEqual(self.run_cmd([os.path.join(self.dir, "gradlew.bat"), "%OS%"]), 1)
        self.assertTrue(any(s.startswith("could not start the command: argument 1 of "
                                         "gradlew.bat holds `%`") for s in self.said), self.said)
        ended = self.store.path("ended")
        self.assertFalse(os.path.isdir(ended) and os.listdir(ended), "the refused run was booked")
        self.assertEqual(self.store.history_records(), [])

    def test_a_bare_bat_in_the_current_folder_takes_its_arguments_literally(self):
        """Red before: CreateProcess found gradlew.bat in the current folder and its own cmd.exe
        start ran the & and expanded the %."""
        make_scripts(self.dir, ["@echo off", '>"%s" echo %%*' % self.out, "exit /b 3"])
        in_cwd(self, self.dir)
        inj = os.path.join(self.tmp, "inj.txt")
        self.assertFalse(set(' %"&').intersection(inj), inj)
        code = self.run_cmd(["gradlew.bat", "a&echo:INJ>" + inj])
        self.assertFalse(os.path.exists(inj), "the & ran as cmd syntax")
        self.assertEqual(code, 3, self.said)
        with open(self.out) as f:
            self.assertEqual(f.read().splitlines()[0], '"a&echo:INJ>%s"' % inj)
        os.remove(self.out)
        del self.said[:]
        self.assertEqual(self.run_cmd(["gradlew.bat", "%OS%"]), 1)
        self.assertEqual(self.said[:1], ["could not start the command: argument 1 of gradlew.bat "
                                         "holds `%`; cmd.exe would expand or split a `%`, `\"` or "
                                         "line break, so a .bat or .cmd cannot take it literally"])
        self.assertFalse(os.path.exists(self.out), "the .bat ran")


@windows_only("a .bat or .cmd and PATHEXT are Windows'")
class CliRefusal(StoreCase):
    """machine_lock.py run and background refuse such an argument before they detach, as they do
    a bare bash (test_cli): a detached run's log would be the only place the refusal showed."""
    flags, call = test_cli.Cli.flags, test_cli.Cli.call

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tool = test_cli.load_tool()

    def setUp(self):
        super().setUp()
        self.hw_path = os.path.join(self.tmp, "hardware.json")
        with open(self.hw_path, "w", encoding="utf-8") as f:
            json.dump(self.hw, f)

    def test_it_is_refused_before_it_detaches_or_books(self):
        make_scripts(self.tmp, ["@exit /b 0"])
        with mock.patch.object(self.tool, "detach", return_value=0) as det:
            for extra in ((), ("--detach",)):
                with self.subTest(extra=extra):
                    code, _, err = self.call("run", "--kind", "engine-test", "--repo", "kit",
                                             "--slug", "t", "--cores", "1", "--no-wait", *extra,
                                             cmd=[os.path.join(self.tmp, "gradlew"), "a%b"])
                    self.assertEqual(code, 1, err)
                    self.assertIn("could not start the command: argument 1 of gradlew.bat "
                                  "holds `%`", err)
        det.assert_not_called()
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")), "a refused run was booked")

    def test_a_background_run_is_refused_before_it_detaches(self):
        """Red before: only run was checked here, so background --detach detached, and the
        refusal showed only in the detached run's log."""
        make_scripts(self.tmp, ["@exit /b 0"])
        with mock.patch.object(self.tool, "detach", return_value=0) as det:
            code, _, err = self.call("background", "--repo", "kit", "--slug", "t", "--detach",
                                     cmd=[os.path.join(self.tmp, "gradlew"), "a%b"])
        self.assertEqual(code, 1, err)
        self.assertIn("could not start the command: argument 1 of gradlew.bat holds `%`", err)
        det.assert_not_called()


if __name__ == "__main__":
    unittest.main()
