"""The kit-automatic-tasks scheduled task, a machine-setup extra (kitlib.machinesetup.extras
AUTO_TASK): its line and argument, and the step through M.run with the fake host of
tests.machinesetup.test_machinesetup. Nothing here reads or writes the real task scheduler: the
task list and the registration are the fake host's, and the kit's place is patched to a main
checkout."""
import os
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.git import repo as gitrepo
from kitlib.machinesetup import extras as E
from tests.machinesetup import test_machinesetup as MS

PYW = 'C:\\Py\\pythonw.exe'


class Line(unittest.TestCase):
    def test_the_argument_is_the_carrier_python_and_the_tick(self):
        self.assertEqual(E.auto_argument('C:\\kit'),
                         '"C:\\kit\\tools\\no_window.py" python "C:\\kit\\tools\\auto_tasks.py" tick')

    def test_every_15_minutes_as_the_user_limited_with_pythonw_and_force(self):
        line = E.auto_task_line('C:\\kit', pythonw=PYW)
        self.assertTrue(line.startswith("$py = 'C:\\Py\\pythonw.exe'; "), line)
        for part in ("Register-ScheduledTask -Force -TaskName 'kit-automatic-tasks'",
                     '-RepetitionInterval (New-TimeSpan -Minutes 15)',
                     '-User $env:USERNAME -RunLevel Limited',
                     '-MultipleInstances IgnoreNew',
                     "-Execute $py -Argument '\"C:\\kit\\tools\\no_window.py\" python "
                     "\"C:\\kit\\tools\\auto_tasks.py\" tick'",
                     "-WorkingDirectory 'C:\\kit'",
                     "-match '\\\\WindowsApps\\\\'", 'Test-Path -LiteralPath $py'):
            self.assertIn(part, line)
        self.assertNotIn('Highest', line)

    def test_same_compares_program_and_argument(self):
        arg = E.auto_argument('C:\\kit')
        task = {'path': '\\kit-automatic-tasks', 'actions': [{'exe': '"c:\\py\\PYTHONW.exe"', 'args': arg}]}
        self.assertTrue(E.auto_task_same(task, PYW, arg))
        self.assertFalse(E.auto_task_same(task, PYW, E.auto_argument('C:\\other')))
        self.assertFalse(E.auto_task_same(task, 'C:\\Other\\pythonw.exe', arg))
        self.assertFalse(E.auto_task_same({'actions': []}, PYW, arg))


class Step(MS.Base):
    """M.run in the old-kit switch against FakeHost, the kit taken as a main checkout."""

    def setUp(self):
        MS.Base.setUp(self)
        MS.touch(os.path.join(self.kit, 'tools', 'auto_tasks.py'))
        self.regs, self.tasks = [], []
        self.host.task_register = lambda line: self.regs.append(line)
        self.host.task_list = lambda: list(self.tasks)
        self.place = gitrepo.Place(gitrepo.MAIN, self.kit, '', None, None)
        for target, value in (('kitlib.machinesetup.worktreesweep.tool_root', lambda kit, environ=None: self.kit),
                              ('kitlib.git.repo.classify', lambda path, *a, **k: self.place),
                              ('kitlib.launcher.pythonw.base_pythonw', lambda base_prefix=None: PYW)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def go(self, apply=False, **kw):
        kw.setdefault('agent_temp', os.path.join(self.root, 'new-agent-temp'))
        kw.setdefault('sweep_task', False)
        ctx = M.Context(self.home, self.kit, self.old, desktop=self.desk, host=self.host, today=MS.TODAY, **kw)
        code, lines = M.run(ctx, apply=apply)
        return code, '\n'.join(lines)

    def mine(self):
        return [r for r in self.regs if 'kit-automatic-tasks' in r]

    def test_the_dry_run_plans_the_task_and_registers_nothing(self):
        code, out = self.go()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('register the scheduled task kit-automatic-tasks (every 15 min, as you, not elevated, '
                      'no window', out)
        self.assertIn(E.auto_task_line(self.kit, PYW), out)
        self.assertEqual(self.regs, [])

    def test_apply_registers_once(self):
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.mine()), 1, out)
        self.assertEqual(self.mine()[0], E.auto_task_line(self.kit, PYW))

    def test_a_task_already_as_planned_is_left_alone(self):
        self.tasks = [{'path': '\\kit-automatic-tasks',
                       'actions': [{'exe': PYW, 'args': E.auto_argument(self.kit)}]}]
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.mine(), [])
        self.assertNotIn('kit-automatic-tasks', out)

    def test_a_task_running_something_else_is_updated(self):
        self.tasks = [{'path': '\\kit-automatic-tasks', 'actions': [{'exe': PYW, 'args': '"C:\\old\\x.py"'}]}]
        code, out = self.go(apply=True)
        self.assertIn('update the scheduled task kit-automatic-tasks', out)
        self.assertEqual(len(self.mine()), 1)

    def test_a_kit_that_is_no_main_checkout_is_a_note(self):
        self.place = gitrepo.Place(gitrepo.LINKED, self.kit, 'a linked worktree', None, None)
        code, out = self.go(apply=True)
        self.assertEqual(self.mine(), [])
        self.assertIn('kit-automatic-tasks task: the kit at %s is not a main checkout' % self.kit, out)

    def test_no_tick_tool_is_a_note(self):
        os.remove(os.path.join(self.kit, 'tools', 'auto_tasks.py'))
        code, out = self.go(apply=True)
        self.assertEqual(self.mine(), [])
        self.assertIn('kit-automatic-tasks task: no %s' % os.path.join(self.kit, 'tools', 'auto_tasks.py'), out)

    def test_an_unreadable_task_list_is_a_note(self):
        def broken():
            raise RuntimeError('schtasks broke')
        self.host.task_list = broken
        code, out = self.go(apply=True)
        self.assertEqual(self.mine(), [])
        self.assertIn('could not read the scheduled tasks (schtasks broke)', out)


if __name__ == '__main__':
    unittest.main()
