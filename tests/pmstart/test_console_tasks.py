"""kitlib.pmstart.consoletasks: the session-start warning for a kit scheduled task whose program is a
console program (python.exe, powershell.exe, ...) rather than pythonw.exe with the carrier
tools/no_window.py (docs/pm-start.md, docs/daily-full.md "The schedule"). Fake task definitions
and fake schtasks output: nothing reads this machine's task scheduler."""
import datetime
import os
import subprocess
import unittest

from kitlib import pmstart
from kitlib.pmstart import consoletasks as ct
from tests.home import temp_home
from tests.pmstart import keep_off_launcher_repos

REAL_READ = ct.read_tasks      # before setUpModule patches it off this machine
NOW = datetime.datetime(2026, 10, 13, 9, 0)
KIT = r"Z:\kit"
TOOLS = {"daily_full.py", "worktree_remove.py", "launch.py", "no_window.py"}
PYW = r"C:\Py\pythonw.exe"
CARRIED = '"Z:\\kit\\tools\\no_window.py" python "Z:\\kit\\tools\\daily_full.py" -C "Z:\\repo"'
FAKES = dict(pm_data=lambda: None, handoff={"globber": lambda p: []}, reminders={"read": lambda p: "[]"},
             liveness={"read": lambda p: "{}"}, stubs={"repos": {}},
             watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0})


def setUpModule():
    keep_off_launcher_repos()


def task(path, exe, args=""):
    return {"path": path, "actions": [{"exe": exe, "args": args}]}


def judge(tasks):
    return ct.judge(tasks, kit=KIT, tools=TOOLS)


class Judge(unittest.TestCase):
    def test_a_kit_task_run_by_python_exe_is_flagged(self):
        lines = judge([task(r"\kit-daily-full", r"C:\Py\python.exe",
                            r'"Z:\kit\tools\daily_full.py" -C "Z:\repo"')])
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
                                     r'"Z:\kit\tools\no_window.py" python "Z:\kit\tools\launch.py"')]), [])

    def test_a_task_that_is_not_the_kits_is_not_flagged(self):
        self.assertEqual(judge([task(r"\SomeoneElsesTask", "powershell.exe", r"-File C:\x\brain.ps1"),
                                task(r"\Microsoft\Windows\X", r"C:\Py\python.exe", r"C:\other\run.py")]), [])

    def test_a_task_naming_the_kit_folder_or_a_kit_tool_is_the_kits(self):
        lines = judge([task(r"\anything", r"C:\Py\python.exe", r'"z:/KIT/tools/whatever.py"'),
                       task(r"\other", r"C:\Py\python.exe", r'"C:\copy\tools\worktree_remove.py" -C x')])
        self.assertEqual(len(lines), 2, lines)

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
                self.assertTrue(ct.is_kit(task("\\" + name, "python.exe"), kit=KIT, tools=set()))

    def test_the_default_kit_is_this_checkout_and_its_tools(self):
        self.assertTrue(os.path.isfile(os.path.join(ct.KIT, "tools", "no_window.py")))
        self.assertIn("no_window.py", ct.kit_tools())


XML = """
<Tasks>


<!-- \\kit-daily-full -->

<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\kit-daily-full</URI></RegistrationInfo>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Py\\python.exe</Command>
      <Arguments>"Z:\\kit\\tools\\daily_full.py" -C "Z:\\repo"</Arguments>
      <WorkingDirectory>Z:\\repo</WorkingDirectory>
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
                {"exe": r"C:\Py\python.exe", "args": r'"Z:\kit\tools\daily_full.py" -C "Z:\repo"'}]},
            {"path": r"\Folder\no-uri", "actions": [{"exe": "notepad.exe", "args": ""}]}])

    def test_read_asks_schtasks_for_the_xml_with_a_timeout(self):
        run = FakeRun()
        self.assertEqual(len(REAL_READ(run=run)), 2)
        (args, kw), = run.calls
        self.assertEqual(args, ["schtasks", "/Query", "/XML", "ONE"])
        self.assertEqual(kw["timeout"], ct.TIMEOUT_S)

    def test_read_failures_raise(self):
        for run, err in ((FakeRun(code=1, stdout=b"ERROR: Access is denied."), RuntimeError),
                         (FakeRun(raises=subprocess.TimeoutExpired("schtasks", 4)), RuntimeError),
                         (FakeRun(raises=OSError("no schtasks")), OSError),
                         (FakeRun(stdout=b"not xml"), Exception)):
            with self.subTest(err=err, code=run.code, raises=run.raises):
                with self.assertRaises(err):
                    REAL_READ(run=run)


class InTheChecklist(unittest.TestCase):
    def checklist(self, read):
        temp_home(self)     # the hub check's current account reads an empty machine config
        return pmstart.checklist({}, NOW, procs=[], console_tasks={"read": read}, **FAKES)

    def test_a_flagged_task_is_a_checklist_line(self):
        def read():
            return ct.parse(XML.replace("Z:\\kit", ct.KIT))
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


if __name__ == "__main__":
    unittest.main()
