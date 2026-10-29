"""A command whose program is a WindowsApps App Execution Alias (kitlib.lock.run alias_check): what
the alias starts runs outside the job of whoever starts it, so it is not pinned to the run's cores
and a stop does not end it. The lock refuses such a command before the run books (1, no record),
and Slot.start refuses it too; a python name is shown the interpreter to run instead. `status`
warns when the first python on PATH is the alias (path_alias_line), and a stop names a process
descended from the run's command that is outside its job and leaves it running (escaped,
escape_lines). Scratch store and a fake WindowsApps folder only, but for one check of this
machine's own alias."""
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
    from kitlib.proc import process


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb"):
        pass
    return path


class FakeApps:
    """A temporary LOCALAPPDATA with Microsoft\\WindowsApps holding empty alias stand-ins, and an
    app folder standing in for this program's own (CreateProcess's first folder); os.environ's
    LOCALAPPDATA and PATH (WindowsApps only) point at them until the case ends."""

    def fake_apps(self):
        self.base = tempfile.mkdtemp(prefix="lock-alias-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.local = os.path.join(self.base, "Local")
        self.apps = os.path.join(self.local, "Microsoft", "WindowsApps")
        for n in ("python.exe", "python3.exe", "pythonw3.exe", "winget.exe", "bash.exe"):
            touch(os.path.join(self.apps, n))
        self.pkg = touch(os.path.join(self.apps, "PythonSoftwareFoundation.Python.3.12_x",
                                      "python3.12.exe"))
        self.appdir = os.path.join(self.base, "app")
        os.makedirs(self.appdir)
        for patch in (mock.patch.dict(os.environ, LOCALAPPDATA=self.local, PATH=self.apps),
                      mock.patch.object(rm, "_create_process_dirs", return_value=[self.appdir])):
            patch.start()
            self.addCleanup(patch.stop)

    def refused(self, cmd):
        with self.assertRaises(OSError) as cm:
            rm.script_command(cmd)
        self.assertIsInstance(cm.exception, rm.ScriptArgError)
        return str(cm.exception)


@windows_only("App Execution Aliases are Windows'")
class AliasCheck(FakeApps, unittest.TestCase):
    def setUp(self):
        self.fake_apps()

    def test_a_bare_python_name_found_as_the_alias_is_refused_with_the_interpreter_to_run(self):
        """Red before: script_command left python3 as given, and the alias started python outside
        the run's job."""
        msg = self.refused(["python3", "-c", "print(1)"])
        alias = os.path.join(self.apps, "python3.exe")
        self.assertEqual(msg, "python3 is the WindowsApps App Execution Alias %s: what it starts "
                              "runs outside the run's job, on every core, and outlives a stop; "
                              'run "%s" -c print(1) instead' % (alias, sys.executable))

    def test_a_named_alias_path_is_refused_with_or_without_its_exe(self):
        for name in (os.path.join(self.apps, "python3.exe"), os.path.join(self.apps, "python3"),
                     self.pkg):
            with self.subTest(name=name):
                self.assertIn("App Execution Alias", self.refused([name, "-V"]))

    def test_a_program_that_is_not_python_is_told_to_name_its_own_path(self):
        msg = self.refused(["winget", "list"])
        self.assertIn("winget is the WindowsApps App Execution Alias %s"
                      % os.path.join(self.apps, "winget.exe"), msg)
        self.assertTrue(msg.endswith("; name the program's own path instead, not the alias"), msg)

    def test_a_windowed_python_name_is_shown_pythonw_when_it_is_there(self):
        msg = self.refused(["pythonw3", "x.py"])
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        self.assertTrue(msg.endswith('run "%s" x.py instead'
                                     % (w if os.path.isfile(w) else sys.executable)), msg)

    def test_what_createprocess_finds_first_is_left_as_given(self):
        """A python.exe in this program's folder wins over PATH (CreateProcess's order), a
        python3.exe in an earlier PATH folder wins over the alias, and a bare bash is Git Bash's
        business (kitlib.proc.bash), never refused here."""
        touch(os.path.join(self.appdir, "python.exe"))
        cmd = ["python", "-V"]
        self.assertIs(rm.script_command(cmd), cmd)
        early = os.path.join(self.base, "early")
        touch(os.path.join(early, "python3.exe"))
        with mock.patch.dict(os.environ, PATH=os.pathsep.join((early, self.apps))):
            cmd = ["python3", "-V"]
            self.assertIs(rm.script_command(cmd), cmd)
        for cmd in (["bash", "-c", "true"], ["BASH.EXE", "-c", "true"]):
            self.assertIs(rm.script_command(cmd), cmd)

    def test_no_localappdata_or_another_platform_is_left_as_given(self):
        cmd = ["python3", "-V"]
        with mock.patch.dict(os.environ):
            del os.environ["LOCALAPPDATA"]
            self.assertIs(rm.script_command(cmd), cmd)
        with mock.patch("kitlib.platform.NAME", "linux"):
            self.assertIs(rm.script_command(cmd), cmd)


@windows_only("App Execution Aliases are Windows'")
class ThisMachine(unittest.TestCase):
    def test_this_machines_python3_alias_is_refused(self):
        alias = os.path.join(os.environ.get("LOCALAPPDATA") or "?", "Microsoft", "WindowsApps",
                             "python3.exe")
        here = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "python3.exe")
        if not os.path.isfile(alias) or os.path.isfile(here) or os.path.isfile("python3.exe"):
            self.skipTest("no python3 alias here, or a python3.exe CreateProcess finds first")
        with mock.patch.dict(os.environ, PATH=os.path.dirname(alias)):
            with self.assertRaises(OSError) as cm:
                rm.script_command(["python3", "-V"])
        self.assertIn("App Execution Alias", str(cm.exception))


