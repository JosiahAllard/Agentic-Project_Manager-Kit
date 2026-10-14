"""kitlib.worktree.task (tools/worktree_sweep_task.py): the registration line, the owner's guard
and where the task may point. PowerShell is a fake runner: nothing is ever registered here.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from kitlib.git import defaults as gitdefaults
from kitlib.worktree import task
from tests import gitfixture
from tests.worktree.test_worktree_remove import KIT, RemoveCase

ENTRY = os.path.join(KIT, 'tools', 'worktree_sweep_task.py')


class Runner(object):
    def __init__(self, rc=0):
        self.rc, self.calls = rc, []

    def __call__(self, args):
        self.calls.append(args)
        return self.rc


class TaskCase(RemoveCase):

    def setUp(self):
        super().setUp()
        # The kit the task points at: its own main checkout with a tools/worktree_remove.py.
        self.kit = gitfixture.TempRepo()
        self.addCleanup(self.kit.cleanup)
        self.tool = self.kit.write(os.path.join('tools', 'worktree_remove.py'), '# tool\n')
        # The task's program: a stand-in pythonw.exe in a base install of its own.
        self.pythonw = os.path.join(self.fx.base, "py'base", 'pythonw.exe')
        os.makedirs(os.path.dirname(self.pythonw))
        with open(self.pythonw, 'w', encoding='utf-8') as f:
            f.write('')

    def run_task(self, *argv, runner=None, root=None, tool=None, pythonw=None):
        runner = runner or Runner()
        got = task.run(list(argv), runner, root or self.repo, tool or self.tool,
                       pythonw=pythonw or self.pythonw)
        return got.code, '\n'.join(got.lines), runner


class Task(TaskCase):

    def test_print_only_registers_nothing(self):
        rc, out, runner = self.run_task()
        self.assertEqual(rc, 0, out)
        self.assertEqual(runner.calls, [])
        # no repo.name: the name read from the remote's URL (docs/kit-json.md "With no file")
        self.assertIn("Register-ScheduledTask -TaskName '%s-worktree-sweep'"
                      % gitdefaults.repo_name(self.repo), out)
        self.assertIn('-C "%s" --sweep-merged --apply' % self.repo, out)
        self.assertIn('"%s"' % os.path.normpath(self.tool), out)
        self.assertIn(task.NOT_THE_STORE_ALIAS, out)
        self.assertIn('-At 03:00', out)
        self.assertIn('printed   nothing was registered', out)

    def test_the_task_starts_no_console_window(self):
        """The task runs pythonw (no console of its own) on the carrier beside the sweep tool,
        which starts the console python on the sweep with no window."""
        rc, out, _ = self.run_task()
        self.assertEqual(rc, 0, out)
        line = out.splitlines()[0]
        self.assertTrue(line.startswith('$py = %s; ' % task.quoted(self.pythonw)), line)
        tool = os.path.normpath(self.tool)
        carrier = os.path.join(os.path.dirname(tool), task.CARRIER)
        self.assertIn("-Execute $py -Argument '\"%s\" python \"%s\" -C \"%s\" --sweep-merged --apply'"
                      % (carrier, tool, os.path.normpath(self.repo)), line)

    def test_the_pythonw_is_never_a_path_lookup(self):
        """`Get-Command pythonw` gives the WindowsApps alias when that is first on PATH: the line
        names the base install's pythonw itself, and by default that is this interpreter's."""
        rc, out, _ = self.run_task()
        self.assertEqual(rc, 0, out)
        self.assertNotIn('Get-Command', out)
        got = task.run([], Runner(), self.repo, self.tool)
        line = got.lines[0]
        self.assertTrue(line.startswith('$py = %s; '
                                        % task.quoted(os.path.join(sys.base_prefix, 'pythonw.exe'))), line)

    def test_a_windows_apps_pythonw_is_refused_before_anything_is_printed(self):
        alias = os.path.join(self.fx.base, 'Microsoft', 'WindowsApps', 'pythonw.exe')
        os.makedirs(os.path.dirname(alias))
        with open(alias, 'w', encoding='utf-8') as f:
            f.write('')
        for argv in ([], ['--register', '--owner-confirmed']):
            rc, out, runner = self.run_task(*argv, pythonw=alias)
            self.assertEqual(rc, 1, out)
            self.assertEqual(runner.calls, [])
            self.assertIn('WindowsApps', out)
            self.assertIn('nothing was registered', out)
            self.assertNotIn('Register-ScheduledTask', out)

    def test_a_missing_pythonw_is_refused(self):
        rc, out, runner = self.run_task('--register', '--owner-confirmed',
                                        pythonw=os.path.join(self.fx.base, 'none', 'pythonw.exe'))
        self.assertEqual(rc, 1, out)
        self.assertEqual(runner.calls, [])
        self.assertIn('refused   no pythonw.exe at', out)

    def test_the_carrier_is_a_tool_of_this_kit(self):
        self.assertEqual(task.CARRIER, 'no_window.py')
        self.assertTrue(os.path.isfile(os.path.join(KIT, 'tools', task.CARRIER)))

    def test_bare_register_is_refused_first(self):
        rc, out, runner = self.run_task('--register', root=os.path.join(self.fx.base, 'nowhere'))
        self.assertEqual(rc, 1, out)
        self.assertIn("refused   --register is the owner's act", out)
        self.assertEqual(runner.calls, [])

    def test_owner_confirmed_runs_powershell_once(self):
        rc, out, runner = self.run_task('--register', '--owner-confirmed')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][:4], ['powershell', '-NoProfile', '-NonInteractive',
                                               '-Command'])
        self.assertIn('registered  powershell exited 0', out)

    def test_a_powershell_failure_is_exit_1(self):
        rc, out, _ = self.run_task('--register', '--owner-confirmed', runner=Runner(1))
        self.assertEqual(rc, 1, out)
        self.assertIn('failed     powershell exited 1', out)

    def test_usage(self):
        for argv in (['--owner-confirmed'], ['--register', '--register'], ['-C'], ['--repo', 'x'],
                     ['-C', 'a', '-C', 'b'], ['extra']):
            rc, out, runner = self.run_task(*argv)
            self.assertEqual(rc, task.USAGE, argv)
            self.assertEqual(runner.calls, [])

    def test_a_linked_worktree_is_not_the_main_checkout(self):
        path = self.workstream('wt', commits=0)
        rc, out, _ = self.run_task('-C', path)
        self.assertEqual(rc, 1, out)
        self.assertIn('is not the main checkout', out)

    def test_a_tool_in_a_branch_worktree_is_refused(self):
        other = os.path.realpath(self.kit.worktree('copy'))
        tool = os.path.join(other, 'tools', 'worktree_remove.py')
        os.makedirs(os.path.dirname(tool), exist_ok=True)
        with open(tool, 'w', encoding='utf-8') as f:
            f.write('# a branch copy\n')
        rc, out, _ = self.run_task(tool=tool)
        self.assertEqual(rc, 1, out)
        self.assertIn('is not in a main checkout', out)

    def test_a_missing_tool_is_refused(self):
        rc, out, _ = self.run_task(tool=os.path.join(self.fx.base, 'none.py'))
        self.assertEqual(rc, 1, out)
        self.assertIn('the sweep tool is not at', out)

    def test_the_configured_name_is_used(self):
        self.configure({'repo': {'name': "it's"}})
        rc, out, _ = self.run_task()
        self.assertEqual(rc, 0, out)
        self.assertIn("-TaskName 'it''s-worktree-sweep'", out)


class Quoting(unittest.TestCase):

    def test_every_single_quote_is_doubled(self):
        for q in task.SINGLE_QUOTES:
            self.assertEqual(task.quoted('a%sb' % q), "'a%s%sb'" % (q, q))


class Entry(TaskCase):

    def test_the_entry_point_prints_usage_to_stderr(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('worktree_sweep_task_entry', ENTRY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(['--owner-confirmed'], runner=mock.Mock(), root=self.repo, tool=self.tool)
        self.assertEqual(rc, 2)
        self.assertIn('usage:', err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        runner = Runner()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main([], runner=runner, root=self.repo, tool=self.tool)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('Register-ScheduledTask', out.getvalue())
        self.assertEqual(runner.calls, [])


if __name__ == '__main__':
    unittest.main()
