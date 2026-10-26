"""Machine setup makes no link (docs/machine-setup.md "Links"): in either mode it reports each link an
earlier setup left under a config folder, with the command that removes the link only, and re-points
a settings path through `<config>/tools/<link>` to the kit's own file (settings.repoint_links), so a
status line keeps working once the owner removes the link. Temp folders only; nothing here reads or
writes the real home."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from kitlib import machinesetup as M
from kitlib.machinesetup import common, links, settings
from tests.machinesetup import test_fresh as F
from tests.machinesetup import test_machinesetup as T
from tests.platforms import windows_only


def links_under(top):
    """{path: normalised target} of every link under `top`, and the folders named `*.old-*`."""
    found, aside = {}, []
    for dirpath, dirnames, _ in os.walk(top):
        for n in list(dirnames):
            p = os.path.join(dirpath, n)
            if '.old-' in n:
                aside.append(p)
            if common.is_link(p):
                found[p] = common.norm(common.link_target(p))
                dirnames.remove(n)
    return found, aside


class OldKitRun(T.Base):
    def test_old_kit_run_makes_no_link(self):
        comfy = os.path.join(self.c2, 'tools', 'comfy')
        usage = os.path.join(self.c2, 'tools', 'usage')
        self.assertFalse(os.path.lexists(usage))
        before = links_under(self.home)
        for apply in (False, True):
            code, out = self.run_it(apply=apply)
            self.assertEqual(code, M.EXIT_OK, out)
            self.assertEqual(links_under(self.home), before, out)            # no link made or replaced
            self.assertEqual(links_under(self.home)[1], [], out)            # nothing renamed aside
            self.assertFalse(common.is_link(comfy))
            self.assertEqual(os.listdir(comfy), ['local-notes.txt'])
            self.assertFalse(os.path.lexists(usage), out)
        self.assertIn('%s: a real folder, left as it is' % comfy, out)
        code, out = self.run_it(apply=True)
        self.assertIn('machine-setup: nothing to do', out)

    def test_old_kit_run_reports_each_leftover_link(self):
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        for p, t in ((os.path.join(self.c1, 'tools'), os.path.join(self.old, 'tools')),
                     (os.path.join(self.c1, 'skills'), os.path.join(self.old, 'skills')),
                     (os.path.join(self.c1, 'knowledge', 'asset-generation'),
                      os.path.join(self.old, 'knowledge', 'asset-generation')),
                     (os.path.join(self.c2, 'tools', 'machine_lock'), os.path.join(self.old, 'tools', 'machine_lock')),
                     (os.path.join(self.c2, 'tools', 'oldthing'), os.path.join(self.old, 'tools', 'usage'))):
            self.assertIn('leftover link %s -> %s: remove it with: cmd /c rmdir "%s"' % (p, t, p), out)
        self.assertIn('also loads worktree-start, which the kit has no skill for', out)
        c2skills = os.path.join(self.c2, 'skills')                         # follows account 1: skipped, not a leftover
        self.assertNotIn('leftover link %s ' % c2skills, out)
        self.assertIn('skipped link %s -> ' % c2skills, out)
        self.assertNotIn('/s', [w for l in out.splitlines() if 'rmdir' in l for w in l.split()])


@windows_only('directory junctions are Windows only')
class Leftovers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-nolink-')
        self.addCleanup(self._cleanup)
        self.kit, self.old, self.home = (os.path.join(self.root, n) for n in ('kit', 'old-kit', 'home'))
        self.acct = os.path.join(self.home, '.claude')
        for rel in ('boot/hook.py', 'optional/usage/statusline-usage.ps1', 'content/skills/land-branch/SKILL.md'):
            T.touch(os.path.join(self.kit, *rel.split('/')))
        os.makedirs(self.old)
        os.makedirs(os.path.join(self.acct, 'tools'))
        os.makedirs(os.path.join(self.acct, 'skills'))
        self.usage = os.path.join(self.acct, 'tools', 'usage')
        T.junction(self.usage, os.path.join(self.kit, 'optional', 'usage'))
        self.skill = os.path.join(self.acct, 'skills', 'land-branch')
        T.junction(self.skill, os.path.join(self.kit, 'content', 'skills', 'land-branch'))

    def _cleanup(self):
        for p in (self.usage, self.skill):
            if common.is_link(p):
                os.rmdir(p)
        shutil.rmtree(self.root, True)

    def ctx(self, fresh):
        return M.Context(self.home, self.kit, old_kit=None if fresh else self.old, config_dirs=[self.acct],
                         host=T.FakeHost(self.kit), today=T.TODAY)

    def test_leftover_link_is_reported_with_a_link_only_command(self):
        for fresh in (True, False):
            with self.subTest(fresh=fresh):
                plan = M.Plan(M.Plan.FRESH_SECTIONS if fresh else None)
                links.survey(plan, self.acct, self.ctx(fresh))
                self.assertEqual((plan.changes, plan.refusals), ([], []))
                want = [links.leftover(self.skill, os.path.join(self.kit, 'content', 'skills', 'land-branch')),
                        links.leftover(self.usage, os.path.join(self.kit, 'optional', 'usage'))]
                self.assertEqual(sorted(plan.notes['links']), sorted(want))
                self.assertIn('leftover link %s -> %s: remove it with: cmd /c rmdir "%s"'
                              % (self.usage, os.path.join(self.kit, 'optional', 'usage'), self.usage),
                              plan.notes['links'])
                self.assertTrue(common.is_link(self.usage))                 # reported, not removed

    def test_a_link_outside_the_kit_is_reported_as_skipped_never_silently(self):
        """The default mode has no older clone to match, so a link an earlier setup made into a kit
        clone that has moved since points outside ctx.kit: it is one "skipped link" line, never
        nothing. So is a container that is a link elsewhere (another account folder)."""
        tool = os.path.join(self.acct, 'tools', 'machine_lock')
        into = os.path.join(self.old, 'tools')
        os.makedirs(into)
        T.junction(tool, into)
        self.addCleanup(os.rmdir, tool)
        other = os.path.join(self.home, '.claude-two')
        os.makedirs(other)
        follows = os.path.join(other, 'skills')
        T.junction(follows, os.path.join(self.acct, 'skills'))
        self.addCleanup(os.rmdir, follows)
        for d, p, t in ((self.acct, tool, into), (other, follows, os.path.join(self.acct, 'skills'))):
            with self.subTest(link=p):
                plan = M.Plan(M.Plan.FRESH_SECTIONS)
                links.survey(plan, d, self.ctx(True))
                self.assertEqual((plan.changes, plan.refusals), ([], []))
                said = [n for n in plan.notes.get('links', []) if n.startswith('skipped link %s -> %s: ' % (p, t))]
                self.assertEqual(len(said), 1, plan.notes)
                self.assertIn('cmd /c rmdir "%s"' % p, said[0])
                self.assertTrue(common.is_link(p))                          # reported, not removed

    def symlink(self, target, link, is_dir):
        """A symlink (not a junction), or skip: making one needs the symlink privilege."""
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except OSError as e:
            self.skipTest('no symlink privilege on this machine: %s' % e)
        self.addCleanup(os.rmdir if is_dir else os.remove, link)

    def notes(self):
        plan = M.Plan(M.Plan.FRESH_SECTIONS)
        links.survey(plan, self.acct, self.ctx(True))
        self.assertEqual((plan.changes, plan.refusals), ([], []))
        return plan.notes['links']

    def test_a_file_symlink_is_removed_with_del_not_rmdir(self):
        """rmdir fails on a file symlink; del removes the link and leaves its target."""
        into = os.path.join(self.kit, 'boot', 'hook.py')
        outside = os.path.join(self.old, 'thing.py')
        T.touch(outside)
        mine = os.path.join(self.acct, 'tools', 'hook.py')
        other = os.path.join(self.acct, 'tools', 'thing.py')
        self.symlink(into, mine, False)
        self.symlink(outside, other, False)
        notes = self.notes()
        self.assertIn('leftover link %s -> %s: remove it with: cmd /c del "%s"' % (mine, into, mine), notes)
        self.assertIn(links.skipped(other, outside), notes)
        self.assertTrue(links.skipped(other, outside).endswith('cmd /c del "%s"' % other))
        self.assertIn(links.leftover(self.usage, os.path.join(self.kit, 'optional', 'usage')), notes)
        self.assertTrue(links.leftover(self.usage, '').endswith('cmd /c rmdir "%s"' % self.usage))  # a junction
        self.assertTrue(os.path.isfile(into) and os.path.isfile(outside))

    def test_a_relative_symlink_target_is_read_against_the_links_folder(self):
        """Read against the current directory, `..\\..\\..\\kit\\...` would land outside the kit."""
        link = os.path.join(self.acct, 'tools', 'comfy')
        into = os.path.join(self.kit, 'optional', 'usage')
        self.symlink(os.path.join('..', '..', '..', 'kit', 'optional', 'usage'), link, True)
        self.assertEqual(common.link_target(link), into)
        self.assertIn(links.leftover(link, into), self.notes())


class StatusLineThroughToolsLink(unittest.TestCase):
    """The live shape: every account's statusLine runs the usage script through
    `%USERPROFILE%/.claude/tools/usage`. Once the link goes that path is dead, so the settings step
    re-points it to the kit's file in both modes."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-nolink-sl-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.kit, self.old, self.home = (os.path.join(self.root, n) for n in ('kit', 'old-kit', 'home'))
        self.acct = os.path.join(self.home, '.claude')
        self.other = os.path.join(self.home, '.claude-two')
        for rel in ('boot/hook.py', 'optional/usage/statusline-usage.ps1', 'tools/machine_lock.py'):
            T.touch(os.path.join(self.kit, *rel.split('/')))
        for d in (self.old, self.acct, self.other):
            os.makedirs(d)
        self.path = os.path.join(self.other, 'settings.json')

    def ctx(self, fresh):
        return M.Context(self.home, self.kit, old_kit=None if fresh else self.old, config_dirs=[self.other],
                         host=T.FakeHost(self.kit), today=T.TODAY)

    def doc(self):
        guard = T.FakeHost(self.kit).guard_entry(self.kit)
        return {'hooks': dict(guard, SessionStart=[{'hooks': [
            {'type': 'command', 'command': T.PYW, 'args': ['%USERPROFILE%/.claude-two/tools/machine_lock/machine_lock.py',
                                                           'status']}]}]),
                'statusLine': {'type': 'command',
                               'command': 'powershell -NoProfile -File "%USERPROFILE%/.claude/tools/usage/statusline-usage.ps1"'}}

    def survey(self, fresh):
        plan = M.Plan(M.Plan.FRESH_SECTIONS if fresh else None)
        settings.survey(plan, self.other, self.ctx(fresh))
        return plan

    def test_statusline_through_a_tools_link_is_repointed(self):
        for fresh in (True, False):
            with self.subTest(fresh=fresh):
                T.touch(self.path, json.dumps(self.doc(), indent=2) + '\n')
                plan = self.survey(fresh)
                self.assertEqual(plan.refusals, [])
                self.assertEqual(len(plan.changes), 1, [c.text for c in plan.changes])
                self.assertIn('re-point the statusLine.command path %USERPROFILE%/.claude/tools/usage/'
                              'statusline-usage.ps1 (through the tools link usage)', plan.changes[0].text)
                plan.changes[0].do()
                with open(self.path, encoding='utf-8') as f:
                    data = json.load(f)
                self.assertEqual(data['statusLine']['command'], 'powershell -NoProfile -File "%s"'
                                 % T.fwd(os.path.join(self.kit, 'optional', 'usage', 'statusline-usage.ps1')))
                self.assertEqual(data['hooks']['SessionStart'][0]['hooks'][0]['args'],
                                 [T.fwd(os.path.join(self.kit, 'tools', 'machine_lock.py')), 'status'])
                again = self.survey(fresh)
                self.assertEqual((again.changes, again.refusals), ([], []))     # settled
                for n in os.listdir(self.other):
                    os.remove(os.path.join(self.other, n))                  # the .bak, for the next round

    def test_a_path_the_kit_cannot_serve_is_refused_in_both_modes(self):
        os.remove(os.path.join(self.kit, 'optional', 'usage', 'statusline-usage.ps1'))
        T.touch(self.path, json.dumps(self.doc(), indent=2) + '\n')
        for fresh in (True, False):
            plan = self.survey(fresh)
            self.assertEqual(plan.changes, [])
            self.assertEqual(len(plan.refusals), 1, plan.refusals)
            self.assertIn('through the tools link usage, and the kit has no', plan.refusals[0])

    def test_a_space_in_the_kit_path_quotes_an_unquoted_token(self):
        data = {'statusLine': {'command': 'powershell -File ~/.claude/tools/usage/statusline-usage.ps1'}}
        spaced = os.path.join(self.root, 'my kit')
        T.touch(os.path.join(spaced, 'optional', 'usage', 'statusline-usage.ps1'))
        ctx = M.Context(self.home, spaced, config_dirs=[self.acct], host=T.FakeHost(spaced), today=T.TODAY)
        self.assertEqual(len(settings.repoint_links(data, ctx)), 1)
        self.assertEqual(data['statusLine']['command'], 'powershell -File "%s"'
                         % T.fwd(os.path.join(spaced, 'optional', 'usage', 'statusline-usage.ps1')))


class FreshRun(F.FreshBase):
    def test_the_default_mode_reports_a_leftover_link(self):
        link = os.path.join(self.c1, 'tools', 'usage')
        os.makedirs(os.path.dirname(link))
        into = os.path.join(self.kit, 'boot')                          # any folder of the kit (it must exist)
        T.junction(link, into)
        self.addCleanup(os.rmdir, link)
        code, out = self.run_it()
        self.assertIn(links.leftover(link, into), out)
        self.assertNotIn('no links switched', out)
        self.assertTrue(common.is_link(link))


if __name__ == '__main__':
    unittest.main()