@windows_only("App Execution Aliases are Windows'")
class PathWarning(FakeApps, unittest.TestCase):
    def setUp(self):
        self.fake_apps()

    def test_the_alias_first_on_path_is_warned_with_the_folder_to_put_ahead(self):
        self.assertEqual(rm.path_alias_line(), (
            "python: first on PATH is the WindowsApps alias %s; a non-python child's python "
            "escapes the run's job; put %s ahead of it on the user PATH"
            % (os.path.join(self.apps, "python.exe"),
               os.path.dirname(os.path.abspath(sys.executable)))))

    def test_a_real_python_ahead_of_it_or_no_alias_is_no_warning(self):
        real = os.path.join(self.base, "real")
        touch(os.path.join(real, "python.exe"))
        for path in (os.pathsep.join((real, self.apps)), real, ""):
            with self.subTest(path=path), mock.patch.dict(os.environ, PATH=path):
                self.assertIsNone(rm.path_alias_line())
        with mock.patch("kitlib.platform.NAME", "linux"):
            self.assertIsNone(rm.path_alias_line())

    def test_an_interpreter_that_is_itself_the_alias_is_not_named_as_the_fix(self):
        with mock.patch.object(sys, "executable", os.path.join(self.apps, "python.exe")):
            line = rm.path_alias_line()
        self.assertTrue(line.endswith("put the real interpreter's folder ahead of it on the user "
                                      "PATH"), line)


