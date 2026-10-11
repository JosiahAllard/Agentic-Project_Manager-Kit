"""kitlib.machinesetup.agenttemp through machine setup's extras (docs/machine-setup.md "Agent temp
init"): the agents' temp folder, AGENT_TEMP, the agent_temp_sweep config key and the sweep task,
against the fake home, kit and FakeHost of test_machinesetup. Nothing here reads or writes the real
home folder, environment or task scheduler.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import extras
from kitlib.config import user as kuser
from kitlib.config.migration import OLD_DIR_NAME
from tests.machinesetup import test_machinesetup as T


def at():
    from kitlib.machinesetup import agenttemp
    return agenttemp


class DefaultFolder(unittest.TestCase):
    def test_default_agent_temp_is_AgentTemp_on_the_kits_drive(self):
        for kit, drive in (('C:\\x\\kit', 'C:'), ('D:\\kit', 'D:'), ('Z:\\src\\kit', 'Z:')):
            self.assertEqual(at().default_agent_temp(kit), drive + '\\AgentTemp', kit)
        self.assertIsNone(at().default_agent_temp('\\\\server\\share\\kit'))


class Init(T.Base):
    def setUp(self):
        T.Base.setUp(self)
        self.new = os.path.join(self.root, 'new-agent-temp')
        self.cfg = os.path.join(kuser.config_dir(self.home), kuser.FILE_NAME)

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
        self.assertIn('scheduled task agent-temp-sweep is not registered', out)
        self.assertIn('--register-sweep-task', out)
        self.assertEqual(self.snapshot(), before)
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

    def test_old_folder_only_config_refuses(self):
        os.rmdir(kuser.config_dir(self.home))
        old = os.path.join(self.home, OLD_DIR_NAME)
        os.makedirs(old)
        with open(os.path.join(old, kuser.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write('{}\n')
        before = self.snapshot()
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('cannot write agent_temp_sweep to the machine config', out)
        self.assertIn('nothing changed', out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.new))

    def test_a_folder_inside_a_git_checkout_refuses(self):
        os.makedirs(os.path.join(self.root, 'repo', '.git'))
        inside = os.path.join(self.root, 'repo', 'temp')
        code, out = self.run_new(apply=True, agent_temp=inside)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('inside the git checkout', out)
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

    def test_task_registered_only_with_the_flag(self):
        code, out = self.run_new(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertFalse([c for c in self.host.calls if c[0] == 'task'])
        self.assertIn('-RunLevel Limited', out)
        code, out = self.run_new(apply=True, register_sweep_task=True)
        self.assertEqual(code, M.EXIT_OK, out)
        calls = [c for c in self.host.calls if c[0] == 'task']
        self.assertEqual(len(calls), 1, out)
        line = calls[0][1]
        for want in ('-RunLevel Limited', '--apply', '-StartWhenAvailable', '-Daily -At 04:00',
                     os.path.join(self.kit, 'optional', 'agenttemp', 'agent_temp_sweep.py')):
            self.assertIn(want, line)
        code, out = self.run_new(apply=True, register_sweep_task=True)
        self.assertIn('machine-setup: nothing to do', out)
        self.assertEqual(len([c for c in self.host.calls if c[0] == 'task']), 1)

    def test_the_tool_passes_register_sweep_task(self):
        sys.path.insert(0, os.path.dirname(T.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(T.TOOL))
        import machine_setup
        argv = ['--old-kit', self.old, '--kit', self.kit, '--home', self.home, '--agent-temp', self.new,
                '--desktop', self.desk, '--apply']
        with mock.patch.object(extras, 'Host', lambda: self.host):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine_setup.main(argv), 0)
            self.assertFalse([c for c in self.host.calls if c[0] == 'task'])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine_setup.main(argv + ['--register-sweep-task']), 0)
        self.assertEqual(len([c for c in self.host.calls if c[0] == 'task']), 1)

    def test_task_query_failure_is_a_note(self):
        self.host.task_error = RuntimeError('schtasks broke')
        code, out = self.run_new(register_sweep_task=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('could not tell whether the scheduled task agent-temp-sweep is registered '
                      '(schtasks broke)', out)

    def test_task_line_quotes_a_kit_path_with_a_quote_and_a_space(self):
        line = at().task_line("C:\\it's a kit")
        self.assertIn("-WorkingDirectory 'C:\\it''s a kit'", line)
        self.assertIn("-Argument '\"C:\\it''s a kit\\optional\\agenttemp\\agent_temp_sweep.py\" --apply'", line)

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
