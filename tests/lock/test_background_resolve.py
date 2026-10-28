"""`machine_lock.py background -- ./gradlew` on Windows: run_background starts its command as a
foreground run does (kitlib.lock.run script_command), so a name with no extension resolves to its
.bat or .cmd, and a .bat or .cmd, resolved or named, starts through the cmd.exe line the kit quotes
itself: an `&` in an argument stays literal. An argument cmd.exe would expand or split (`%`, `"`, a
line break) is refused before the run's id is claimed: 1, nothing written. Scratch store only."""
import os
import shutil
import unittest

from tests.platforms import windows_only

from .helpers import StoreCase, clean_env
from .test_run import store_files
from .test_run_resolve import make_scripts

if os.name == "nt":
    from kitlib.lock import background as bg


@windows_only("a .bat or .cmd and PATHEXT are Windows'")
class BackgroundScript(StoreCase):
    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(self.layout()["medium_cores"])
        self.dir = os.path.join(self.tmp, "proj")
        os.mkdir(self.dir)
        self.out = os.path.join(self.tmp, "out.txt")
        self.inj = os.path.join(self.tmp, "inj.txt")    # what an & read as cmd syntax creates
        self.assertFalse(set(' %"&').intersection(self.inj), self.inj)
        make_scripts(self.dir, ["@echo off", '>"%s" echo %%*' % self.out,
                                '>>"%s" echo bg=%%MACHINE_LOCK_BG%%' % self.out, "exit /b 3"])

    def run_bg(self, cmd):
        return bg.run_background(cmd, "kit", "bg", store=self.store, env=clean_env())

    def lines(self):
        with open(self.out) as f:
            return f.read().splitlines()

    def test_an_extensionless_script_runs_its_bat(self):
        """Red before: the raw argv went to CreateProcess, WinError 193 for the shell script."""
        code = self.run_bg([os.path.join(self.dir, "gradlew"), "x y", "b"])
        self.assertEqual(code, 3, self.said)
        lines = self.lines()
        self.assertEqual(lines[0], '"x y" b')
        self.assertRegex(lines[1], r"^bg=r-\d{8}T\d{6}Z-[0-9a-f]{6}:\d+:\d+$")
        self.assertEqual(self.store.read("ended/%s.json" % lines[1][3:].split(":")[0])["cmd"],
                         [os.path.join(self.dir, "gradlew"), "x y", "b"])

    def test_an_ampersand_argument_to_a_bat_or_cmd_stays_literal(self):
        """Red before: CreateProcess's own start of a .bat left `a&echo:INJ>f` unquoted, so the
        implicit cmd.exe ran the echo and made the file."""
        shutil.copy(os.path.join(self.dir, "gradlew.bat"), os.path.join(self.dir, "gradlew.cmd"))
        for name in ("gradlew.bat", "gradlew.cmd"):
            with self.subTest(name=name):
                inj = self.inj + "." + name
                arg = "a&echo:INJ>" + inj
                code = self.run_bg([os.path.join(self.dir, name), arg])
                self.assertFalse(os.path.exists(inj), "the & ran as cmd syntax")
                self.assertEqual(code, 3, self.said)
                self.assertEqual(self.lines()[0], '"%s"' % arg)

    def test_an_argument_cmd_would_expand_or_split_is_refused_before_anything_is_written(self):
        """Red before: the .bat ran with the argument (exit 3), its run booked and ended."""
        bat = os.path.join(self.dir, "gradlew.bat")
        for arg, shown in (("%OS%", "`%`"), ('a"b', '`"`'), ("a\nb", "a line break")):
            with self.subTest(arg=arg):
                before = store_files(self.root)
                del self.said[:]
                self.assertEqual(self.run_bg([bat, "ok", arg]), 1, self.said)
                self.assertEqual(self.said, ["could not start the command: argument 2 of "
                                             "gradlew.bat holds %s; cmd.exe would expand or split "
                                             "a `%%`, `\"` or line break, so a .bat or .cmd cannot "
                                             "take it literally" % shown])
                self.assertFalse(os.path.exists(self.out), "the .bat ran")
                self.assertEqual(store_files(self.root), before)


if __name__ == "__main__":
    unittest.main()
