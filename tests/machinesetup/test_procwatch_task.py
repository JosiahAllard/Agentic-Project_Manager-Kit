"""kitlib.machinesetup.procwatch through machine setup's extras (docs/machine-setup.md "Extras",
the procwatch task): the task line, and the dry run against apply, with the fake host of
test_machinesetup. Nothing here reads or writes the real task scheduler.
"""
import contextlib
import importlib.util
import io
import os
import sys
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import extras, procwatch as P
from tests.machinesetup import test_machinesetup as T


def load_procwatch_script():
    """optional/procwatch/procwatch.py as a module (its folder on sys.path for `import winproc`,
    which only main() imports)."""
    path = os.path.join(T.KIT, P.SCRIPT)
    spec = importlib.util.spec_from_file_location('procwatch_script_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TaskLine(unittest.TestCase):
    PYW = 'C:\\Py\\pythonw.exe'

    def line(self, kill=False, kit='C:\\kit', log='D:\\kit-temp\\procwatch.log'):
        return P.task_line(kit, log, kill, pythonw=self.PYW)

    def test_the_program_is_the_base_pythonw_and_the_carrier_comes_first(self):
        line = self.line()
        self.assertTrue(line.startswith("$py = 'C:\\Py\\pythonw.exe'; "), line)
        self.assertNotIn('Get-Command', line)
        self.assertIn("-Execute $py -Argument '\"C:\\kit\\tools\\no_window.py\" python "
                      "\"C:\\kit\\optional\\procwatch\\procwatch.py\" --log \"D:\\kit-temp\\procwatch.log\"'", line)
        self.assertTrue(os.path.isfile(os.path.join(T.KIT, 'tools', 'no_window.py')))

    def test_a_store_alias_or_a_missing_file_throws_in_powershell(self):
        alias = 'C:\\Store\\WindowsApps\\pythonw.exe'
        line = P.task_line('C:\\kit', 'D:\\l.log', pythonw=alias)
        self.assertIn("-match '\\\\WindowsApps\\\\'", line)
        self.assertIn('Test-Path -LiteralPath $py', line)

    def test_the_default_pythonw_is_the_base_install_literal(self):
        from kitlib.launcher import pythonw as W
        line = P.task_line('C:\\kit', 'D:\\l.log')
        self.assertIn("$py = '%s'" % W.base_pythonw().replace("'", "''"), line)

    def test_the_target_is_the_kit_not_a_home_junction_and_the_log_is_not_in_the_home(self):
        line = self.line()
        self.assertNotIn('.claude', line)
        self.assertNotIn('\\tools\\procwatch', line)
        self.assertIn('C:\\kit\\optional\\procwatch\\procwatch.py', line)
        self.assertIn('--log \"D:\\kit-temp\\procwatch.log\"', line)

    def test_no_kill_unless_asked(self):
        self.assertNotIn('--kill', self.line())
        self.assertNotIn('--only', self.line())
        self.assertIn(' --kill --only %s\'' % ','.join(P.SAFE), self.line(kill=True))

    def test_every_15_minutes_as_the_user_not_elevated_and_replacing_an_old_task(self):
        line = self.line()
        self.assertIn("-TaskName 'Claude procwatch'", line)
        self.assertIn('Register-ScheduledTask -Force', line)
        self.assertIn('-RepetitionInterval (New-TimeSpan -Minutes 15)', line)
        self.assertIn('-RunLevel Limited', line)
        self.assertIn('-User $env:USERNAME', line)

    def test_a_kit_path_with_a_quote_and_a_space_is_quoted(self):
        line = self.line(kit="C:\\it's a kit")
        self.assertIn("-WorkingDirectory 'C:\\it''s a kit'", line)
        self.assertIn("'\"C:\\it''s a kit\\tools\\no_window.py\" python", line)

    def test_safe_is_the_kill_allowlisted_rules_of_the_script(self):
        script = load_procwatch_script()
        self.assertEqual(set(P.SAFE), {r[0] for r in script.RULES if r[4]})
        self.assertEqual(len(P.SAFE), len(set(P.SAFE)))


class Step(T.Base):
    """The step through M.run against FakeHost, in the old-kit switch (the default mode installs plugins, which the fake host does not)."""

    def setUp(self):
        T.Base.setUp(self)
        self.new = os.path.join(self.root, 'new-agent-temp')
        self.regs = []
        self.host.task_register = lambda line: self.regs.append(line)    # the fake's own adds the sweep's name

    def go(self, apply=False, **kw):
        kw.setdefault('agent_temp', self.new)
        kw.setdefault('sweep_task', False)
        ctx = M.Context(self.home, self.kit, self.old, desktop=self.desk, host=self.host, today=T.TODAY, **kw)
        code, lines = M.run(ctx, apply=apply)
        return code, '\n'.join(lines)

    def test_nothing_is_planned_without_the_flag(self):
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.regs, [])
        self.assertNotIn(P.TASK, out)

    def test_dry_run_plans_the_task_and_registers_nothing(self):
        before = self.snapshot()
        code, out = self.go(procwatch=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('register the scheduled task Claude procwatch (every 15 min', out)
        self.assertIn('report only, kills nothing', out)
        self.assertIn(os.path.join(self.new, 'procwatch.log'), out)
        self.assertEqual(self.regs, [])
        self.assertEqual(self.snapshot(), before)

    def test_apply_registers_once_with_the_log_in_the_agent_temp_folder_and_no_kill(self):
        code, out = self.go(apply=True, procwatch=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.regs), 1, out)
        line = self.regs[0]
        self.assertIn("-TaskName 'Claude procwatch'", line)
        self.assertIn('--log "%s"' % os.path.join(self.new, 'procwatch.log'), line)
        self.assertNotIn('--kill', line)
        self.assertIn('done: register the scheduled task Claude procwatch', out)

    def test_apply_with_kill_adds_only_the_safe_set(self):
        code, out = self.go(apply=True, procwatch=True, procwatch_kill=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn("--kill --only %s'" % ','.join(P.SAFE), self.regs[0])
        self.assertIn('ends the SAFE rules', out)

    def test_a_task_already_there_is_updated_not_skipped(self):
        self.host.tasks = {P.TASK}
        code, out = self.go(apply=True, procwatch=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('update the scheduled task Claude procwatch', out)
        self.assertEqual(len(self.regs), 1)

    def test_a_failed_query_is_a_note_and_registers_nothing(self):
        self.host.task_error = RuntimeError('schtasks broke')
        code, out = self.go(apply=True, procwatch=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('could not tell whether the scheduled task Claude procwatch is registered '
                      '(schtasks broke)', out)
        self.assertEqual(self.regs, [])

    def test_a_failed_registration_stops_the_run(self):
        def boom(line):
            raise RuntimeError('no pythonw')
        self.host.task_register = boom
        code, out = self.go(apply=True, procwatch=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('STOPPED at: register the scheduled task Claude procwatch', out)

    def test_kill_without_procwatch_is_refused_by_the_context(self):
        with self.assertRaises(ValueError):
            M.Context(self.home, self.kit, self.old, host=self.host, procwatch_kill=True)


class Tool(T.Base):
    """tools/machine_setup.py's flags, against FakeHost."""

    def setUp(self):
        T.Base.setUp(self)
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        self.regs = []
        self.host.task_register = lambda line: self.regs.append(line)
        self.new = os.path.join(self.root, 'new-agent-temp')

    def main(self, *flags):
        import machine_setup
        argv = ['--old-kit', self.old, '--kit', self.kit, '--home', self.home, '--agent-temp', self.new, '--desktop', self.desk,
                '--no-sweep-task'] + list(flags)
        err = io.StringIO()
        with mock.patch.object(extras, 'Host', lambda: self.host), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            code = machine_setup.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_procwatch_flag_registers_report_only_and_the_kill_flag_adds_the_kill(self):
        self.assertEqual(self.main('--apply')[0], 0)
        self.assertEqual(self.regs, [])
        self.assertEqual(self.main('--procwatch', '--apply')[0], 0)
        self.assertEqual(len(self.regs), 1)
        self.assertNotIn('--kill', self.regs[0])
        self.assertEqual(self.main('--procwatch', '--procwatch-kill', '--apply')[0], 0)
        self.assertIn('--kill --only', self.regs[1])

    def test_the_dry_run_is_the_default(self):
        code, out, _ = self.main('--procwatch')
        self.assertEqual(code, 0)
        self.assertIn('register the scheduled task Claude procwatch', out)
        self.assertEqual(self.regs, [])

    def test_kill_alone_is_a_usage_error(self):
        code, _, err = self.main('--procwatch-kill')
        self.assertEqual(code, M.EXIT_USAGE)
        self.assertIn('--procwatch-kill goes with --procwatch', err)
        self.assertEqual(self.regs, [])

    def test_the_wrapper_passes_the_switches_on(self):
        with open(os.path.join(T.KIT, 'installer', 'machine-setup.ps1'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn("if ($Procwatch) { $argv += '--procwatch' }", text)
        self.assertIn("if ($ProcwatchKill) { $argv += '--procwatch-kill' }", text)


if __name__ == '__main__':
    unittest.main()
