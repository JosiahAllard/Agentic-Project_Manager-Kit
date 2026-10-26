"""kitlib.pmstart.consoletasks: the session-start warning for a kit scheduled task whose program is a
console program (python.exe, powershell.exe, ...) rather than pythonw.exe with the carrier
tools/no_window.py (docs/pm-start.md, docs/daily-full.md "The schedule"). Fake task definitions
and fake schtasks output; one read-only `schtasks /Query /XML ONE` (RealRead) is the only read of
this machine's task scheduler, and it skips when schtasks cannot answer."""
import codecs
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from kitlib import pluginsource, pmstart
from kitlib.pmstart import consoletasks as ct
from kitlib.proc import spawn
from tests.home import temp_home
from tests.pmstart import HANDOFF_FAKE, keep_off_launcher_repos
from tests.platforms import windows_only

REAL_READ = ct.read_tasks      # before setUpModule patches it off this machine
NOW = datetime.datetime(2026, 10, 13, 9, 0)
KIT = r"Q:\kit"
PYW = r"C:\Py\pythonw.exe"
CARRIED = '"Q:\\kit\\tools\\no_window.py" python "Q:\\kit\\tools\\daily_full.py" -C "Q:\\repo"'
FAKES = dict(pm_data=lambda: None, handoff=HANDOFF_FAKE, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, repos={},
             watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0})


def setUpModule():
    keep_off_launcher_repos()


def task(path, exe, args=""):
    return {"path": path, "actions": [{"exe": exe, "args": args}]}


def judge(tasks):
    return ct.judge(tasks, roots={KIT})


class Judge(unittest.TestCase):
    def test_a_kit_task_run_by_python_exe_is_flagged(self):
        lines = judge([task(r"\kit-daily-full", r"C:\Py\python.exe",
                            r'"Q:\kit\tools\daily_full.py" -C "Q:\repo"')])
        self.assertEqual(len(lines), 1)
        self.assertIn(r"scheduled task \kit-daily-full starts python.exe, a console program", lines[0])
        self.assertIn("pythonw.exe with the carrier tools\\no_window.py", lines[0])
        self.assertIn('docs/daily-full.md "The schedule"', lines[0])

    def test_a_kit_task_run_by_powershell_is_flagged(self):
        lines = judge([task(r"\agent-temp-sweep", "powershell.exe", "-File x.ps1"),
                       task(r"\a-worktree-sweep", "powershell", "-Command x")])
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(r"\a-worktree-sweep starts powershell.exe", lines[0])
        self.assertIn(r"\agent-temp-sweep starts powershell.exe", lines[1])

    def test_every_console_program_is_flagged(self):
        for exe in ("python", "PYTHON.EXE", "py.exe", "pwsh.exe", r"C:\Windows\System32\cmd.exe",
                    '"C:\\Program Files\\Git\\bin\\bash.exe"', "C:/Py/python.exe"):
            with self.subTest(exe=exe):
                self.assertEqual(len(judge([task(r"\kit-daily-full", exe, "x")])), 1)

    def test_pythonw_with_the_carrier_is_not_flagged(self):
        self.assertEqual(judge([task(r"\kit-daily-full", PYW, CARRIED),
                                task(r"\Claude coordinators after reboot", PYW,
                                     r'"Q:\kit\tools\no_window.py" python "Q:\kit\tools\launch.py"')]), [])

    def test_a_task_that_is_not_the_kits_is_not_flagged(self):
        self.assertEqual(judge([task(r"\SomeoneElsesTask", "powershell.exe", r"-File C:\x\brain.ps1"),
                                task(r"\Microsoft\Windows\X", r"C:\Py\python.exe", r"C:\other\run.py")]), [])

    def test_a_task_naming_a_path_in_the_kit_folder_is_the_kits_and_a_tool_name_elsewhere_is_not(self):
        rows = ((r'"q:/KIT/tools/whatever.py"', True),             # case and slashes do not matter
                (r"Q:\kit\tools\daily_full.py -C x", True),          # unquoted
                (r'-X "Q:\kit"', True),                              # the folder itself
                (r"""-Command "& 'Q:\kit\tools\x.ps1'" """, True),   # PowerShell's call operator
                (r"-NoProfile -Command & 'Q:\kit\tools\x.ps1' -y", True),
                (r"--flag=Q:\kit\x", True),                          # an option's value
                (r'-File:"Q:\kit\tools\x.ps1"', True),
                (r"""-Command "& 'C:\proj\tools\gate.ps1'" """, False),
                (r"--flag=C:\proj\tools\gate.py", False),
                (r'"C:\copy\tools\worktree_remove.py" -C x', False),  # a kit tool's name in a copy
                (r"C:\proj\tools\gate.py --full", False),            # another repository's gate
                (r'"Q:\kit2\tools\gate.py"', False),                 # a folder that only starts like it
                (r"tools\gate.py", False))                           # relative: no folder to be in
        for args, mine in rows:
            with self.subTest(args=args):
                self.assertEqual(len(judge([task(r"\anything", r"C:\Py\python.exe", args)])), int(mine))

    def test_a_program_inside_the_kit_folder_is_the_kits(self):
        self.assertEqual(len(judge([task(r"\x", r"Q:\kit\.venv\Scripts\python.exe", "-V")])), 1)

    def test_every_console_action_of_a_task_is_one_line(self):
        t = {"path": r"\kit-daily-full", "actions": [{"exe": "python.exe", "args": "a"},
                                                     {"exe": PYW, "args": CARRIED},
                                                     {"exe": "cmd.exe", "args": "/c b"}]}
        self.assertEqual(len(judge([t])), 2)

    def test_the_names_the_kit_registers(self):
        from kitlib.launcher import autostart
        from kitlib.machinesetup import agenttemp
        from kitlib.worktree import task as wtask
        for name in (autostart.TASK_NAME, autostart.OLD_TASK_NAME, agenttemp.TASK, wtask.task_name("r")):
            with self.subTest(name=name):
                self.assertTrue(ct.is_kit(task("\\" + name, "python.exe"), roots=set()))

    def test_the_default_kit_is_this_checkout(self):
        self.assertTrue(os.path.isfile(os.path.join(ct.KIT, "tools", "no_window.py")))
        self.assertIn(os.path.normcase(os.path.normpath(ct.KIT)), ct.kit_roots(environ={}))