@windows_only("App Execution Aliases are Windows'")
class RunRefusal(FakeApps, StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.fake_apps()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)
        patch = mock.patch.object(rm, "WATCH_WINDOWS", False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_run_command_refuses_it_before_the_run_books(self):
        code = rm.run_command(["python3", "-c", "print(1)"], "engine-test", "kit", "alias",
                              cores=1, store=self.store, env=clean_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(s.startswith("could not start the command: python3 is the WindowsApps "
                                         "App Execution Alias") for s in self.said), self.said)
        ended = self.store.path("ended")
        self.assertFalse(os.path.isdir(ended) and os.listdir(ended), "the refused run was booked")
        self.assertEqual(self.store.history_records(), [])

    def test_slot_start_refuses_it_too(self):
        """Red before: Slot.start handed the alias to CreateProcess (here an empty file, so
        WinError 193; a real alias would start its program outside the job)."""
        s = rm.book("engine-test", "kit", "alias", cores=1, store=self.store, env=clean_env())
        with s:
            for cmd in (["python3", "-V"], [os.path.join(self.apps, "winget.exe")]):
                with self.subTest(cmd=cmd), self.assertRaises(OSError) as cm:
                    s.start(cmd)
                self.assertIn("App Execution Alias", str(cm.exception))


@windows_only("App Execution Aliases are Windows'")
class CliRefusal(FakeApps, StoreCase):
    """machine_lock.py run and background refuse an alias before they detach, and status warns
    when python on PATH is one."""
    flags, call = test_cli.Cli.flags, test_cli.Cli.call

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tool = test_cli.load_tool()

    def setUp(self):
        StoreCase.setUp(self)
        self.fake_apps()
        self.hw_path = os.path.join(self.tmp, "hardware.json")
        with open(self.hw_path, "w", encoding="utf-8") as f:
            json.dump(self.hw, f)

    def test_run_and_background_refuse_it_before_they_detach(self):
        alias = os.path.join(self.apps, "python3.exe")
        with mock.patch.object(self.tool, "detach", return_value=0) as det:
            for verb, extra in (("run", ()), ("run", ("--detach",)), ("background", ("--detach",))):
                with self.subTest(verb=verb, extra=extra):
                    args = ((verb, "--kind", "engine-test", "--cores", "1", "--no-wait")
                            if verb == "run" else (verb,))
                    code, _, err = self.call(*args, "--repo", "kit", "--slug", "t", *extra,
                                             cmd=[alias, "-V"])
                    self.assertEqual(code, 1, err)
                    self.assertIn("App Execution Alias %s" % alias, err)
        det.assert_not_called()
        self.assertFalse(os.path.isdir(self.store.path("ended"))
                         and os.listdir(self.store.path("ended")), "a refused run was booked")

    def test_status_warns_when_python_on_path_is_the_alias(self):
        """Red before: status said nothing of it."""
        code, out, err = self.call("status")
        self.assertEqual(code, 0, err)
        self.assertIn("python: first on PATH is the WindowsApps alias %s"
                      % os.path.join(self.apps, "python.exe"), out + err)


@windows_only("the run's job is Windows'")
class StopEscape(StoreCase):
    """A stop names each live process descended from the run's command (by the parent pid chain)
    that is outside the run's job, and never ends it."""
    SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]

    def setUp(self):
        super().setUp()
        self.said = []
        self.store.say = self.said.append
        self.confine(1)
        patch = mock.patch.object(rm, "WATCH_WINDOWS", False)
        patch.start()
        self.addCleanup(patch.stop)

    def stop(self, s, p, snap=None):
        rm.request_stop(self.store, s.id, "test")
        with mock.patch.object(process, "snapshot", snap or process.snapshot):
            code, how = s.wait(p)
        s.release(code, how)
        self.assertEqual((code, how), (1, "cancelled"))

    def test_stop_names_a_descendant_outside_the_job_and_leaves_it_running(self):
        """Red before: stop said only that the tree was ended. The outsider is a real process
        outside the run's job, made a child of the run's command in the snapshot (what an alias's
        program looks like)."""
        s = rm.book("engine-test", "kit", "esc", cores=1, store=self.store, env=clean_env())
        p = s.start(self.SLEEP)
        out = subprocess.Popen(self.SLEEP)
        self.addCleanup(out.wait)
        self.addCleanup(out.kill)
        self.assertEqual(s.job.contains(out.pid), "out")
        real = process.snapshot

        def snap():
            return [(pid, p.pid if pid == out.pid else pp, n) for pid, pp, n in real()]
        self.stop(s, p, snap)
        lines = [x for x in self.said if "pid %d " % out.pid in x]
        self.assertEqual(len(lines), 1, self.said)
        self.assertIn("outside the run's job", lines[0])
        self.assertIn("stop did not end it", lines[0])
        self.assertIn("parent pid", lines[0])
        self.assertIsNone(out.poll(), "a process outside the job was ended")

    def test_a_stop_with_nothing_outside_the_job_says_nothing_of_it(self):
        s = rm.book("engine-test", "kit", "esc", cores=1, store=self.store, env=clean_env())
        p = s.start(self.SLEEP)
        self.stop(s, p)
        self.assertFalse([x for x in self.said if "outside the run's job" in x], self.said)


if __name__ == "__main__":
    unittest.main()
