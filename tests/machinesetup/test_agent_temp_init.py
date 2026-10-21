"""kitlib.machinesetup.agenttemp through machine setup's extras (docs/machine-setup.md "Agent temp
init"): the agents' temp folder, AGENT_TEMP, the agent_temp_sweep config key and the sweep task,
against the fake home, kit and FakeHost of test_machinesetup. Nothing here reads or writes the real
home folder, environment or task scheduler.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import extras
from kitlib.config import user as kuser
from kitlib.config.migration import OLD_DIR_NAME
from tests.home import PM_REPO_FENCE_ENV
from tests.machinesetup import test_machinesetup as T


def at():
    from kitlib.machinesetup import agenttemp
    return agenttemp


class ShortcutPythonw(unittest.TestCase):
    def test_the_shortcut_target_is_the_base_install_pythonw_even_from_a_venv(self):
        from kitlib.launcher import pythonw as P
        with mock.patch.object(sys, 'executable', 'C:\\venv\\Scripts\\python.exe'), \
                mock.patch.object(P.os.path, 'isfile', return_value=True):
            self.assertEqual(extras.pythonw(), P.base_pythonw())

    def test_a_store_alias_base_install_falls_back_to_this_interpreter(self):
        from kitlib.launcher import pythonw as P
        alias = 'C:\\Store\\WindowsApps\\pythonw.exe'
        with mock.patch.object(P, 'base_pythonw', return_value=alias), \
                mock.patch.object(P.os.path, 'isfile', return_value=True):
            self.assertEqual(extras.pythonw(), sys.executable)


class DefaultFolder(unittest.TestCase):
    def test_default_agent_temp_is_kit_temp_on_the_kits_drive(self):
        for kit, drive in (('C:\\x\\kit', 'C:'), ('D:\\kit', 'D:'), ('Z:\\src\\kit', 'Z:')):
            self.assertEqual(at().default_agent_temp(kit), drive + '\\kit-temp', kit)
        self.assertIsNone(at().default_agent_temp('\\\\server\\share\\kit'))


class SweepDryRun(unittest.TestCase):
    """agenttemp.sweep_dry_run with a fake `run`: nothing is started."""
    KIT = 'C:\\k'

    def fake(self, code=0, out='', err='', raises=None):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, code, out, err)
        return run, calls

    def test_argv_has_no_apply_and_is_python_then_the_sweep(self):
        run, calls = self.fake(out='a\n')
        at().sweep_dry_run(self.KIT, 'py.exe', run=run)
        argv = calls[0][0]
        self.assertEqual(argv, ['py.exe', os.path.join(os.path.normpath(self.KIT), at().SWEEP)])
        self.assertNotIn('--apply', argv)
        self.assertEqual(argv, at().sweep_dry_run_argv(self.KIT, 'py.exe'))

    def test_home_is_passed_on_to_the_sweep(self):
        run, calls = self.fake()
        at().sweep_dry_run(self.KIT, 'py.exe', home='C:\\h', run=run)
        self.assertEqual(calls[0][0][-2:], ['--home', 'C:\\h'])
        self.assertEqual(calls[0][0], at().sweep_dry_run_argv(self.KIT, 'py.exe', 'C:\\h'))
        run, calls = self.fake()
        at().sweep_dry_run(self.KIT, 'py.exe', run=run)
        self.assertNotIn('--home', calls[0][0])

    def test_the_sweep_is_run_and_read_as_utf8_whatever_the_locale(self):
        run, calls = self.fake()
        at().sweep_dry_run(self.KIT, 'py.exe', run=run)
        kw = calls[0][1]
        self.assertEqual(kw.get('encoding'), 'utf-8')
        self.assertEqual(kw.get('errors'), 'replace')
        self.assertEqual(kw['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertNotIn('text', kw)

    def test_creationflags_and_timeout_are_passed(self):
        run, calls = self.fake()
        at().sweep_dry_run(self.KIT, 'py.exe', timeout=7, run=run)
        kw = calls[0][1]
        self.assertEqual(kw['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(kw['timeout'], 7)

    def test_exit_0_and_3_return_the_non_blank_lines_of_stdout_and_stderr(self):
        for code in (0, 3):
            run, _ = self.fake(code=code, out='one\n\n  \ntwo\n', err='three\n')
            self.assertEqual(at().sweep_dry_run(self.KIT, 'py.exe', run=run), ['one', 'two', 'three'], code)

    def test_exit_1_and_2_raise(self):
        for code in (1, 2):
            run, _ = self.fake(code=code, out='bad config\n')
            with self.assertRaises(RuntimeError) as cm:
                at().sweep_dry_run(self.KIT, 'py.exe', run=run)
            self.assertIn('exit %d' % code, str(cm.exception))
            self.assertIn('bad config', str(cm.exception))

    def test_a_timeout_raises(self):
        run, _ = self.fake(raises=subprocess.TimeoutExpired('x', 5))
        with self.assertRaises(RuntimeError) as cm:
            at().sweep_dry_run(self.KIT, 'py.exe', timeout=5, run=run)
        self.assertIn('took over 5 s', str(cm.exception))


class TaskRegistered(unittest.TestCase):
    """agenttemp.task_registered against a fake subprocess.run: schtasks is never run."""

    def query(self, rc=0, out='', err='', raises=None):
        seen = {}

        def run(cmd, **kw):
            seen.update(kw, cmd=cmd)
            if raises:
                raise raises
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return seen, run

    def test_exit_0_is_registered_and_a_timeout_is_passed(self):
        seen, run = self.query(0)
        self.assertTrue(at().task_registered('agent-temp-sweep', run=run))
        self.assertEqual(seen['cmd'], ['schtasks', '/Query', '/TN', 'agent-temp-sweep'])
        self.assertEqual(seen['timeout'], at().TASK_TIMEOUT)

    @unittest.skipUnless(os.name == 'nt', 'the not-found text is Windows\' own')
    def test_not_found_is_not_registered(self):
        import ctypes
        _, run = self.query(1, err='ERROR: %s\n' % ctypes.FormatError(2))
        self.assertFalse(at().task_registered('agent-temp-sweep', run=run))

    def test_access_denied_is_a_named_failure_not_unregistered(self):
        _, run = self.query(1, err='ERROR: Access is denied.\n')
        with self.assertRaises(RuntimeError) as c:
            at().task_registered('agent-temp-sweep', run=run)
        self.assertIn('Access is denied', str(c.exception))

    def test_a_timeout_is_a_named_failure(self):
        _, run = self.query(raises=subprocess.TimeoutExpired('schtasks', 30))
        with self.assertRaises(RuntimeError) as c:
            at().task_registered('agent-temp-sweep', timeout=30, run=run)
        self.assertIn('timed out after 30 s', str(c.exception))

    def test_the_hosts_task_exists_uses_it(self):
        with mock.patch.object(at(), 'task_registered', lambda name: ('asked', name)):
            self.assertEqual(extras.Host().task_exists('agent-temp-sweep'), ('asked', 'agent-temp-sweep'))


class Init(T.Base):
    def setUp(self):
        T.Base.setUp(self)
        self.new = os.path.join(self.root, 'new-agent-temp')
        self.cfg = os.path.join(self.cfg_dir, kuser.FILE_NAME)          # the PM repository's config.json

    def run_new(self, apply=False, **kw):
        kw.setdefault('agent_temp', self.new)
        code, lines = M.run(M.Context(self.home, self.kit, self.old, desktop=self.desk, host=self.host,
                                      today=T.TODAY, **kw), apply=apply)
        return code, '\n'.join(lines)

    def write_cfg(self, doc, crlf=False):
        text = json.dumps(doc, indent=4) + '\n'
        with open(self.cfg, 'wb') as f:
            f.write((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))

    def test_dry_run_plans_folder_env_config_and_task_note_when_nothing_is_set(self):
        before = self.snapshot()
        code, out = self.run_new()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertNotIn('REFUSED', out)
        self.assertIn('make the agent temp folder %s' % self.new, out)
        self.assertIn('user environment AGENT_TEMP = %s' % self.new, out)
        self.assertIn('add agent_temp_sweep = {"dir": %s, "max_age_days": 3}' % json.dumps(self.new), out)
        self.assertIn('register the scheduled task agent-temp-sweep', out)
        self.assertIn('run the sweep once without --apply: ', out)
        self.assertIn('before the task is registered', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.host.calls, [])                          # no dry run of the sweep either
        self.assertFalse(os.path.exists(self.new))

    def test_apply_creates_folder_sets_env_and_writes_config_key_without_other_keys_lost(self):
        ledger = os.path.join(self.root, 'ledger.md')
        self.write_cfg({'ledger_path': ledger}, crlf=True)
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(os.path.isdir(self.new))
        self.assertEqual(self.host.env['AGENT_TEMP'], self.new)
        raw = T.readb(self.cfg)
        self.assertIn(b'\r\n', raw)
        self.assertTrue(raw.decode('utf-8').startswith('{\r\n    "ledger_path"'))
        doc = json.loads(raw.decode('utf-8'))
        self.assertEqual(doc, {'ledger_path': ledger,
                               'agent_temp_sweep': {'dir': self.new, 'max_age_days': 3}})
        self.assertEqual(kuser.user_config(self.home)['agent_temp_sweep']['dir'], self.new)

    def test_second_run_is_nothing_to_do(self):
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        snap = self.snapshot()
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('machine-setup: nothing to do', out)
        self.assertEqual(self.snapshot(), snap)
        code, out = self.run_new(agent_temp=None)        # the config key names it now
        self.assertIn('machine-setup: nothing to do', out)

    def test_existing_config_key_is_never_overwritten_and_is_noted(self):
        other = os.path.join(self.root, 'other-temp')
        self.write_cfg({'agent_temp_sweep': {'dir': other, 'max_age_days': 5}})
        before = T.readb(self.cfg)
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('agent_temp_sweep.dir is %s, not %s; left as is' % (other, self.new), out)
        self.assertEqual(T.readb(self.cfg), before)

    def test_env_set_to_another_folder_is_noted_not_overwritten(self):
        self.write_cfg({'agent_temp_sweep': {'dir': self.temp}})
        other = os.path.join(self.root, 'other-temp')
        self.host.env['AGENT_TEMP'] = other
        code, out = self.run_new(apply=True, agent_temp=None)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('user environment AGENT_TEMP is %s, not %s' % (other, self.temp), out)
        self.assertEqual(self.host.env['AGENT_TEMP'], other)
        self.assertFalse([c for c in self.host.calls if c[:2] == ('env', 'AGENT_TEMP')])
        code, out = self.run_new(apply=True)             # --agent-temp given: it is set
        self.assertEqual(self.host.env['AGENT_TEMP'], self.new)

    def test_a_config_still_in_the_home_folder_refuses(self):
        # the PM repository has none yet: the home file is the move's to carry, never written here
        os.rmdir(self.cfg_dir)
        for name in (kuser.DIR_NAME, OLD_DIR_NAME):
            with self.subTest(name):
                folder = os.path.join(self.home, name)
                os.makedirs(folder)
                with open(os.path.join(folder, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
                    f.write('{}\n')
                before = self.snapshot()
                code, out = self.run_new(apply=True)
                self.assertEqual(code, M.EXIT_REFUSED, out)
                self.assertIn('REFUSED - cannot read the machine config for agent_temp_sweep: the machine '
                              'config is still in the home folder (%s)' % os.path.join(folder, kuser.FILE_NAME), out)
                self.assertIn('nothing changed', out)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(os.path.exists(self.new))
                self.assertFalse(os.path.exists(self.cfg_dir))
                shutil.rmtree(folder)

    def test_no_pm_repo_pointer_refuses_naming_the_pointer_step_and_makes_no_home_file(self):
        patch = mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''})
        patch.start()
        self.addCleanup(patch.stop)
        kuser.forget()
        before = self.snapshot()
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED - cannot write agent_temp_sweep to the machine config: no PM repo '
                      'pointer is set', out)
        self.assertIn('tools/machine_setup.py --pm-repo', out)
        self.assertIn('nothing changed', out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.home, kuser.DIR_NAME)))
        self.assertFalse(os.path.exists(self.new))

    def test_a_folder_inside_a_git_checkout_refuses(self):
        os.makedirs(os.path.join(self.root, 'repo', '.git'))
        inside = os.path.join(self.root, 'repo', 'temp')
        code, out = self.run_new(apply=True, agent_temp=inside)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('inside a git checkout or worktree', out)
        self.assertFalse(os.path.exists(inside))

    def assert_refused_untouched(self, out, code, folder):
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('may not be swept', out)
        self.assertIn('nothing changed', out)
        self.assertFalse([c for c in self.host.calls if c[0] in ('icacls', 'env', 'task')], out)
        self.assertFalse(os.path.exists(self.cfg) and 'agent_temp_sweep' in T.readb(self.cfg).decode())

    def test_the_sweeps_refusals_apply_to_every_source_of_the_folder(self):
        # The folder gets the config key and an ACL with inheritance removed, so a folder the sweep
        # would refuse (a drive root, the home folder or one in it such as %LOCALAPPDATA%\Temp, the
        # kit, the machine config folder) is refused here too, from --agent-temp or AGENT_TEMP.
        local_temp = os.path.join(self.home, 'AppData', 'Local', 'Temp')
        os.makedirs(local_temp)
        cfg_dir = kuser.config_dir(self.home)
        for folder in (os.path.abspath(os.sep), self.home, local_temp, self.kit,
                       os.path.join(self.kit, 'temp'), cfg_dir):
            code, out = self.run_new(agent_temp=folder)
            self.assert_refused_untouched(out, code, folder)
        self.host.env['AGENT_TEMP'] = local_temp                 # taken from the user environment
        code, out = self.run_new(apply=True, agent_temp=None)
        self.assert_refused_untouched(out, code, local_temp)
        self.assertEqual(self.host.env['AGENT_TEMP'], local_temp)

    @unittest.skipUnless(hasattr(__import__('_winapi'), 'CreateJunction'), 'a junction needs Windows')
    def test_a_folder_through_a_junction_into_a_repo_refuses(self):
        import _winapi
        repo = os.path.join(self.root, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        os.makedirs(os.path.join(repo, 'src'))
        via = os.path.join(self.root, 'via')
        _winapi.CreateJunction(os.path.join(repo, 'src'), via)
        self.addCleanup(lambda: os.path.lexists(via) and os.rmdir(via))
        code, out = self.run_new(apply=True, agent_temp=os.path.join(via, 'temp'))
        self.assert_refused_untouched(out, code, via)
        self.assertIn('git checkout', out)
        self.assertFalse(os.path.exists(os.path.join(repo, 'src', 'temp')))

    def tasks(self):
        return [c for c in self.host.calls if c[0] == 'task']

    def test_task_registered_by_default_after_a_dry_run_of_the_sweep(self):
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        calls = self.tasks()
        self.assertEqual(len(calls), 1, out)
        line = calls[0][1]
        for want in ('-RunLevel Limited', '--apply', '-StartWhenAvailable', '-Daily -At 04:00',
                     os.path.join(self.kit, 'optional', 'agenttemp', 'agent_temp_sweep.py')):
            self.assertIn(want, line)
        kinds = [c[0] for c in self.host.calls]
        self.assertLess(kinds.index('sweep-dry-run'), kinds.index('task'))   # the dry run first
        self.assertIn(('sweep-dry-run', self.kit, self.home), self.host.calls)   # the run's --home reaches it
        dry = '  sweep dry run (nothing removed): claude/p/old-session  4.0 days  would'
        self.assertIn(dry, out)
        self.assertLess(out.index(dry), out.index('done: register the scheduled task'))

    def test_no_sweep_task_leaves_it_unregistered_and_notes_the_line(self):
        code, out = self.run_new(apply=True, sweep_task=False)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.tasks(), [])
        self.assertNotIn('sweep-dry-run', [c[0] for c in self.host.calls])
        self.assertIn('scheduled task agent-temp-sweep is not registered (--no-sweep-task)', out)
        self.assertIn('-RunLevel Limited', out)

    def test_no_sweep_task_leaves_a_registered_task_and_says_how_to_undo_it(self):
        self.host.tasks = {'agent-temp-sweep'}
        code, out = self.run_new(apply=True, sweep_task=False)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.tasks(), [])
        self.assertIn('scheduled task agent-temp-sweep is registered; --no-sweep-task leaves it', out)
        self.assertIn('Unregister-ScheduledTask -TaskName agent-temp-sweep', out)

    def test_a_registered_task_is_left_and_a_second_run_does_nothing(self):
        code, out = self.run_new(apply=True)
        self.assertEqual(len(self.tasks()), 1, out)
        calls = len(self.host.calls)
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('machine-setup: nothing to do', out)
        self.assertEqual(len(self.host.calls), calls)                     # no dry run, no register
        self.host.tasks = {'agent-temp-sweep'}                            # one registered by hand
        self.host.calls = []
        code, out = self.run_new(apply=True, agent_temp=None)
        self.assertIn('machine-setup: nothing to do', out)
        self.assertEqual(self.host.calls, [])

    def test_a_sweep_dry_run_that_refuses_registers_nothing(self):
        self.host.sweep_error = RuntimeError('the sweep dry run exit 2, so the task is not registered')
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertEqual(self.tasks(), [])
        self.assertIn('machine-setup: STOPPED at: run the sweep once without --apply', out)
        self.assertIn('the sweep dry run exit 2', out)

    def test_the_acl_is_applied_before_the_sweep_dry_run(self):
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        kinds = [c[0] for c in self.host.calls]
        self.assertLess(kinds.index('icacls'), kinds.index('sweep-dry-run'), out)

    def test_a_refusing_sweep_dry_run_still_leaves_the_acl_applied(self):
        self.host.sweep_error = RuntimeError('the sweep dry run exit 2, so the task is not registered')
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('icacls', [c[0] for c in self.host.calls], out)

    def test_the_standalone_tool_applies_the_acl_before_the_dry_run_too(self):
        self.host.sweep_error = RuntimeError('the sweep dry run exit 2, so the task is not registered')
        code, out, _ = self.init_tool('--agent-temp', self.new, '--apply')
        self.assertEqual(code, 1, out)
        self.assertIn('icacls', [c[0] for c in self.host.calls], out)

    def test_the_dry_run_report_is_printed_even_when_registering_fails(self):
        with mock.patch.object(self.host, 'task_register', side_effect=RuntimeError('no pythonw')):
            code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('  sweep dry run (nothing removed): claude/p/old-session  4.0 days  would', out)
        self.assertIn('machine-setup: STOPPED at: register the scheduled task agent-temp-sweep', out)

    def test_the_tool_registers_by_default_and_no_sweep_task_opts_out(self):
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        import machine_setup
        argv = ['--old-kit', self.old, '--kit', self.kit, '--home', self.home, '--agent-temp', self.new,
                '--desktop', self.desk, '--apply']
        with mock.patch.object(extras, 'Host', lambda: self.host):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine_setup.main(argv + ['--no-sweep-task']), 0)
            self.assertEqual(self.tasks(), [])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(machine_setup.main(argv + ['--no-sweep-task', '--register-sweep-task']),
                                 M.EXIT_USAGE)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine_setup.main(argv), 0)
            self.assertEqual(len(self.tasks()), 1)
            with contextlib.redirect_stdout(io.StringIO()):                # the old opt-in, still accepted
                self.assertEqual(machine_setup.main(argv + ['--register-sweep-task']), 0)
        self.assertEqual(len(self.tasks()), 1)

    def init_tool(self, *argv):
        """tools/agent_temp_init.py main() against FakeHost: (exit code, stdout, stderr)."""
        tools = os.path.dirname(T.TOOL)
        sys.path.insert(0, tools)
        self.addCleanup(sys.path.remove, tools)
        import agent_temp_init
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(extras, 'Host', lambda: self.host), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent_temp_init.main(['--kit', self.kit, '--home', self.home] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_agent_temp_init_tool_dry_run_then_apply_does_the_temp_steps_only(self):
        before = self.snapshot()
        code, out, _ = self.init_tool('--agent-temp', self.new)
        self.assertEqual(code, 0, out)
        self.assertIn('agent-temp-init: DRY RUN', out)
        self.assertIn('make the agent temp folder %s' % self.new, out)
        self.assertIn('(made by this run): permissions', out)
        self.assertNotIn('links:', out)
        self.assertNotIn('settings:', out)
        self.assertNotIn('Machine schedule', out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.host.calls, [])
        code, out, _ = self.init_tool('--agent-temp', self.new, '--apply', '--no-sweep-task')
        self.assertEqual(code, 0, out)
        self.assertIn('agent-temp-init: made', out)
        self.assertTrue(os.path.isdir(self.new))
        self.assertEqual(self.host.env['AGENT_TEMP'], self.new)
        self.assertEqual(kuser.user_config(self.home)['agent_temp_sweep']['dir'], self.new)
        self.assertEqual(sorted({c[0] for c in self.host.calls}), ['env', 'icacls'])
        code, out, _ = self.init_tool('--agent-temp', self.new, '--apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.tasks()), 1)
        self.assertIn('sweep dry run (nothing removed): ', out)
        code, out, _ = self.init_tool('--apply')          # the config key names it now
        self.assertEqual(code, 0, out)
        self.assertIn('agent-temp-init: nothing to do', out)

    def test_agent_temp_init_tool_exit_codes(self):
        code, out, _ = self.init_tool('--agent-temp', os.path.join(self.home, 'AppData', 'Local', 'Temp'),
                                      '--apply')
        self.assertEqual(code, 1, out)
        self.assertIn('agent-temp-init: REFUSED', out)
        self.assertIn('agent-temp-init: nothing changed', out)
        self.assertEqual(self.host.calls, [])
        code, _, err = self.init_tool('--old-kit', self.old)    # not an option of this tool
        self.assertEqual(code, 2, err)
        self.assertIn('unrecognized arguments', err)
        code, out, _ = self.init_tool('--help')
        self.assertEqual(code, 0)
        self.assertIn('--no-sweep-task', out)
        code, _, err = self.init_tool('--no-sweep-task', '--register-sweep-task')
        self.assertEqual(code, 2, err)
        self.assertIn('not both', err)

    def test_task_query_failure_is_a_note(self):
        self.host.task_error = RuntimeError('schtasks broke')
        code, out = self.run_new()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('could not tell whether the scheduled task agent-temp-sweep is registered '
                      '(schtasks broke)', out)

    def test_task_line_quotes_a_kit_path_with_a_quote_and_a_space(self):
        line = at().task_line("C:\\it's a kit")
        self.assertIn("-WorkingDirectory 'C:\\it''s a kit'", line)
        self.assertIn("-Argument '\"C:\\it''s a kit\\tools\\no_window.py\" python"
                      " \"C:\\it''s a kit\\optional\\agenttemp\\agent_temp_sweep.py\" --apply'", line)

    def test_task_line_starts_no_console_window(self):
        """The task runs pythonw (no console of its own) on the kit's carrier, which starts the
        console python on the sweep with no window."""
        line = at().task_line('C:\\kit', pythonw='C:\\Py\\pythonw.exe')
        self.assertTrue(line.startswith("$py = 'C:\\Py\\pythonw.exe'; "), line)
        self.assertNotIn('Get-Command', line)
        self.assertIn("-Execute $py -Argument '\"C:\\kit\\tools\\no_window.py\" python"
                      " \"C:\\kit\\optional\\agenttemp\\agent_temp_sweep.py\" --apply'", line)
        self.assertTrue(os.path.isfile(os.path.join(T.KIT, 'tools', 'no_window.py')))

    def test_task_line_defaults_to_the_base_install_pythonw_not_a_path_lookup(self):
        """The Store alias first on PATH is never asked: the literal is the base install's."""
        from kitlib.launcher import pythonw as P
        line = at().task_line('C:\\kit')
        self.assertIn("$py = '%s'" % P.base_pythonw().replace("'", "''"), line)
        self.assertNotIn('Get-Command', line)

    def test_task_line_throws_in_powershell_for_a_store_alias_or_a_missing_file(self):
        alias = 'C:\\Store\\WindowsApps\\pythonw.exe'
        line = at().task_line('C:\\kit', pythonw=alias)
        self.assertIn(alias, line)
        self.assertIn("-match '\\\\WindowsApps\\\\'", line)
        self.assertIn('Test-Path -LiteralPath $py', line)

    def test_acl_step_no_longer_refuses_when_the_folder_is_made_in_the_same_run(self):
        code, out = self.run_new()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertNotIn('does not exist', out)
        make = out.index('make the agent temp folder')
        grant = out.index('%s (made by this run): permissions' % self.new)
        self.assertLess(make, grant)
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(self.host.acl_fixed)
        icacls = [c for c in self.host.calls if c[0] == 'icacls'][0][1]
        self.assertEqual(icacls[:3], [self.new, '/inheritance:r', '/grant:r'])


if __name__ == '__main__':
    unittest.main()