class KitRoots(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.top = d.name
        self.export = os.path.join(self.top, "0.4.0-main.7")
        self.live = os.path.join(self.top, "live")
        os.makedirs(self.export)
        os.makedirs(self.live)

    def norm(self, p):
        return os.path.normcase(os.path.normpath(p))

    def test_a_checkout_is_its_own_only_root(self):
        self.assertEqual(ct.kit_roots(self.live, environ={}), {self.norm(self.live)})

    def test_an_export_adds_the_folder_of_every_export_and_its_live_checkout(self):
        open(os.path.join(self.export, pluginsource.MARKER), "w").close()
        roots = ct.kit_roots(self.export, environ={pluginsource.LIVE_ROOT_ENV: self.live})
        self.assertEqual(roots, {self.norm(self.export), self.norm(self.top), self.norm(self.live)})
        other = os.path.join(self.top, "0.4.0-main.9", "tools", "daily_full.py")
        self.assertEqual(len(ct.judge([task(r"\x", "python.exe", '"%s"' % other)], roots=roots)), 1)

    def test_an_export_with_no_variable_adds_the_main_checkout_its_marker_records(self):
        """The session start run from the plugin export with no KIT_LIVE_ROOT: a task naming the
        main checkout the export's marker records is the kit's too."""
        main = os.path.join(self.top, "main")
        os.makedirs(os.path.join(main, ".git"))
        os.makedirs(os.path.join(main, "tools"))
        open(os.path.join(main, "kit.json"), "w").close()
        with open(os.path.join(self.export, pluginsource.MARKER), "w", encoding="utf-8") as fh:
            json.dump({"checkout": main.replace("\\", "/")}, fh)
        roots = ct.kit_roots(self.export, environ={})
        self.assertEqual(roots, {self.norm(self.export), self.norm(self.top), self.norm(main)})
        line = '"%s"' % os.path.join(main, "tools", "daily_full.py")
        self.assertEqual(len(ct.judge([task(r"\x", "python.exe", line)], roots=roots)), 1)


XML = """
<Tasks>


<!-- \\kit-daily-full -->

<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\kit-daily-full</URI></RegistrationInfo>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Py\\python.exe</Command>
      <Arguments>"Q:\\kit\\tools\\daily_full.py" -C "Q:\\repo"</Arguments>
      <WorkingDirectory>Q:\\repo</WorkingDirectory>
    </Exec>
  </Actions>
</Task>


<!-- \\Folder\\no-uri -->

<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions Context="Author">
    <ComHandler><ClassId>{0}</ClassId></ComHandler>
    <Exec><Command>notepad.exe</Command></Exec>
  </Actions>
</Task>

</Tasks>
"""


DECLARED = '<?xml version="1.0" encoding="UTF-16"?>'   # what schtasks /Query /XML writes first
KIT_NON_ASCII = "Q:\\K\u00eft"


class FakeRun:
    def __init__(self, stdout=XML.encode("ascii"), code=0, stderr=b"", raises=None):
        self.stdout, self.code, self.stderr, self.raises = stdout, code, stderr, raises
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(args, self.code, self.stdout, self.stderr)


class Read(unittest.TestCase):
    def test_parse_names_and_exec_actions(self):
        self.assertEqual(ct.parse(XML), [
            {"path": r"\kit-daily-full", "actions": [
                {"exe": r"C:\Py\python.exe", "args": r'"Q:\kit\tools\daily_full.py" -C "Q:\repo"'}]},
            {"path": r"\Folder\no-uri", "actions": [{"exe": "notepad.exe", "args": ""}]}])

    def test_read_asks_schtasks_for_the_xml_with_a_timeout(self):
        run = FakeRun()
        self.assertEqual(len(REAL_READ(run=run)), 2)
        (args, kw), = run.calls
        self.assertEqual(args, ["schtasks", "/Query", "/XML", "ONE"])
        self.assertEqual(kw["timeout"], ct.TIMEOUT_S)

    def test_utf16_output_with_its_declaration_goes_to_the_parser_as_bytes(self):
        text = DECLARED + XML.replace("Q:\\kit", KIT_NON_ASCII)
        for raw in (codecs.BOM_UTF16_LE + text.encode("utf-16-le"), text.encode("utf-16-le"),
                    text.encode("utf-16"), codecs.BOM_UTF16_BE + text.encode("utf-16-be")):
            with self.subTest(raw=raw[:4]):
                self.assertIsInstance(ct.xml_input(raw), bytes)
                tasks = REAL_READ(run=FakeRun(stdout=raw))
                self.assertIn(KIT_NON_ASCII + "\\tools\\daily_full.py", tasks[0]["actions"][0]["args"])

    def test_single_byte_output_declaring_utf16_is_read_in_the_console_code_page(self):
        # schtasks /Query /XML writes encoding="UTF-16" and one byte per character to a pipe
        text = DECLARED + XML.replace("Q:\\kit", KIT_NON_ASCII)
        try:
            raw = text.encode("oem")
        except UnicodeEncodeError:
            self.skipTest("the OEM code page has no %r" % KIT_NON_ASCII)
        tasks = REAL_READ(run=FakeRun(stdout=raw))
        self.assertEqual(tasks[0]["actions"][0]["args"],
                         '"%s\\tools\\daily_full.py" -C "Q:\\repo"' % KIT_NON_ASCII)
        lines = ct.judge(tasks, roots={KIT_NON_ASCII})
        self.assertEqual(len(lines), 1)

    def test_single_byte_output_declaring_a_single_byte_encoding_keeps_it(self):
        raw = (b'<?xml version="1.0" encoding="windows-1252"?>'
               + XML.replace("Q:\\kit", KIT_NON_ASCII).encode("cp1252"))
        self.assertIsInstance(ct.xml_input(raw), bytes)
        self.assertIn(KIT_NON_ASCII, REAL_READ(run=FakeRun(stdout=raw))[0]["actions"][0]["args"])

    def test_read_failures_raise(self):
        for run, err in ((FakeRun(code=1, stdout=b"ERROR: Access is denied."), RuntimeError),
                         (FakeRun(raises=subprocess.TimeoutExpired("schtasks", 4)), RuntimeError),
                         (FakeRun(raises=OSError("no schtasks")), OSError),
                         (FakeRun(stdout=b"not xml"), Exception)):
            with self.subTest(err=err, code=run.code, raises=run.raises):
                with self.assertRaises(err):
                    REAL_READ(run=run)


@windows_only("schtasks is Windows'")
class RealRead(unittest.TestCase):
    def test_this_machines_task_list_parses(self):
        """One read-only `schtasks /Query /XML ONE`; skips when schtasks cannot answer in time."""
        seen = []

        def run(*a, **kw):
            seen.append(spawn.run(*a, **kw))
            return seen[-1]
        try:
            tasks = REAL_READ(run=run, timeout=30)
        except (OSError, RuntimeError) as e:
            self.skipTest("schtasks could not answer: %s" % e)
        except ET.ParseError as e:
            # no task at all, or an informational message: nothing of the XML to check
            raw = seen[-1].stdout or b""
            if b"<Task" in raw or b"<\0T\0a\0s\0k\0" in raw:
                raise
            self.skipTest("schtasks gave no task XML: %s" % e)
        self.assertIsInstance(tasks, list)
        for t in tasks:
            self.assertTrue(t["path"])
            for a in t["actions"]:
                self.assertEqual(set(a), {"exe", "args"})


class InTheChecklist(unittest.TestCase):
    def checklist(self, read):
        temp_home(self)     # the hub check's current account reads an empty machine config
        return pmstart.checklist({}, NOW, procs=[], console_tasks={"read": read}, **FAKES)

    def test_a_flagged_task_is_a_checklist_line(self):
        def read():
            return ct.parse(XML.replace("Q:\\kit", ct.KIT))
        txt = self.checklist(read)
        self.assertIn(r"- scheduled task \kit-daily-full starts python.exe, a console program", txt)

    def test_no_line_when_every_kit_task_is_carried(self):
        txt = self.checklist(lambda: [task(r"\kit-daily-full", PYW, CARRIED)])
        self.assertNotIn("scheduled task", txt)

    def test_a_failed_read_is_one_could_not_check_line(self):
        def boom():
            raise RuntimeError("schtasks /Query /XML timed out after 4 s")
        txt = self.checklist(boom)
        self.assertIn("- could not check console_tasks: RuntimeError: schtasks /Query /XML timed out", txt)

    def test_the_tests_keep_the_check_off_this_machine(self):
        self.assertEqual(ct.check({}), [])


class Linux(unittest.TestCase):
    """On Linux (docs/linux-support-plan.md slice 10) a job opens no console: check says nothing
    and never reads; read_tasks is the pm-kit jobs read back, never schtasks."""

    def setUp(self):
        from unittest import mock
        patch = mock.patch("kitlib.platform.NAME", "linux")
        patch.start()
        self.addCleanup(patch.stop)
        self.mock = mock

    def test_check_is_empty_without_reading(self):
        def read():
            raise AssertionError("read on Linux")
        self.assertEqual(ct.check({}, read=read), [])

    def test_read_tasks_is_the_jobs_read_back(self):
        from kitlib.platform.linux import schedule
        jobs = [{"path": "\\pm-kit-x", "name": "x", "actions": []}]
        with self.mock.patch.object(schedule, "read_back", return_value=jobs) as got:
            self.assertEqual(REAL_READ(run="RUN"), jobs)
        got.assert_called_once_with(run="RUN")


if __name__ == "__main__":
    unittest.main()
