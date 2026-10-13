"""Hook paths the hosts/ move changed (docs/machine-setup.md "Moved hook paths"): settings written
before it name <kit>/claude/hooks/<x>.py, which machine setup re-points to hosts/claude/shell/hooks
in every mode and in the PM settings, so no account gets a second guard entry. Runs on a temp kit;
settings.expand reads Windows paths only, so the tests read an absolute path of this OS as one."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import common, links, settings
from tests.machinesetup import test_machinesetup as T

HOOKS = ('guard.py', 'pm_session_start.py', 'coordinator_session_start.py')


REAL_EXPAND = settings.expand


def expand(tok, home):
    return REAL_EXPAND(tok, home) or (os.path.normpath(tok) if os.path.isabs(tok) else '')


class MovedHooks(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-moved-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.kit = os.path.join(self.root, 'kit')
        self.home = os.path.join(self.root, 'home')
        self.acct = os.path.join(self.home, '.claude')
        for name in HOOKS:
            T.touch(self.new(name))
            T.touch(self.old(name))
        os.makedirs(self.acct)
        patcher = mock.patch.object(settings, 'expand', expand)
        patcher.start()
        self.addCleanup(patcher.stop)

    def new(self, name):
        return os.path.join(self.kit, *(common.SHELL + '/hooks/' + name).split('/'))

    def old(self, name):
        return os.path.join(self.kit, common.MOVED_FROM, 'hooks', name)

    def ctx(self, **kw):
        kw.setdefault('old_kit', None if kw.get('fresh') else os.path.join(self.root, 'old-kit'))
        return M.Context(self.home, self.kit, config_dirs=[self.acct], host=T.FakeHost(self.kit),
                         today=T.TODAY, **kw)

    def settings_with(self, path, hooks):
        T.touch(path, json.dumps({'hooks': hooks}, indent=2) + '\n')

    def guard_hook(self, script):
        return {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [T.fwd(script)]}]}]}

    def args(self, path, event):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return [h.get('args') or h.get('command') for g in data['hooks'][event] for h in g['hooks']]

    def apply(self, plan):
        self.assertEqual(plan.refusals, [])
        for c in plan.changes:
            c.do()

    def test_repoint_moved_rewrites_args_and_shell_commands_in_every_event(self):
        data = {'hooks': {'PreToolUse': self.guard_hook(self.old('guard.py'))['PreToolUse'],
                          'SessionStart': [{'hooks': [
                              {'type': 'command', 'command': 'python "%s"' % T.fwd(self.old('pm_session_start.py'))},
                              {'type': 'command', 'command': T.PYW, 'args': [self.old('coordinator_session_start.py')]},
                              {'type': 'command', 'command': 'python C:/elsewhere/x.py'}]}]}}
        ctx = self.ctx()
        did = settings.repoint_moved(data, ctx)
        self.assertEqual(len(did), 3, did)
        pre = data['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertEqual(pre['args'], [T.fwd(self.new('guard.py'))])
        hs = data['hooks']['SessionStart'][0]['hooks']
        self.assertEqual(hs[0]['command'], 'python "%s"' % T.fwd(self.new('pm_session_start.py')))
        self.assertEqual(hs[1]['args'], [T.fwd(self.new('coordinator_session_start.py'))])
        self.assertEqual(hs[2]['command'], 'python C:/elsewhere/x.py')
        self.assertEqual(settings.repoint_moved(data, ctx), [])        # the second run has nothing to do

    def test_a_moved_name_the_kit_lacks_is_refused(self):
        T.touch(self.old('gone.py'))
        data = {'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [self.old('gone.py')]}]}]}}
        with self.assertRaises(ValueError) as caught:
            settings.repoint_moved(data, self.ctx())
        self.assertIn('gone.py', str(caught.exception))

    def test_switch_mode_repoints_the_guard_and_adds_no_second_entry(self):
        path = os.path.join(self.acct, 'settings.json')
        self.settings_with(path, self.guard_hook(self.old('guard.py')))
        plan = M.Plan()
        settings.survey(plan, self.acct, self.ctx())
        self.apply(plan)
        self.assertEqual(self.args(path, 'PreToolUse'), [[T.fwd(self.new('guard.py'))]])

    def test_fresh_mode_repoints_the_guard_and_adds_no_second_entry(self):
        path = os.path.join(self.acct, 'settings.json')
        self.settings_with(path, self.guard_hook(self.old('guard.py')))
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        settings.survey(plan, self.acct, self.ctx(fresh=True))
        self.apply(plan)
        self.assertEqual(self.args(path, 'PreToolUse'), [[T.fwd(self.new('guard.py'))]])
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        settings.survey(plan, self.acct, self.ctx(fresh=True))
        self.assertEqual(plan.changes, [])                            # settled

    def test_the_pm_hook_is_repointed_not_refused(self):
        pm = os.path.join(self.root, 'pmrepo', '.claude', 'settings.local.json')
        self.settings_with(pm, {'SessionStart': [{'matcher': settings.PM_MATCHER, 'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [T.fwd(self.old('pm_session_start.py'))]}]}]})
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        settings.survey_pm(plan, self.ctx(fresh=True, pm_settings=pm))
        self.apply(plan)
        self.assertEqual(self.args(pm, 'SessionStart'), [[T.fwd(self.new('pm_session_start.py'))]])


@unittest.skipUnless(sys.platform == 'win32', 'directory junctions are Windows only')
class MovedSkillLinks(unittest.TestCase):
    def test_a_skill_link_into_the_pre_move_folder_is_repointed(self):
        root = tempfile.mkdtemp(prefix='machinesetup-moved-links-')
        self.addCleanup(shutil.rmtree, root, True)
        kit, old, home = (os.path.join(root, n) for n in ('kit', 'old-kit', 'home'))
        acct = os.path.join(home, '.claude')
        want = os.path.join(kit, *(common.SHELL + '/skills/land-branch').split('/'))
        was = os.path.join(kit, common.MOVED_FROM, 'skills', 'land-branch')
        T.touch(os.path.join(want, 'SKILL.md'))
        T.touch(os.path.join(was, 'SKILL.md'))
        os.makedirs(os.path.join(acct, 'skills'))
        os.makedirs(old)
        link = os.path.join(acct, 'skills', 'land-branch')
        T.junction(link, was)
        shutil.rmtree(os.path.join(kit, common.MOVED_FROM))              # gone after the move: dangling
        ctx = M.Context(home, kit, old_kit=old, config_dirs=[acct], host=T.FakeHost(kit), today=T.TODAY)
        plan = M.Plan()
        links.survey(plan, acct, ctx)
        self.assertEqual(plan.refusals, [])
        self.assertEqual(len(plan.changes), 1, [c.text for c in plan.changes])
        self.assertIn('re-point the link', plan.changes[0].text)
        plan.changes[0].do()
        self.assertEqual(common.norm(common.link_target(link)), common.norm(want))


class Stubs(unittest.TestCase):
    """The stubs at the old paths run the same-named entry under hosts/claude/shell/hooks."""

    def test_each_stub_names_its_entry(self):
        for name in HOOKS:
            stub = os.path.join(T.KIT, common.MOVED_FROM, 'hooks', name)
            ns = {'__file__': stub, '__name__': 'stub'}
            with open(stub, encoding='utf-8') as f:
                exec(compile(f.read(), stub, 'exec'), ns)
            self.assertEqual(ns['NEW'], os.path.join(T.KIT, *(common.SHELL + '/hooks/' + name).split('/')))
            self.assertTrue(os.path.isfile(ns['NEW']), ns['NEW'])


if __name__ == '__main__':
    unittest.main()
