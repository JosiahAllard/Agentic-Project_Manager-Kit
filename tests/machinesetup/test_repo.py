"""The committed repo setup, kitlib.machinesetup.repo (`tools/machine_setup.py --repo PATH`, and
installer/new-repo.ps1, its PowerShell front end; docs/install.md "Committed setup (--repo)"),
against a throwaway repository (tests/gitfixture.py), never a real one: its dry run, its writes
and its refusals. Fake identities only (example.invalid). The wrapper's own part (argument
passing, the PowerShell location, the console encoding) is tests/stubs/test_new_repo.py, Windows."""
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import config, stubs
from kitlib.machinesetup import repo as R
from tests import gitfixture

KIT = stubs.KIT_ROOT
TOOL = os.path.join(KIT, 'tools', 'machine_setup.py')
OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


class Base(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.root = os.path.realpath(self.fx.root)
        self.kit = KIT

    def run_it(self, apply=False, target=None, owner=OWNER, agent=AGENT, **kw):
        ctx = R.RepoContext(target or self.root, owner, agent, self.kit, **kw)
        code, lines = R.run(ctx, apply=apply)
        return code, '\n'.join(lines)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as fh:
            return fh.read()

    def kit_copy(self):
        """A copy of the kit's templates, tools and kitlib beside the repository, so a test can
        change a template without touching this kit."""
        kit = os.path.join(os.path.realpath(self.fx.base), 'kitcopy')
        for name in ('tools', 'kitlib', 'templates'):
            shutil.copytree(os.path.join(KIT, name), os.path.join(kit, name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(os.path.join(KIT, 'hosts.json'), kit)
        self.kit = kit
        return kit


class DryRunAndApply(Base):
    def test_the_dry_run_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        code, out = self.run_it(stubs=True)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('DRY RUN', out)
        self.assertIn('"email": "%s"' % OWNER, out)
        self.assertIn('would write  %s' % os.path.join(self.root, 'tools', 'landed.py'), out)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_no_stubs_without_the_switch(self):
        """Decision 21 (docs/multi-host-plan.md section 7): a repository holds no stubs unless asked."""
        code, out = self.run_it()
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('none (skills and roles run', out)
        self.assertNotIn('would write  %s' % os.path.join(self.root, 'tools', 'landed.py'), out)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')), out)
        self.assertIn('new-repo: wrote .kit.json and the starter files', out)
        self.assertNotIn('kit_stubs', out)

    def test_apply_writes_the_config_and_the_stubs(self):
        code, out = self.run_it(apply=True, stubs=True, main_branch='master')
        self.assertEqual(code, R.EXIT_OK, out)
        with open(os.path.join(self.root, '.kit.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertEqual(data, {'repo': {'main_branch': 'master', 'name': 'repo'},
                                'owner': {'email': OWNER, 'agent_emails': [AGENT]}})
        got = stubs.run(self.root, 'check')
        self.assertEqual(got.code, stubs.OK, got.lines)
        for name in ('land', 'ledger', 'gate', 'preflight', 'new_worktree'):
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'tools', name + '.py')), name)
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('.kit.json exists already', out)

    def test_repo_name_is_written_to_kit_json(self):
        code, out = self.run_it(repo_name='pm')
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('"repo": {"main_branch": "main", "name": "pm"}', out)
        code, out = self.run_it(apply=True, repo_name='pm')
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertEqual(config.load(self.root).section('repo')['name'], 'pm')

    def test_an_upper_case_folder_name_is_lower_cased(self):
        named = os.path.join(os.path.realpath(self.fx.base), 'MyRepo')
        os.makedirs(named)
        self.fx.git('init', '-q', '-b', 'main', named, cwd=named)
        code, out = self.run_it(target=named)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('"name": "myrepo"', out)

    def test_apply_writes_the_ignore_files_the_index_and_the_next_steps(self):
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('.claude/worktrees/', self.read('.gitignore').splitlines())
        self.assertIn('.codex/worktrees/', self.read('.gitignore').splitlines())
        self.assertIn('* text=auto', self.read('.gitattributes').splitlines())
        self.assertIn('docs/INDEX.md', self.read('docs/INDEX.md'))
        for text in ('core.hooksPath', 'source: auto', 'gate.steps', 'opts the repository into the doc index'):
            self.assertIn(text, out)

    def test_an_existing_ignore_file_gets_only_its_missing_lines(self):
        self.fx.write('.gitignore', 'build/\n.claude/worktrees/')
        self.fx.write('.gitattributes', '*   text=auto\n*.sh text eol=lf\n')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        lines = self.read('.gitignore').splitlines()
        self.assertEqual(lines[:3], ['build/', '.claude/worktrees/', R.INSTALLER_MARK])
        self.assertEqual(lines.count('.claude/worktrees/'), 1)
        self.assertEqual(lines.count('.codex/worktrees/'), 1)
        self.assertIn('.claude/settings.local.json', lines)
        self.assertEqual(self.read('.gitattributes'), '*   text=auto\n*.sh text eol=lf\n')
        self.assertIn('unchanged', out)

    def test_an_existing_index_is_kept(self):
        self.fx.write('docs/INDEX.md', '# mine\n')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertEqual(self.read('docs/INDEX.md'), '# mine\n')

    def test_a_missing_origin_is_a_warning_and_a_present_one_is_not(self):
        code, out = self.run_it()
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('WARNING:  no remote named origin', out)
        self.assertIn('remote add origin', out)
        self.fx.git('remote', 'add', 'origin', 'https://example.invalid/r.git')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('remote:   origin -> https://example.invalid/r.git', out)
        self.assertNotIn('WARNING', out)

    def test_tracked_md_files_the_starter_index_misses_are_listed_not_refused(self):
        self.fx.write('README.md', '# r\n')
        self.fx.write('docs/guide.md', '# g\n')
        self.fx.write('déjà vu.md', '# d\n')
        self.fx.git('add', '--', 'README.md', 'docs/guide.md', 'déjà vu.md')
        code, out = self.run_it()
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('add these to docs/INDEX.md', out)
        for name in ('README.md', 'docs/guide.md', 'déjà vu.md'):
            self.assertIn('    ' + name, out.splitlines())
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_OK, out)
        os.remove(os.path.join(self.root, '.kit.json'))
        code, out = self.run_it()
        self.assertNotIn('add these', out)      # the index exists now: it is the owner's to keep


