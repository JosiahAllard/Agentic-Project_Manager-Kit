"""kitlib.machinesetup and tools/machine_setup.py (docs/machine-setup.md), against a fake home
folder, a fake old kit clone and a fake kit, all in a temp folder. The Windows side (shortcuts,
icacls, the user environment, guard.py's settings entry) is a FakeHost; nothing here reads or
writes the real home folder, desktop, environment or permissions.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import common, extras, settings
from kitlib.config import user as kuser

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'machine_setup.py')
TODAY = '20990101'
ME = 'HOST\\someone'


def fwd(p):
    return p.replace('\\', '/')


def junction(link, target):
    import _winapi
    _winapi.CreateJunction(target, link)


def readb(path):
    with open(path, 'rb') as f:
        return f.read()


def touch(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def acl_text(folder, inherited, extra=()):
    flag = '(I)' if inherited else ''
    rows = ['NT AUTHORITY\\SYSTEM:%s(OI)(CI)(F)' % flag, 'BUILTIN\\Administrators:%s(OI)(CI)(F)' % flag,
            '%s:%s(OI)(CI)(F)' % (ME, flag)]
    if inherited:
        rows.append('NT AUTHORITY\\Authenticated Users:(I)(OI)(CI)(M)')
    rows.extend(extra)                                               # explicit entries of others
    pad = ' ' * (len(folder) + 1)
    return '%s %s\n%s\n\nSuccessfully processed 1 files; Failed processing 0 files\n' % (
        folder, rows[0], '\n'.join(pad + r for r in rows[1:]))


class FakeHost(object):
    def __init__(self, kit):
        self.kit = kit
        self.shortcuts, self.env, self.calls = {}, {}, []
        self.acl_fixed = False
        self.acl_extra = []                                          # 'who:(rights)' explicit rows
        self.me = ME

    def desktop(self):
        raise AssertionError('the tests always pass a desktop')

    def read_shortcut(self, path):
        if not os.path.isfile(path):
            return None
        return dict(self.shortcuts.get(os.path.normcase(path)) or {'target': '', 'args': '', 'workdir': ''})

    def write_shortcut(self, path, target, args, workdir, description):
        self.calls.append(('shortcut', path))
        touch(path, 'lnk')
        self.shortcuts[os.path.normcase(path)] = {'target': target, 'args': args, 'workdir': workdir}

    def icacls(self, args):
        if len(args) == 1:
            return acl_text(args[0], not self.acl_fixed, self.acl_extra)
        self.calls.append(('icacls', list(args)))
        if args[1] in ('/remove:g', '/remove:d'):
            deny = args[1] == '/remove:d'
            self.acl_extra = [r for r in self.acl_extra
                              if not (r.split(':(')[0] == args[2].lstrip('*') and ('(DENY)' in r) == deny)]
        else:
            self.acl_fixed = True
        return 'processed file: %s\n' % args[0]

    def user(self):
        return self.me

    def env_get(self, name):
        return self.env.get(name)

    def env_set(self, name, value):
        self.calls.append(('env', name, value))
        self.env[name] = value

    def guard_entry(self, kit):
        return {'PreToolUse': [{'matcher': 'Bash|PowerShell', 'hooks': [
            {'type': 'command', 'command': 'pythonw', 'args': [fwd(os.path.join(kit, 'claude', 'hooks', 'guard.py'))]}]}]}


@unittest.skipUnless(sys.platform == 'win32', 'directory junctions are Windows only')
class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='machinesetup-')
        self.addCleanup(self._cleanup)
        self.home = os.path.join(self.root, 'home')
        self.old = os.path.join(self.root, 'old-kit')
        self.kit = os.path.join(self.root, 'kit')
        self.desk = os.path.join(self.root, 'desktop')
        self.temp = os.path.join(self.root, 'agent-temp')
        for d in (self.desk, self.temp):
            os.makedirs(d)
        # the old kit clone
        for rel in ('tools/machine_lock/machine_lock.py', 'tools/comfy/comfy_gen.py', 'tools/usage/statusline-usage.ps1', 'tools/heavy_run.py',
                    'hooks/no_root_search.py', 'hooks/main_checkout_redirect_guard.py', 'hooks/pm_session_start.py',
                    'hooks/conductor_session_start.py', 'hooks/mystery.py', 'knowledge/asset-generation/LOG.md',
                    'skills/land-branch/SKILL.md', 'skills/worktree-start/SKILL.md', 'agents/coordinator.md'):
            touch(os.path.join(self.old, *rel.split('/')))
        # the kit
        for rel in ('claude/hooks/guard.py', 'claude/hooks/pm_session_start.py',
                    'claude/hooks/coordinator_session_start.py', 'tools/machine_lock.py', 'kitlib/__init__.py',
                    'optional/comfy/comfy_gen.py', 'optional/procwatch/procwatch.py',
                    'optional/usage/statusline-usage.ps1', 'optional/usage/write-usage-status.ps1',
                    'optional/agenttemp/agent_temp_sweep.py', 'knowledge/asset-generation/README.md',
                    'claude/skills/land-branch/SKILL.md', 'reserve_ui.pyw'):
            touch(os.path.join(self.kit, *rel.split('/')))
        # account 1: every container a link into the old kit
        self.c1 = os.path.join(self.home, '.claude')
        os.makedirs(self.c1)
        junction(os.path.join(self.c1, 'tools'), os.path.join(self.old, 'tools'))
        junction(os.path.join(self.c1, 'skills'), os.path.join(self.old, 'skills'))
        junction(os.path.join(self.c1, 'agents'), os.path.join(self.old, 'agents'))
        os.makedirs(os.path.join(self.c1, 'knowledge'))
        junction(os.path.join(self.c1, 'knowledge', 'asset-generation'),
                 os.path.join(self.old, 'knowledge', 'asset-generation'))
        # account 2: real containers, one real folder where a link goes, one container following account 1
        self.c2 = os.path.join(self.home, '.claude-two')
        os.makedirs(os.path.join(self.c2, 'tools'))
        junction(os.path.join(self.c2, 'tools', 'machine_lock'), os.path.join(self.old, 'tools', 'machine_lock'))
        touch(os.path.join(self.c2, 'tools', 'comfy', 'local-notes.txt'), 'keep me\n')
        junction(os.path.join(self.c2, 'tools', 'oldthing'), os.path.join(self.old, 'tools', 'usage'))
        junction(os.path.join(self.c2, 'skills'), os.path.join(self.c1, 'skills'))
        os.makedirs(kuser.config_dir(self.home))
        self.s1 = os.path.join(self.c1, 'settings.json')
        self.s2 = os.path.join(self.c2, 'settings.json')
        self.write_settings(self.s1, self.settings_doc(), crlf=True)
        self.write_settings(self.s2, {'model': 'opus', 'hooks': {'SessionStart': [{'hooks': [
            {'type': 'command', 'command': 'python "%s"' % fwd(os.path.join(self.old, 'hooks', 'conductor_session_start.py'))}]}]}},
            indent=4)
        self.host = FakeHost(self.kit)

    def _cleanup(self):
        for dirpath, dirnames, _ in os.walk(self.root, topdown=True):
            for n in list(dirnames):
                p = os.path.join(dirpath, n)
                if common.is_link(p):
                    os.rmdir(p)                                      # the link only
                    dirnames.remove(n)
        shutil.rmtree(self.root, ignore_errors=True)

    def settings_doc(self):
        old = fwd(self.old)
        return {
            'permissions': {'allow': ['Bash(git status)'], 'note': 'café'},
            'hooks': {
                'PreToolUse': [
                    {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python %s/hooks/no_root_search.py' % old}]},
                    {'matcher': 'Bash', 'hooks': [
                        {'type': 'command', 'command': 'python', 'args': ['%s/hooks/main_checkout_redirect_guard.py' % old]},
                        {'type': 'command', 'command': 'echo keep-me'}]},
                ],
                'SessionStart': [{'hooks': [{'type': 'command', 'command': 'python "%s/hooks/pm_session_start.py"' % old}]}],
            },
            'statusLine': {'type': 'command',
                           'command': 'powershell -NoProfile -File "%s/tools/usage/statusline-usage.ps1"'
                                      % fwd(self.c1).replace(fwd(self.home), '%USERPROFILE%')},
        }

    def write_settings(self, path, doc, indent=2, crlf=False):
        text = json.dumps(doc, indent=indent, ensure_ascii=False) + '\n'
        if crlf:
            text = text.replace('\n', '\r\n')
        with open(path, 'wb') as f:
            f.write(text.encode('utf-8'))

    def ctx(self, **kw):
        kw.setdefault('config_dirs', None)
        return M.Context(self.home, self.kit, self.old, agent_temp=self.temp, desktop=self.desk,
                         host=self.host, today=TODAY, **kw)

    def run_it(self, apply=False, **kw):
        code, lines = M.run(self.ctx(**kw), apply=apply)
        return code, '\n'.join(lines)

    def snapshot(self):
        out = {}
        for dirpath, dirnames, files in os.walk(self.home):
            for n in list(dirnames):
                p = os.path.join(dirpath, n)
                if common.is_link(p):
                    out[p] = 'link:' + common.norm(common.link_target(p))
                    dirnames.remove(n)
                else:
                    out[p] = 'dir'
            for n in files:
                with open(os.path.join(dirpath, n), 'rb') as f:
                    out[os.path.join(dirpath, n)] = f.read()
        out['desktop'] = sorted(os.listdir(self.desk))
        out['host'] = (dict(self.host.env), list(self.host.calls), self.host.acl_fixed)
        return out

    def target(self, *parts):
        return common.norm(common.link_target(os.path.join(*parts)))

    def kitp(self, rel):
        return common.norm(os.path.join(self.kit, *rel.split('/')))


class DryRunApplyAgain(Base):
    def test_dry_run_changes_nothing_and_prints_each_change_and_diff(self):
        before = self.snapshot()
        code, out = self.run_it()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.snapshot(), before)
        self.assertIn('DRY RUN', out)
        self.assertIn('re-run with --apply', out)
        self.assertIn('replace the link', out)
        self.assertIn('a real folder: rename it to comfy.old-%s' % TODAY, out)
        diff = [l.strip() for l in out.splitlines()]
        self.assertIn('-' + ' ' * 12 + '"command": "python %s/hooks/no_root_search.py"' % fwd(self.old), diff)
        self.assertTrue(any(l.startswith('+') and 'claude/hooks/guard.py' in l for l in diff), out)
        self.assertIn('the kit has no skill worktree-start', out)
        self.assertIn('agents', out)
        self.assertIn('left as is', out)
        self.assertIn('icacls', out)
        self.assertIn('not carried over', out)
        self.assertIn('heavy_run.py', out)
        self.assertIn('%s -> ' % os.path.join(self.c2, 'tools', 'oldthing'), out)
        self.assertIn('KIT_MACHINE_LOCK', out)
        self.assertNotIn(os.path.join(self.c2, 'skills', 'land-branch'), out)   # follows account 1

    def test_apply_makes_every_change_and_a_second_run_has_nothing_to_do(self):
        s1_before = readb(self.s1)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('made ', out)
        # links
        self.assertFalse(common.is_link(os.path.join(self.c1, 'tools')))
        self.assertEqual(self.target(self.c1, 'tools', 'machine_lock'), self.kitp('tools'))
        self.assertEqual(self.target(self.c1, 'tools', 'kitlib'), self.kitp('kitlib'))
        self.assertEqual(self.target(self.c1, 'tools', 'usage'), self.kitp('optional/usage'))
        self.assertEqual(self.target(self.c1, 'skills', 'land-branch'), self.kitp('claude/skills/land-branch'))
        self.assertEqual(self.target(self.c1, 'skills', 'worktree-start'),     # no kit skill: kept
                         common.norm(os.path.join(self.old, 'skills', 'worktree-start')))
        self.assertEqual(self.target(self.c1, 'knowledge', 'asset-generation'), self.kitp('knowledge/asset-generation'))
        self.assertEqual(self.target(self.c1, 'agents'), common.norm(os.path.join(self.old, 'agents')))
        self.assertEqual(self.target(self.c2, 'tools', 'machine_lock'), self.kitp('tools'))
        self.assertEqual(self.target(self.c2, 'tools', 'comfy'), self.kitp('optional/comfy'))
        aside = os.path.join(self.c2, 'tools', 'comfy.old-' + TODAY, 'local-notes.txt')
        self.assertEqual(readb(aside).decode('utf-8').strip(), 'keep me')
        self.assertTrue(common.is_link(os.path.join(self.c2, 'skills')))
        self.assertTrue(os.path.isfile(os.path.join(self.c1, 'tools', 'machine_lock', 'machine_lock.py')))
        # the old kit is untouched
        self.assertTrue(os.path.isfile(os.path.join(self.old, 'tools', 'comfy', 'comfy_gen.py')))
        # settings
        bak = self.s1 + '.machine-setup-%s.bak' % TODAY
        self.assertEqual(readb(bak), s1_before)
        raw = readb(self.s1)
        self.assertIn(b'\r\n', raw)
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        doc = json.loads(raw.decode('utf-8'))
        text = json.dumps(doc)
        self.assertNotIn('no_root_search', text)
        self.assertNotIn('main_checkout_redirect_guard', text)
        self.assertIn('echo keep-me', text)
        self.assertEqual(doc['permissions'], self.settings_doc()['permissions'])
        pre = doc['hooks']['PreToolUse']
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[1]['hooks'][0]['args'], [fwd(os.path.join(self.kit, 'claude', 'hooks', 'guard.py'))])
        self.assertEqual(doc['hooks']['SessionStart'][0]['hooks'][0]['command'],
                         'python "%s"' % fwd(os.path.join(self.kit, 'claude', 'hooks', 'pm_session_start.py')))
        self.assertEqual(doc['statusLine'], self.settings_doc()['statusLine'])   # through tools/usage: kept
        doc2 = json.loads(readb(self.s2).decode('utf-8'))
        self.assertEqual(doc2['hooks']['SessionStart'][0]['hooks'][0]['command'],
                         'python "%s"' % fwd(os.path.join(self.kit, 'claude', 'hooks', 'coordinator_session_start.py')))
        self.assertTrue(readb(self.s2).decode('utf-8').startswith('{\n    "model"'))
        # extras
        lnk = os.path.join(self.desk, extras.SHORTCUT)
        self.assertEqual(self.host.shortcuts[os.path.normcase(lnk)]['args'],
                         '"%s"' % os.path.join(self.kit, 'reserve_ui.pyw'))
        self.assertEqual(self.host.env['KIT_MACHINE_LOCK'], os.path.join(self.kit, 'tools', 'machine_lock.py'))
        self.assertTrue(self.host.acl_fixed)
        icacls = [c for c in self.host.calls if c[0] == 'icacls'][0][1]
        self.assertEqual(icacls, [self.temp, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F',
                                  '*S-1-5-32-544:(OI)(CI)F', ME + ':(OI)(CI)F'])
        # again
        snap = self.snapshot()
        code, out = self.run_it(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('machine-setup: nothing to do', out)
        for s in ('links', 'settings', 'extras'):
            self.assertIn('%s: nothing to do' % s, out)
        self.assertEqual(self.snapshot(), snap)

    def test_settings_diff_shows_only_changed_lines(self):
        code, out = self.run_it(config_dirs=[self.c2])
        diff = [l.strip() for l in out.splitlines() if l.strip()[:1] in '+-' and not l.strip().startswith(('---', '+++'))]
        self.assertEqual(len([l for l in diff if l.startswith('-')]), 1, out)
        self.assertFalse([l for l in diff if '"model"' in l], out)   # unchanged, not in the diff


class Refusals(Base):
    def assertRefusedUnchanged(self, needle, **kw):
        before = self.snapshot()
        code, out = self.run_it(apply=True, **kw)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('REFUSED', out)
        self.assertIn(needle, out)
        self.assertIn('nothing changed', out)
        self.assertEqual(self.snapshot(), before)

    def test_an_old_kit_hook_with_no_kit_counterpart_refuses_and_is_named(self):
        doc = self.settings_doc()
        doc['hooks']['SessionStart'][0]['hooks'].append(
            {'type': 'command', 'command': 'python %s/hooks/mystery.py' % fwd(self.old)})
        self.write_settings(self.s1, doc)
        self.assertRefusedUnchanged('mystery.py')

    def test_a_repoint_target_missing_from_the_kit_refuses(self):
        os.remove(os.path.join(self.kit, 'claude', 'hooks', 'pm_session_start.py'))
        self.assertRefusedUnchanged('claude/hooks/pm_session_start.py')

    def test_a_path_through_a_tools_link_the_kit_cannot_serve_refuses(self):
        os.remove(os.path.join(self.kit, 'optional', 'usage', 'statusline-usage.ps1'))
        self.assertRefusedUnchanged('statusline-usage.ps1')

    def test_a_missing_link_target_in_the_kit_refuses(self):
        shutil.rmtree(os.path.join(self.kit, 'optional', 'procwatch'))
        self.assertRefusedUnchanged('optional/procwatch')

    def test_a_file_where_a_link_goes_refuses(self):
        touch(os.path.join(self.c2, 'tools', 'kitlib'))
        self.assertRefusedUnchanged('is a file where a link')

    def test_the_kit_as_its_own_old_kit_refuses(self):
        code, lines = M.run(M.Context(self.home, self.kit, self.kit, host=self.host, today=TODAY,
                                      desktop=self.desk), apply=True)
        self.assertEqual(code, M.EXIT_REFUSED)
        self.assertIn('the same folder', '\n'.join(lines))

    def test_a_folder_that_is_not_a_kit_refuses(self):
        os.remove(os.path.join(self.kit, 'claude', 'hooks', 'guard.py'))
        self.assertRefusedUnchanged('not a kit checkout')

    def test_a_missing_config_folder_refuses(self):
        self.assertRefusedUnchanged('no config folder', config_dirs=[os.path.join(self.home, '.claude-none')])

    def test_settings_changed_after_the_survey_stops_and_says_how_to_restore(self):
        ctx = self.ctx(config_dirs=[self.c2])
        plan = M.survey(ctx)
        change = [c for c in plan.changes if c.section == 'settings'][0]
        with open(self.s2, 'ab') as f:
            f.write(b' ')
        with self.assertRaises(RuntimeError):
            change.do()
        self.assertFalse(os.path.exists(self.s2 + '.machine-setup-%s.bak' % TODAY))


class Edges(Base):
    """Review fixes: what a write keeps, what it refuses, and the apply report."""

    def test_empty_groups_and_events_it_did_not_empty_are_kept(self):
        doc = self.settings_doc()
        doc['hooks']['PreToolUse'].append({'matcher': 'Read', 'hooks': []})
        doc['hooks']['Stop'] = []
        self.write_settings(self.s1, doc)
        code, out = self.run_it(apply=True, config_dirs=[self.c1])
        self.assertEqual(code, M.EXIT_OK, out)
        hooks = json.loads(readb(self.s1).decode('utf-8'))['hooks']
        self.assertEqual(hooks['Stop'], [])
        self.assertIn({'matcher': 'Read', 'hooks': []}, hooks['PreToolUse'])
        self.assertNotIn('no_root_search', json.dumps(hooks))           # the group it emptied went

    def test_a_hard_linked_settings_file_refuses(self):
        other = os.path.join(self.root, 'shared-settings.json')
        os.link(self.s2, other)
        before = readb(other)
        code, out = self.run_it(apply=True, config_dirs=[self.c2])
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('a hard link (2 names)', out)
        self.assertEqual(readb(other), before)

    def test_a_settings_file_linked_after_the_survey_is_not_replaced(self):
        plan = M.survey(self.ctx(config_dirs=[self.c2]))
        change = [c for c in plan.changes if c.section == 'settings'][0]
        os.link(self.s2, os.path.join(self.root, 'shared-settings.json'))
        with self.assertRaisesRegex(RuntimeError, 'hard link'):
            change.do()

    def test_a_symlinked_settings_file_refuses(self):
        real = os.path.join(self.root, 'real-settings.json')
        os.replace(self.s2, real)
        try:
            os.symlink(real, self.s2)
        except (OSError, NotImplementedError) as e:
            self.skipTest('no symlink privilege: %s' % e)
        code, out = self.run_it(apply=True, config_dirs=[self.c2])
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('a symbolic link', out)
        self.assertTrue(os.path.islink(self.s2))

    def test_a_hooks_event_that_is_not_a_list_refuses_cleanly(self):
        self.write_settings(self.s2, {'hooks': {'SessionStart': {'oops': 1}}})
        code, out = self.run_it(apply=True, config_dirs=[self.c2])
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('hooks.SessionStart is not a list', out)

    def test_a_hook_path_it_cannot_resolve_refuses(self):
        for cmd in ('python hooks/x.py', 'python x.py', 'python %OTHER%/x.py', 'python ${OTHER}/x.py'):
            self.write_settings(self.s2, {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': cmd}]}]}})
            code, out = self.run_it(apply=True, config_dirs=[self.c2])
            self.assertEqual(code, M.EXIT_REFUSED, cmd + '\n' + out)
            self.assertIn('cannot resolve', out)

    def test_words_switches_and_project_paths_are_not_refused(self):
        for cmd in ('cmd /c echo hi', 'python "$CLAUDE_PROJECT_DIR/.claude/hooks/x.py"',
                    'python ${CLAUDE_PLUGIN_ROOT}/x.py', 'echo keep-me'):
            self.write_settings(self.s2, {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': cmd}]}]}})
            code, out = self.run_it(config_dirs=[self.c2])
            self.assertEqual(code, M.EXIT_OK, cmd + '\n' + out)

    def test_other_explicit_acl_entries_are_removed_and_the_check_then_holds(self):
        self.host.acl_extra = ['HOST\\guest:(OI)(CI)(M)', 'S-1-5-21-1-2-3-1001:(OI)(CI)(RX)',
                               'NT AUTHORITY\\SYSTEM:(DENY)(W)']
        code, out = self.run_it(config_dirs=[self.c2])
        self.assertIn('other explicit entries removed (HOST\\guest, *S-1-5-21-1-2-3-1001, NT AUTHORITY\\SYSTEM)', out)
        code, out = self.run_it(apply=True, config_dirs=[self.c2])
        self.assertEqual(code, M.EXIT_OK, out)
        calls = [c[1][1:] for c in self.host.calls if c[0] == 'icacls']
        self.assertIn(['/remove:g', 'HOST\\guest'], calls)
        self.assertIn(['/remove:g', '*S-1-5-21-1-2-3-1001'], calls)
        self.assertIn(['/remove:d', 'NT AUTHORITY\\SYSTEM'], calls)
        code, out = self.run_it(config_dirs=[self.c2])
        self.assertIn('extras: nothing to do', out)

    def test_an_empty_user_domain_refuses(self):
        self.host.me = '\\someone'
        code, out = self.run_it(config_dirs=[self.c2])
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('USERDOMAIN or USERNAME is empty', out)

    def test_any_failure_part_way_reports_done_and_restore_as_it_goes(self):
        def boom(name, value):
            raise KeyError('registry')
        self.host.env_set = boom
        seen = []
        code, lines = M.run(self.ctx(config_dirs=[self.c2]), apply=True, out=seen.append)
        self.assertEqual(code, M.EXIT_REFUSED)
        self.assertEqual(seen, lines)
        text = '\n'.join(seen)
        self.assertIn('STOPPED at: user environment KIT_MACHINE_LOCK', text)
        self.assertIn('error: KeyError', text)
        self.assertIn('restore: [Environment]::SetEnvironmentVariable', text)
        self.assertIn('already done by this run (', text)
        self.assertIn('Machine schedule.lnk', text.split('already done by this run')[1])

    def test_a_config_folder_listed_twice_through_a_link_is_read_once(self):
        alias = os.path.join(self.home, '.claude-alias')
        junction(alias, self.c1)
        self.assertEqual(self.ctx(config_dirs=[self.c1, alias, self.c1]).config_dirs, [self.c1])
        self.assertNotIn(alias, self.ctx().config_dirs)


class Parts(unittest.TestCase):
    def test_default_config_dirs_skip_the_kit_machine_folder_and_files(self):
        root = tempfile.mkdtemp(prefix='machinesetup-')
        self.addCleanup(shutil.rmtree, root, True)
        for n in ('.claude', '.claude-two', kuser.DIR_NAME, 'other'):
            os.makedirs(os.path.join(root, n))
        touch(os.path.join(root, '.claude-file'))
        self.assertEqual([os.path.basename(d) for d in M.default_config_dirs(root)], ['.claude', '.claude-two'])

    def test_layout_round_trips_indent_ascii_bom_and_final_newline(self):
        for indent, ascii_, bom, final in ((4, True, True, False), (2, False, False, True), ('\t', False, False, True)):
            text = json.dumps({'a': ['café', {'b': 1}]}, indent=indent, ensure_ascii=ascii_) + ('\n' if final else '')
            raw = (b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8')
            layout, data = settings.read(raw)
            self.assertTrue(layout['exact'])
            self.assertEqual(settings.write(layout, data), raw)

    def test_expand_reads_home_forms_and_ignores_words(self):
        home = os.path.join(tempfile.gettempdir(), 'h')
        want = os.path.normpath(os.path.join(home, '.claude', 'x.py'))
        for tok in ('~/.claude/x.py', '%USERPROFILE%\\.claude\\x.py', '%userprofile%/.claude/x.py', '$HOME/.claude/x.py',
                    '${HOME}/.claude/x.py', '${USERPROFILE}/.claude/x.py', '$env:USERPROFILE\\.claude\\x.py',
                    '$env:HOME/.claude/x.py', '%HOMEDRIVE%%HOMEPATH%\\.claude\\x.py',
                    '$env:HOMEDRIVE$env:HOMEPATH/.claude/x.py', '%HOMEPATH%/.claude/x.py', '$env:HOMEPATH/.claude/x.py'):
            self.assertEqual(settings.expand(tok, home), want, tok)
        self.assertEqual(settings.expand('$HOMEDIR/x.py', home), '')     # another variable
        for tok in ('python', '-File', 'x.py', '"'):
            self.assertEqual(settings.expand(tok, home), '')

    @unittest.skipUnless(sys.platform == 'win32', 'directory junctions are Windows only')
    def test_is_link_finds_a_junction_without_os_path_isjunction(self):
        """os.path.isjunction is Python 3.12+; the reparse-tag fallback serves older ones."""
        root = tempfile.mkdtemp(prefix='machinesetup-')
        self.addCleanup(shutil.rmtree, root, True)
        target, link = os.path.join(root, 't'), os.path.join(root, 'j')
        os.makedirs(target)
        junction(link, target)
        self.addCleanup(os.rmdir, link)
        saved = os.path.__dict__.pop('isjunction', None)
        try:
            self.assertFalse(hasattr(os.path, 'isjunction'))
            self.assertTrue(common.is_link(link))
            self.assertFalse(common.is_link(target))
            self.assertFalse(common.is_link(os.path.join(root, 'none')))
        finally:
            if saved is not None:
                os.path.isjunction = saved

    def test_acl_done_reads_icacls(self):
        folder = os.path.join(tempfile.gettempdir(), 'agent-temp')
        self.assertTrue(extras.acl_done(acl_text(folder, False), folder, ME))
        self.assertFalse(extras.acl_done(acl_text(folder, True), folder, ME))
        self.assertFalse(extras.acl_done(acl_text(folder, False), folder, 'HOST\\other'))

    def test_real_guard_entry_shape(self):
        entry = extras.Host().guard_entry(KIT)
        args = entry['PreToolUse'][0]['hooks'][0]['args']
        self.assertEqual(common.norm(args[0]), common.norm(os.path.join(KIT, 'claude', 'hooks', 'guard.py')))


@unittest.skipUnless(sys.platform == 'win32', 'directory junctions are Windows only')
class Tool(Base):
    def test_old_kit_is_required(self):
        r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        r = subprocess.run([sys.executable, TOOL, '--old-kit', os.path.join(self.root, 'none')],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE)
        self.assertIn('no old kit folder', r.stderr)

    def test_the_powershell_wrapper_passes_its_flags_and_exit_codes(self):
        """installer/machine-setup.ps1: a refusal that stops before the machine is read (a missing
        config folder) proves -OldKit, -HomeDir and -ConfigDir reach the tool; a missing old kit is 2."""
        ps1 = os.path.join(KIT, 'installer', 'machine-setup.ps1')
        missing = os.path.join(self.home, '.claude-none')
        cmd = ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1]
        r = subprocess.run(cmd + ['-OldKit', self.old, '-HomeDir', self.home, '-ConfigDir', missing],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
        self.assertIn('REFUSED - no config folder at %s' % missing, r.stdout)
        self.assertIn('DRY RUN', r.stdout)
        r = subprocess.run(cmd + ['-OldKit', os.path.join(self.root, 'none')], capture_output=True, text=True)
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)

    def ps1(self, args, env=None):
        ps = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                          'powershell.exe')
        ps1 = os.path.join(KIT, 'installer', 'machine-setup.ps1')
        return subprocess.run([ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ps1] + args,
                              capture_output=True, text=True, env=env)

    def test_the_wrapper_splits_config_dirs_and_passes_apply(self):
        """-ConfigDir a,b under -File arrives as one string; both folders must reach the tool. Two
        missing folders refuse before anything is read, so -Apply changes nothing here."""
        m1, m2 = os.path.join(self.home, '.claude-none1'), os.path.join(self.home, '.claude-none2')
        r = self.ps1(['-OldKit', self.old, '-HomeDir', self.home, '-ConfigDir', '%s,%s' % (m1, m2), '-Apply'])
        self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
        self.assertIn('machine-setup: APPLY', r.stdout)
        self.assertIn('REFUSED - no config folder at %s' % m1, r.stdout)
        self.assertIn('REFUSED - no config folder at %s' % m2, r.stdout)

    def test_the_wrapper_without_old_kit_or_python_is_usage(self):
        r = self.ps1([])
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('-OldKit', r.stdout)
        sysdir = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32')
        env = dict(os.environ, PATH=sysdir + ';' + os.path.join(sysdir, 'WindowsPowerShell', 'v1.0'))
        r = self.ps1(['-OldKit', self.old], env=env)
        self.assertEqual(r.returncode, M.EXIT_USAGE, r.stdout + r.stderr)
        self.assertIn('no python on PATH', r.stdout)

    def test_a_path_the_console_cannot_spell_prints_replaced(self):
        missing = os.path.join(self.home, '.claude-\u0436')
        env = dict(os.environ, PYTHONIOENCODING='cp1252')
        r = subprocess.run([sys.executable, TOOL, '--old-kit', self.old, '--home', self.home, '--config-dir', missing],
                           capture_output=True, env=env)
        self.assertEqual(r.returncode, M.EXIT_REFUSED, r.stdout + r.stderr)
        self.assertIn(b'REFUSED - no config folder at', r.stdout)
        self.assertNotIn(b'Traceback', r.stderr)

    def test_main_dry_run_then_apply(self):
        sys.path.insert(0, os.path.dirname(TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(TOOL))
        import machine_setup
        argv = ['--old-kit', self.old, '--kit', self.kit, '--home', self.home, '--agent-temp', self.temp,
                '--desktop', self.desk]
        with mock.patch.object(extras, 'Host', lambda: self.host):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(machine_setup.main(argv), 0)
            self.assertIn('change(s) to make', buf.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine_setup.main(argv + ['--apply']), 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(machine_setup.main(argv), 0)
            self.assertIn('machine-setup: nothing to do', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
