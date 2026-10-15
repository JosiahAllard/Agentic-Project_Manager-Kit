"""Hook paths plan steps 7 and 14 changed (docs/machine-setup.md "Moved hook paths"): settings
written before them name <kit>/claude/hooks/<x>.py or <kit>/hosts/claude/shell/hooks/<x>.py, which
machine setup re-points to the kit's one hook entry, boot/hook.py with the host and the script's
words, in every mode and in the PM settings, so no account gets a second guard entry. Runs on a temp
kit; settings.expand reads Windows paths only, so the tests read an absolute path of this OS as one."""
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
WORDS = {'guard.py': ['claude', 'PreToolUse'], 'pm_session_start.py': ['claude', 'SessionStart', 'pm'],
         'coordinator_session_start.py': ['claude', 'SessionStart', 'coordinator']}


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
        T.touch(self.entry())
        os.makedirs(self.acct)
        patcher = mock.patch.object(settings, 'expand', expand)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self):
        return os.path.join(self.kit, 'boot', 'hook.py')

    def new(self, name):
        """Where the hook scripts were from the hosts/ move to plan step 14."""
        return os.path.join(self.kit, *(common.SHELL + '/hooks/' + name).split('/'))

    def old(self, name):
        """Where they were before the hosts/ move."""
        return os.path.join(self.kit, common.MOVED_FROM, 'hooks', name)

    def now(self, name):
        """The exec-form args that run `name`'s work today."""
        return [T.fwd(self.entry())] + WORDS[name]

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
        for where in (self.old, self.new):
            with self.subTest(where=where.__name__):
                data = {'hooks': {'PreToolUse': self.guard_hook(where('guard.py'))['PreToolUse'],
                                  'SessionStart': [{'hooks': [
                                      {'type': 'command',
                                       'command': 'python "%s"' % T.fwd(where('pm_session_start.py'))},
                                      {'type': 'command', 'command': T.PYW,
                                       'args': [where('coordinator_session_start.py'), '--x']},
                                      {'type': 'command', 'command': 'python C:/elsewhere/x.py'}]}]}}
                ctx = self.ctx()
                did = settings.repoint_moved(data, ctx)
                self.assertEqual(len(did), 3, did)
                self.assertIn('re-point the PreToolUse hook guard.py from ', did[0])
                pre = data['hooks']['PreToolUse'][0]['hooks'][0]
                self.assertEqual(pre['args'], self.now('guard.py'))
                hs = data['hooks']['SessionStart'][0]['hooks']
                self.assertEqual(hs[0]['command'], 'python "%s" claude SessionStart pm' % T.fwd(self.entry()))
                self.assertEqual(hs[1]['args'], self.now('coordinator_session_start.py') + ['--x'])
                self.assertEqual(hs[2]['command'], 'python C:/elsewhere/x.py')
                self.assertEqual(settings.repoint_moved(data, ctx), [])        # the second run has nothing to do

    def test_a_name_that_was_no_kit_hook_is_refused(self):
        for where in (self.old, self.new):
            data = {'hooks': {'SessionStart': [{'hooks': [
                {'type': 'command', 'command': T.PYW, 'args': [where('kit_header.py')]}]}]}}
            with self.assertRaises(ValueError) as caught:
                settings.repoint_moved(data, self.ctx())
            self.assertIn('kit_header.py', str(caught.exception))

    def test_a_kit_without_the_entry_is_refused(self):
        os.remove(self.entry())
        data = {'hooks': {'PreToolUse': self.guard_hook(self.new('guard.py'))['PreToolUse']}}
        with self.assertRaises(ValueError) as caught:
            settings.repoint_moved(data, self.ctx())
        self.assertIn('which the kit lacks', str(caught.exception))

    def test_a_name_in_another_case_still_maps(self):
        """norm folds case on Windows, where the old path names the same file in any case."""
        fold = lambda path: os.path.normpath(os.path.abspath(path)).lower()    # noqa: E731
        data = {'hooks': {'PreToolUse': self.guard_hook(self.old('Guard.PY'))['PreToolUse']}}
        with mock.patch.object(settings, 'norm', fold), mock.patch.object(common, 'norm', fold):
            did = settings.repoint_moved(data, self.ctx())
        self.assertEqual(len(did), 1, did)
        self.assertEqual(data['hooks']['PreToolUse'][0]['hooks'][0]['args'], self.now('guard.py'))

    def test_switch_mode_repoints_the_guard_and_adds_no_second_entry(self):
        path = os.path.join(self.acct, 'settings.json')
        self.settings_with(path, self.guard_hook(self.old('guard.py')))
        plan = M.Plan()
        settings.survey(plan, self.acct, self.ctx())
        self.apply(plan)
        self.assertEqual(self.args(path, 'PreToolUse'), [self.now('guard.py')])

    def test_fresh_mode_repoints_the_guard_and_adds_no_second_entry(self):
        for where in (self.old, self.new):
            path = os.path.join(self.acct, 'settings.json')
            self.settings_with(path, self.guard_hook(where('guard.py')))
            plan = M.Plan(M.Plan.FRESH_SECTIONS)
            settings.survey(plan, self.acct, self.ctx(fresh=True))
            self.apply(plan)
            self.assertEqual(self.args(path, 'PreToolUse'), [self.now('guard.py')])
            plan = M.Plan(M.Plan.FRESH_SECTIONS)
            settings.survey(plan, self.acct, self.ctx(fresh=True))
            self.assertEqual(plan.changes, [])                        # settled
            for n in os.listdir(self.acct):
                if n != 'settings.json':
                    os.remove(os.path.join(self.acct, n))          # the .bak, for the next round

    def test_the_pm_hook_is_repointed_not_refused(self):
        pm = os.path.join(self.root, 'pmrepo', '.claude', 'settings.local.json')
        self.settings_with(pm, {'SessionStart': [{'matcher': settings.PM_MATCHER, 'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': [T.fwd(self.old('pm_session_start.py'))]}]}]})
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        settings.survey_pm(plan, self.ctx(fresh=True, pm_settings=pm))
        self.apply(plan)
        self.assertEqual(self.args(pm, 'SessionStart'), [self.now('pm_session_start.py')])
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        settings.survey_pm(plan, self.ctx(fresh=True, pm_settings=pm))
        self.assertEqual((plan.changes, plan.refusals), ([], []), 'the re-pointed hook is the PM hook')