class Stacks(Base):
    def test_stack_writes_the_gate_section_the_lists_and_loads(self):
        code, out = self.run_it(apply=True, stubs=True, stack='python')
        self.assertEqual(code, R.EXIT_OK, out)
        cfg = config.load(self.root)
        self.assertEqual(cfg.section_file('gate'), 'tools/gate.json')
        self.assertEqual([st['name'] for st in cfg.section('gate')['steps']], ['unit-tests'])
        self.assertIn('__pycache__/', self.read('.gitignore').splitlines())
        self.assertIn('*.pyc        binary', self.read('.gitattributes').splitlines())
        got = stubs.run(self.root, 'check')
        self.assertEqual(got.code, stubs.OK, got.lines)
        self.assertIn("'Ran N tests'", out)

    def test_append_keeps_crlf_and_stack_comments_are_not_missing_lines(self):
        path = os.path.join(self.root, '.gitattributes')
        with open(path, 'wb') as fh:
            fh.write(b'* text=auto\r\n*.sh text eol=lf\r\n')
        code, out = self.run_it(stack='dotnet')
        self.assertEqual(code, R.EXIT_OK, out)
        append = [ln for ln in out.splitlines() if 'would append' in ln and 'gitattributes' in ln]
        self.assertEqual(len(append), 1, out)
        self.assertNotIn('Visual Studio', append[0])
        code, out = self.run_it(apply=True, stack='dotnet')
        self.assertEqual(code, R.EXIT_OK, out)
        with open(path, 'rb') as fh:
            raw = fh.read()
        self.assertIn(b'*.cs         text eol=lf\r\n', raw)
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), raw)

    def test_stack_refusals_write_nothing(self):
        code, out = self.run_it(stack='cobol')
        self.assertEqual(code, R.EXIT_USAGE, out)
        self.assertIn('not a stack the kit has', out)
        self.fx.write('tools/gate.json', '{}\n')
        code, out = self.run_it(apply=True, stack='python')
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('gate.json exists already', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
        self.assertEqual(self.read('tools/gate.json'), '{}\n')

    def test_godot_copies_its_tools_files_keeps_an_existing_one_and_names_its_pass_string(self):
        self.fx.write('tools/godot.cmd', 'rem mine\n')
        code, out = self.run_it(apply=True, stack='godot')
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertEqual(self.read('tools/godot.cmd'), 'rem mine\n')
        self.assertIn('kept', out)
        self.assertIn('UNIT TESTS OK', out)
        self.assertNotIn('Ran N tests', out)
        for rel in ('tools/godot.cmd', '.kit.json', 'tools/gate.json'):
            os.remove(os.path.join(self.root, *rel.split('/')))
        shutil.rmtree(os.path.join(self.root, '.claude'))
        code, out = self.run_it(apply=True, stack='godot')
        self.assertEqual(code, R.EXIT_OK, out)
        src = os.path.join(KIT, 'templates', 'stacks', 'godot', 'tools', 'godot.cmd')
        with open(src, 'rb') as a, open(os.path.join(self.root, 'tools', 'godot.cmd'), 'rb') as b:
            self.assertEqual(a.read(), b.read())
        self.assertIn('wrote    %s' % os.path.join(self.root, 'tools', 'godot.cmd'), out)
        self.assertIn('.gitattributes, docs/INDEX.md and tools/godot.cmd as usual', out)

    def test_a_godot_dry_run_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        code, out = self.run_it(stack='godot')
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('would create  %s' % os.path.join(self.root, 'tools', 'godot.cmd'), out)
        skill = os.path.join(self.root, '.claude', 'skills', 'godot-render-check', 'SKILL.md')
        self.assertIn('would create  %s' % skill, out)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_godot_writes_its_skill_with_the_kit_path(self):
        code, out = self.run_it(apply=True, stack='godot')
        self.assertEqual(code, R.EXIT_OK, out)
        skill = os.path.join(self.root, '.claude', 'skills', 'godot-render-check', 'SKILL.md')
        with open(skill, encoding='utf-8') as fh:
            text = fh.read()
        self.assertNotIn('{kit_root}', text)
        self.assertIn(os.path.normpath(KIT).replace('\\', '/') + '/tools/machine_lock.py', text)
        self.assertIn('wrote    %s' % skill, out)

    def test_an_unresolvable_kit_root_refuses_before_anything_is_written(self):
        kit = self.kit_copy()
        os.remove(os.path.join(kit, 'tools', 'machine_lock.py'))
        for apply in (False, True):
            code, out = self.run_it(apply=apply, stack='godot')
            self.assertEqual(code, R.EXIT_REFUSED, out)
            self.assertIn('stack_skill refused', out)
            self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
            self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))
            self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))

    def test_a_stack_tools_file_named_like_a_stub_refuses_at_plan_time(self):
        kit = self.kit_copy()
        stack = os.path.join(kit, 'templates', 'stacks', 'clash')
        os.makedirs(os.path.join(stack, 'tools'))
        shutil.copy(os.path.join(kit, 'templates', 'stacks', 'python', 'stack.json'), stack)
        shutil.copy(os.path.join(kit, 'templates', 'stacks', 'python', 'tools', 'gate.json'),
                    os.path.join(stack, 'tools'))
        with open(os.path.join(stack, 'tools', 'Landed.py'), 'w', encoding='utf-8') as fh:
            fh.write("print('not a stub')\n")
        before = sorted(os.listdir(self.root))
        for apply in (False, True):
            code, out = self.run_it(apply=apply, stubs=True, stack='clash')
            self.assertEqual(code, R.EXIT_REFUSED, out)
            self.assertIn("REFUSED - the clash stack's tools file Landed.py has the name of a kit tool stub", out)
            self.assertEqual(sorted(os.listdir(self.root)), before)


