"""kitlib.proc.spawn: run, call and popen are subprocess's own with one change, the creationflags
of kitlib.proc.job.no_window_flags; and carry, what tools/no_window.py does. Nothing starts here:
subprocess is faked. The real processes are in tests/proc/test_no_window.py."""
import os
import subprocess
import unittest
from unittest import mock

if os.name == "nt":
    from kitlib.proc import job, spawn, win32

NEW_GROUP = 0x200


@unittest.skipUnless(os.name == "nt", "Windows only")
class FlagsTest(unittest.TestCase):
    """What each wrapper hands subprocess, with and without a console."""

    def started(self, name, console, *args, **kw):
        """(the positional arguments, the keywords) subprocess.<name> got, and what came back."""
        back = object()
        with mock.patch.object(job, "has_console", return_value=console), \
                mock.patch.object(spawn.subprocess, {"popen": "Popen"}.get(name, name),
                                  return_value=back) as fake:
            got = getattr(spawn, name)(*args, **kw)
        self.assertIs(got, back)
        self.assertEqual(fake.call_count, 1)
        return fake.call_args[0], fake.call_args[1]

    def test_no_console_adds_no_window(self):
        for name in ("run", "call", "popen"):
            with self.subTest(name):
                args, kw = self.started(name, False, ["prog", "a"], cwd="d", timeout=3)
                self.assertEqual(args, (["prog", "a"],))
                self.assertEqual(kw, {"cwd": "d", "timeout": 3,
                                      "creationflags": win32.CREATE_NO_WINDOW})

    def test_console_parent_call_is_unchanged(self):
        """Not even a creationflags=0 keyword: the call is the one subprocess got before."""
        for name in ("run", "call", "popen"):
            with self.subTest(name):
                args, kw = self.started(name, True, ["prog"], cwd="d")
                self.assertEqual((args, kw), ((["prog"],), {"cwd": "d"}))

    def test_other_flags_are_kept(self):
        for name in ("run", "call", "popen"):
            with self.subTest(name):
                _, kw = self.started(name, False, ["prog"], creationflags=NEW_GROUP)
                self.assertEqual(kw["creationflags"], NEW_GROUP | win32.CREATE_NO_WINDOW)
                _, kw = self.started(name, True, ["prog"], creationflags=NEW_GROUP)
                self.assertEqual(kw["creationflags"], NEW_GROUP)

    def test_a_callers_console_choice_is_kept(self):
        for name in ("run", "call", "popen"):
            for flag in (win32.CREATE_NEW_CONSOLE, win32.DETACHED_PROCESS,
                         win32.CREATE_NO_WINDOW):
                with self.subTest(name=name, flag=flag):
                    _, kw = self.started(name, False, ["prog"], creationflags=flag)
                    self.assertEqual(kw["creationflags"], flag)

    def test_detached_process_is_never_added(self):
        for name in ("run", "call", "popen"):
            _, kw = self.started(name, False, ["prog"])
            self.assertFalse(kw["creationflags"] & win32.DETACHED_PROCESS)

    def test_the_callers_keywords_are_not_changed_in_place(self):
        mine = {"cwd": "d"}
        with mock.patch.object(job, "has_console", return_value=False), \
                mock.patch.object(spawn.subprocess, "run"):
            spawn.run(["prog"], **mine)
        self.assertEqual(mine, {"cwd": "d"})

    def test_an_error_from_subprocess_comes_through(self):
        with mock.patch.object(job, "has_console", return_value=False), \
                mock.patch.object(spawn.subprocess, "run", side_effect=OSError("no such program")):
            with self.assertRaises(OSError):
                spawn.run(["prog"])


@unittest.skipUnless(os.name == "nt", "Windows only")
class ConsolePythonTest(unittest.TestCase):

    def test_the_python_beside_a_pythonw(self):
        seen = []

        def isfile(p):
            seen.append(p)
            return True
        with mock.patch.object(spawn.os.path, "isfile", side_effect=isfile):
            got = spawn.console_python(os.path.join("C:\\", "py", "pythonw.exe"))
        self.assertEqual(got, os.path.join("C:\\", "py", "python.exe"))
        self.assertEqual(seen, [got])

    def test_none_beside_it(self):
        with mock.patch.object(spawn.os.path, "isfile", return_value=False):
            self.assertIsNone(spawn.console_python(os.path.join("C:\\", "py", "pythonw.exe")))


@unittest.skipUnless(os.name == "nt", "Windows only")
class CarryTest(unittest.TestCase):
    """carry(argv): start the program with no console window, wait, its exit code."""

    def carried(self, argv, console=False, python="C:\\py\\python.exe", **fake):
        with mock.patch.object(job, "has_console", return_value=console), \
                mock.patch.object(spawn, "console_python", return_value=python), \
                mock.patch.object(spawn.subprocess, "call", **fake) as call:
            return spawn.carry(argv), call

    def test_the_exit_code_is_the_programs(self):
        code, call = self.carried(["prog.exe", "a", "b c"], return_value=3)
        self.assertEqual(code, 3)
        self.assertEqual(call.call_args[0], (["prog.exe", "a", "b c"],))
        self.assertEqual(call.call_args[1], {"creationflags": win32.CREATE_NO_WINDOW})

    def test_python_is_the_console_python_beside_this_interpreter(self):
        for word in ("python", "PYTHON", "python.exe"):
            with self.subTest(word):
                _, call = self.carried([word, "tool.py"], return_value=0)
                self.assertEqual(call.call_args[0], (["C:\\py\\python.exe", "tool.py"],))

    def test_python_stays_a_bare_name_when_none_is_beside(self):
        _, call = self.carried(["python", "tool.py"], python=None, return_value=0)
        self.assertEqual(call.call_args[0], (["python", "tool.py"],))

    def test_another_program_is_started_as_named(self):
        _, call = self.carried(["C:\\x\\python.exe", "tool.py"], return_value=0)
        self.assertEqual(call.call_args[0], (["C:\\x\\python.exe", "tool.py"],))

    def test_from_a_console_nothing_is_added(self):
        _, call = self.carried(["prog.exe"], console=True, return_value=0)
        self.assertEqual(call.call_args[1], {})

    def test_no_program_is_a_usage_error_and_starts_nothing(self):
        code, call = self.carried([], return_value=0)
        self.assertEqual(code, spawn.EXIT_USAGE)
        call.assert_not_called()

    def test_a_program_that_cannot_start(self):
        code, _ = self.carried(["nope.exe"], side_effect=OSError("not found"))
        self.assertEqual(code, spawn.EXIT_NO_START)

    def test_the_codes_are_not_a_programs_usual_ones(self):
        self.assertEqual((spawn.EXIT_USAGE, spawn.EXIT_NO_START), (2, 127))

    def test_the_wrappers_use_the_shared_subprocess_module(self):
        """A test that fakes subprocess.run on any kit module still sees the start."""
        self.assertIs(spawn.subprocess, subprocess)


if __name__ == "__main__":
    unittest.main()