@unittest.skipUnless(sys.platform == 'win32', 'directory junctions are Windows only')
class MovedSkillLinks(unittest.TestCase):
    """Machine setup makes no link (docs/machine-setup.md "Links"): a skill link into a folder the
    skills have left is a leftover, reported with the command that removes it, not re-pointed."""

    def test_a_skill_link_into_the_pre_move_folder_is_a_leftover(self):
        self.reported(common.MOVED_FROM + '/skills')

    def test_a_skill_link_into_the_claude_shells_old_copy_is_a_leftover(self):
        self.reported(common.SHELL + '/skills')           # the rendered copy before decision 29

    def reported(self, moved):
        root = tempfile.mkdtemp(prefix='machinesetup-moved-links-')
        self.addCleanup(shutil.rmtree, root, True)
        kit, old, home = (os.path.join(root, n) for n in ('kit', 'old-kit', 'home'))
        acct = os.path.join(home, '.claude')
        want = os.path.join(kit, *(links.SKILLS_DIR + '/land-branch').split('/'))
        was = os.path.join(kit, *(moved + '/land-branch').split('/'))
        T.touch(os.path.join(want, 'SKILL.md'))
        T.touch(os.path.join(was, 'SKILL.md'))
        os.makedirs(os.path.join(acct, 'skills'))
        os.makedirs(old)
        link = os.path.join(acct, 'skills', 'land-branch')
        T.junction(link, was)
        shutil.rmtree(os.path.dirname(was))                              # gone after the move: dangling
        ctx = M.Context(home, kit, old_kit=old, config_dirs=[acct], host=T.FakeHost(kit), today=T.TODAY)
        plan = M.Plan()
        links.survey(plan, acct, ctx)
        self.assertEqual((plan.changes, plan.refusals), ([], []))
        self.assertIn(links.leftover(link, was), plan.notes['links'])
        self.assertIn('cmd /c rmdir "%s"' % link, links.leftover(link, was))
        self.assertTrue(common.is_link(link))                               # reported, left in place
        self.assertTrue(os.path.isfile(os.path.join(want, 'SKILL.md')))
        os.rmdir(link)                                                     # the link only


class Retired(unittest.TestCase):
    """The kit keeps no hook script at the old paths (plan decision 28): an account's settings
    reach boot/hook.py only once machine setup has re-pointed them."""

    def test_no_hook_script_is_left_at_an_old_path(self):
        self.assertFalse(os.path.exists(os.path.join(T.KIT, common.MOVED_FROM)))
        hooks = os.path.join(T.KIT, *settings.HOOKS.split('/'))
        self.assertEqual(sorted(n for n in os.listdir(hooks) if n != '__pycache__'), ['hooks.json'])
        self.assertTrue(os.path.isfile(os.path.join(T.KIT, *settings.ENTRY.split('/'))))
        self.assertEqual(sorted(settings.RETIRED), sorted(HOOKS))
        self.assertEqual(settings.entry_words('guard.py'), WORDS['guard.py'])


if __name__ == '__main__':
    unittest.main()