class Refusals(Base):
    def test_identities(self):
        code, out = self.run_it(owner=OWNER, agent=OWNER.upper())
        self.assertEqual(code, R.EXIT_USAGE, out)
        self.assertIn('are the same', out)
        code, out = self.run_it(owner='not-an-email')
        self.assertEqual(code, R.EXIT_USAGE, out)
        self.assertIn("--owner-email 'not-an-email' is not an email address", out)
        code, out = self.run_it(owner='')
        self.assertEqual(code, R.EXIT_USAGE, out)

    def test_where(self):
        sub = os.path.join(self.root, 'sub')
        os.makedirs(sub)
        code, out = self.run_it(apply=True, target=sub)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('name its top level', out)
        plain = os.path.join(os.path.realpath(self.fx.base), 'plain')
        os.makedirs(plain)
        code, out = self.run_it(apply=True, target=plain)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('is not a git checkout', out)
        code, out = self.run_it(apply=True, target=os.path.join(KIT, 'docs'))
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('inside the kit itself', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def test_a_repo_name_that_is_not_a_repo_name_is_usage_and_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        for bad in ('PM', 'my_repo', 'a.b'):
            code, out = self.run_it(apply=True, repo_name=bad)
            self.assertEqual(code, R.EXIT_USAGE, (bad, out))
            self.assertIn('is not a lower-case repo name', out)
        odd = os.path.join(os.path.realpath(self.fx.base), 'Odd_Name')
        os.makedirs(odd)
        self.fx.git('init', '-q', '-b', 'main', odd, cwd=odd)
        code, out = self.run_it(apply=True, target=odd)
        self.assertEqual(code, R.EXIT_USAGE, out)
        self.assertIn("the folder name 'odd_name' is not a lower-case repo name", out)
        self.assertIn('give --repo-name', out)
        self.assertFalse(os.path.exists(os.path.join(odd, '.kit.json')))
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_a_file_where_a_stub_goes_refuses_before_anything_is_written(self):
        self.fx.write('tools/landed.py', "print('own copy')\n")
        code, out = self.run_it(apply=True, stubs=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('not a stub (kept', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def test_an_unreadable_template_refuses_before_any_write(self):
        for template, kw in ((('stacks', 'python', 'tools', 'gate.json'), {'stack': 'python'}),
                             (('stacks', 'python', 'stack.json'), {'stack': 'python'}),
                             (('common', 'INDEX.md'), {}), (('common', '.gitignore'), {}),
                             (('common', 'gitattributes'), {})):
            path = os.path.join(KIT, 'templates', *template)
            before = sorted(os.listdir(self.root))
            real_open = open

            def failing(p, *a, **k):
                if os.path.normcase(os.path.abspath(str(p))) == os.path.normcase(path):
                    raise PermissionError(13, 'Permission denied', p)
                return real_open(p, *a, **k)
            for apply in (False, True):
                with mock.patch('builtins.open', failing):
                    code, out = self.run_it(apply=apply, **kw)
                self.assertEqual(code, R.EXIT_REFUSED, (template, out))
                self.assertIn('new-repo: REFUSED - cannot read %s' % path, out)
                self.assertNotIn('wrote', out)
                self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_private_remote_without_pm_data_is_usage(self):
        code, out = self.run_it(apply=True, private_remote='/x/private.git')
        self.assertEqual(code, R.EXIT_USAGE, out)
        self.assertIn('--private-remote needs --pm-data', out)


class FailedWrites(Base):
    """A file that cannot be written: read-only, and an open for writing that fails (root writes
    a read-only file elsewhere than Windows)."""

    def setUp(self):
        Base.setUp(self)
        self.locked = set()
        real_open = open

        def guarded(p, mode='r', *a, **k):
            if any(c in mode for c in 'wax+') and os.path.normcase(os.path.abspath(str(p))) in self.locked:
                raise PermissionError(13, 'Permission denied', p)
            return real_open(p, mode, *a, **k)
        patch = mock.patch('builtins.open', guarded)
        patch.start()
        self.addCleanup(patch.stop)

    def readonly(self, rel, text):
        path = self.fx.write(rel, text)
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(os.chmod, path, stat.S_IWRITE | stat.S_IREAD)
        self.locked.add(os.path.normcase(os.path.abspath(path)))
        return path

    def test_a_failed_write_stops_with_exit_1_and_never_says_wrote(self):
        path = self.readonly('.gitignore', 'build/\n')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('new-repo: FAILED - cannot write %s' % path, out)
        self.assertIn('already written', out)
        self.assertNotIn('new-repo: wrote', out)
        self.assertNotIn('appended %s' % path, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'docs', 'INDEX.md')))

    def test_a_failed_write_names_what_was_written_and_the_target_that_failed(self):
        path = self.readonly('.gitignore', 'build/\n')
        code, out = self.run_it(apply=True, stubs=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        undo = [ln for ln in out.splitlines() if 'already written' in ln]
        self.assertEqual(len(undo), 1, out)
        self.assertIn(os.path.join(self.root, '.kit.json'), undo[0])
        self.assertIn('(and the stubs listed above)', undo[0])
        self.assertNotIn(path, undo[0])
        self.assertIn('%s may be partly written' % path, out)

    def test_a_later_failure_does_not_tell_the_user_to_delete_an_appended_file(self):
        gitignore = self.fx.write('.gitignore', 'build/\n')
        attrs = self.readonly('.gitattributes', '* text=auto\n')
        code, out = self.run_it(apply=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('new-repo: FAILED - cannot write %s' % attrs, out)
        self.assertIn('.claude/worktrees/', self.read('.gitignore'))
        undo = [ln for ln in out.splitlines() if 'already written' in ln]
        self.assertEqual(len(undo), 1, out)
        self.assertNotIn(gitignore, undo[0])
        self.assertIn("appended to %s : remove the '# Added by the kit installer' block to undo" % gitignore, out)


class Stubs(Base):
    def test_a_stub_write_failure_names_the_skill_files_in_the_undo_list(self):
        real = stubs.run

        def run(root, verb, dry_run=False, **kw):
            if dry_run:
                return real(root, verb, dry_run=True, **kw)
            return stubs.Outcome(stubs.PROBLEMS, ['a folder (kept)  %s' % os.path.join(root, 'tools', 'x.py')])
        with mock.patch.object(stubs, 'run', run):
            code, out = self.run_it(apply=True, stubs=True, stack='godot')
        self.assertEqual(code, R.EXIT_REFUSED, out)
        skill = os.path.join(self.root, '.claude', 'skills', 'godot-render-check', 'SKILL.md')
        undo = [l for l in out.splitlines() if 'To undo: delete' in l]
        self.assertEqual(len(undo), 1, out)
        self.assertIn(skill, undo[0])
        self.assertIn(skill, [l for l in out.splitlines() if 'STOPPED' in l][0])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'docs', 'INDEX.md')))

    def test_a_refused_stub_plan_refuses_after_printing_it(self):
        with mock.patch.object(stubs, 'run', lambda *a, **k: stubs.Outcome(stubs.CONFIG, ['kit_stubs: refused: x'])):
            code, out = self.run_it(stubs=True)
        self.assertEqual(code, R.EXIT_REFUSED, out)
        self.assertIn('  kit_stubs: refused: x', out)
        self.assertIn('REFUSED - kitlib.stubs refused the stubs above (exit 2); nothing was written', out)


class PmData(Base):
    """--pm-data: the kit's tools/pm_data.py runs as a child after the files; a copy of the kit
    with a stand-in pm_data.py, so nothing is made in any repository."""

    def pm_tool(self, body):
        kit = self.kit if self.kit != KIT else self.kit_copy()
        with open(os.path.join(kit, 'tools', 'pm_data.py'), 'w', encoding='utf-8') as fh:
            fh.write(body)
        return kit

    def test_the_dry_run_prints_the_command_and_runs_nothing(self):
        kit = self.pm_tool("raise SystemExit('ran')\n")
        data = os.path.join(os.path.realpath(self.fx.base), "pm data's")
        code, out = self.run_it(pm_data=data, private_remote='/srv/private.git/')
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn("would run after the files above:  python %s -C %s init --path '%s' --url %s"
                      % (os.path.join(kit, 'tools', 'pm_data.py'), kit, data.replace("'", "''"),
                         '/srv/private.git'), out)
        self.assertNotIn('PM data: python', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def test_apply_runs_it_after_the_files_and_passes_its_lines(self):
        self.pm_tool("import sys\nprint('made', sys.argv[1:])\n")
        code, out = self.run_it(apply=True, pm_data=os.path.join(self.fx.base, 'data'))
        self.assertEqual(code, R.EXIT_OK, out)
        self.assertIn('pm_data.py init exited 0 (done)', out)
        self.assertTrue(out.index('new-repo: wrote .kit.json') < out.index('PM data: python'), out)
        self.assertIn("  made ['-C', ", out)

    def test_its_exit_code_is_reported_and_passed_on(self):
        for body, code_want, said in (
                ("import sys\nprint('no')\nsys.exit(1)\n", 1, 'pm_data.py init exited 1 (refused, it changed nothing)'),
                ("import sys\nsys.exit(3)\n", 3, 'pm_data.py init exited 3 (made locally'),
                ("raise RuntimeError('boom')\n", 1, 'pm_data.py init exited 1 (it crashed;'),
                ("import sys\nprint('crashed: ValueError: x')\nsys.exit(5)\n", 5, 'pm_data.py init exited 5 (it crashed;')):
            self.pm_tool(body)
            for rel in ('.kit.json', '.gitignore', '.gitattributes', 'docs/INDEX.md'):
                p = os.path.join(self.root, *rel.split('/'))
                if os.path.exists(p):
                    os.remove(p)
            code, out = self.run_it(apply=True, pm_data=os.path.join(self.fx.base, 'data'))
            self.assertEqual(code, code_want, out)
            self.assertIn(said, out)
            self.assertIn('run the PM-data step alone:  python ', out)
            self.assertTrue(os.path.isfile(os.path.join(self.root, '.kit.json')), out)


class Helpers(unittest.TestCase):
    def test_trim_slash_keeps_a_drive_root(self):
        self.assertEqual(R.trim_slash('C:\\x\\'), 'C:\\x')
        self.assertEqual(R.trim_slash('C:\\'), 'C:' + os.sep)
        self.assertEqual(R.trim_slash('/srv/r.git/'), '/srv/r.git')

    def test_ps_quote(self):
        self.assertEqual(R.ps_quote('C:\\a\\b.py'), 'C:\\a\\b.py')
        self.assertEqual(R.ps_quote("pm data's"), "'pm data''s'")


class Cli(Base):
    """tools/machine_setup.py --repo reaches the same run."""

    def test_repo_mode_through_the_tool(self):
        r = subprocess.run([sys.executable, TOOL, '--repo', self.root, '--owner-email', OWNER,
                            '--agent-email', AGENT, '--repo-name', 'pm'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, R.EXIT_OK, r.stdout + r.stderr)
        self.assertIn('new-repo: DRY RUN', r.stdout)
        self.assertIn('"name": "pm"', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def test_machine_options_are_usage_with_repo_and_repo_options_without(self):
        for args, said in ((['--repo', self.root, '--pm-settings', 'x'], '--pm-settings goes with the machine setup'),
                           (['--agent-email', AGENT], '--agent-email goes with --repo only'),
                           (['--repo', self.root, '--old-kit', self.root], 'not allowed with')):
            r = subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True)
            self.assertEqual(r.returncode, R.EXIT_USAGE, r.stdout + r.stderr)
            self.assertIn(said, r.stderr)


if __name__ == '__main__':
    unittest.main()
