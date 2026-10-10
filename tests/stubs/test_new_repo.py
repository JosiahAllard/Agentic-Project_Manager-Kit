"""installer/new-repo.ps1 against a throwaway repository (tests/gitfixture.py), never a real one:
its dry run, -Apply, and its refusals. Fake identities only (example.invalid)."""
import json
import os
import shutil
import stat
import subprocess
import unittest
from unittest import mock

from kitlib import config, stubs
from kitlib.config import user
from tests import gitfixture

SCRIPT = os.path.join(stubs.KIT_ROOT, 'installer', 'new-repo.ps1')
OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'


class NewRepo(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.root = os.path.realpath(self.fx.root)

    def ps(self, *args, target=None, owner=OWNER, agent=AGENT, script=SCRIPT):
        argv = ['powershell', '-NoProfile', '-NonInteractive', '-File', script,
                '-Target', target or self.root, '-OwnerEmail', owner, '-AgentEmail', agent]
        done = subprocess.run(argv + list(args), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def kit_copy(self):
        """A copy of the kit beside the throwaway repository, so a test can lock or add template
        files without touching this kit. Returns the copy's new-repo.ps1."""
        kit = os.path.join(os.path.realpath(self.fx.base), 'kitcopy')
        for name in ('installer', 'tools', 'kitlib', 'templates'):
            shutil.copytree(os.path.join(stubs.KIT_ROOT, name), os.path.join(kit, name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        return kit, os.path.join(kit, 'installer', 'new-repo.ps1')

    def lock(self, path):
        """Open `path` for read+write sharing nothing, until the test ends: any other read fails."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        handle = k32.CreateFileW(path, 0xC0000000, 0, None, 3, 0x80, None)  # read+write, share none
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value, 'could not lock the file')
        self.addCleanup(k32.CloseHandle, wintypes.HANDLE(handle))

    def test_the_dry_run_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        code, out = self.ps()
        self.assertEqual(code, 0, out)
        self.assertIn('DRY RUN', out)
        self.assertIn('"email": "%s"' % OWNER, out)
        self.assertIn('would write  %s' % os.path.join(self.root, 'tools', 'landed.py'), out)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_apply_writes_the_config_and_the_stubs(self):
        code, out = self.ps('-Apply', '-MainBranch', 'master')
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.root, '.kit.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertEqual(data, {'repo': {'main_branch': 'master'},
                                'owner': {'email': OWNER, 'agent_emails': [AGENT]}})
        got = stubs.run(self.root, 'check')
        self.assertEqual(got.code, stubs.OK, got.lines)
        code, out = self.ps('-Apply')
        self.assertEqual(code, 1, out)
        self.assertIn('.kit.json exists already', out)

    def test_refusals(self):
        code, out = self.ps(owner=OWNER, agent=OWNER.upper())
        self.assertEqual(code, 2, out)
        self.assertIn('are the same', out)
        code, out = self.ps(owner='not-an-email')
        self.assertEqual(code, 2, out)
        sub = os.path.join(self.root, 'sub')
        os.makedirs(sub)
        code, out = self.ps('-Apply', target=sub)
        self.assertEqual(code, 1, out)
        self.assertIn('name its top level', out)
        plain = os.path.join(os.path.realpath(self.fx.base), 'plain')
        os.makedirs(plain)
        code, out = self.ps('-Apply', target=plain)
        self.assertEqual(code, 1, out)
        self.assertIn('is not a git checkout', out)
        code, out = self.ps('-Apply', target=os.path.join(stubs.KIT_ROOT, 'docs'))
        self.assertEqual(code, 1, out)
        self.assertIn('inside the kit itself', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def test_a_file_where_a_stub_goes_refuses_before_anything_is_written(self):
        self.fx.write('tools/landed.py', "print('own copy')\n")
        code, out = self.ps('-Apply')
        self.assertEqual(code, 1, out)
        self.assertIn('not a stub (kept', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as fh:
            return fh.read()

    def test_apply_writes_the_ignore_files_the_index_and_the_next_steps(self):
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        self.assertIn('.claude/worktrees/', self.read('.gitignore').splitlines())
        self.assertIn('.codex/worktrees/', self.read('.gitignore').splitlines())
        self.assertIn('* text=auto', self.read('.gitattributes').splitlines())
        self.assertIn('docs/INDEX.md', self.read('docs/INDEX.md'))
        for text in ('core.hooksPath', 'no gate step selected', 'gate.steps'):
            self.assertIn(text, out)

    def test_an_existing_ignore_file_gets_only_its_missing_lines(self):
        self.fx.write('.gitignore', 'build/\n.claude/worktrees/')
        self.fx.write('.gitattributes', '*   text=auto\n*.sh text eol=lf\n')
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        lines = self.read('.gitignore').splitlines()
        self.assertEqual(lines[:2], ['build/', '.claude/worktrees/'])
        self.assertEqual(lines.count('.claude/worktrees/'), 1)
        self.assertEqual(lines.count('.codex/worktrees/'), 1)
        self.assertIn('.claude/settings.local.json', lines)
        self.assertEqual(self.read('.gitattributes'), '*   text=auto\n*.sh text eol=lf\n')
        self.assertIn('unchanged', out)

    def test_an_existing_index_is_kept(self):
        self.fx.write('docs/INDEX.md', '# mine\n')
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('docs/INDEX.md'), '# mine\n')

    def test_a_missing_origin_is_a_warning_and_a_present_one_is_not(self):
        code, out = self.ps()
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING:  no remote named origin', out)
        self.assertIn('remote add origin', out)
        self.fx.git('remote', 'add', 'origin', 'https://example.invalid/r.git')
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        self.assertIn('remote:   origin -> https://example.invalid/r.git', out)
        self.assertNotIn('WARNING', out)

    def test_stack_writes_the_gate_section_the_lists_and_loads(self):
        code, out = self.ps('-Apply', '-Stack', 'python')
        self.assertEqual(code, 0, out)
        cfg = config.load(self.root)
        self.assertEqual(cfg.section_file('gate'), 'tools/gate.json')
        self.assertEqual([st['name'] for st in cfg.section('gate')['steps']], ['unit-tests'])
        self.assertIn('__pycache__/', self.read('.gitignore').splitlines())
        self.assertIn('*.pyc        binary', self.read('.gitattributes').splitlines())
        got = stubs.run(self.root, 'check')
        self.assertEqual(got.code, stubs.OK, got.lines)

    def test_append_keeps_crlf_and_stack_comments_are_not_missing_lines(self):
        path = os.path.join(self.root, '.gitattributes')
        with open(path, 'wb') as fh:
            fh.write(b'* text=auto\r\n*.sh text eol=lf\r\n')
        code, out = self.ps('-Stack', 'dotnet')
        self.assertEqual(code, 0, out)
        append = [ln for ln in out.splitlines() if 'would append' in ln and 'gitattributes' in ln]
        self.assertEqual(len(append), 1, out)
        self.assertNotIn('Visual Studio', append[0])
        code, out = self.ps('-Apply', '-Stack', 'dotnet')
        self.assertEqual(code, 0, out)
        with open(path, 'rb') as fh:
            raw = fh.read()
        self.assertIn(b'*.cs         text eol=lf\r\n', raw)
        self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'), raw)

    def test_stack_refusals_write_nothing(self):
        code, out = self.ps('-Stack', 'cobol')
        self.assertEqual(code, 2, out)
        self.assertIn('not a stack the kit has', out)
        self.fx.write('tools/gate.json', '{}\n')
        code, out = self.ps('-Apply', '-Stack', 'python')
        self.assertEqual(code, 1, out)
        self.assertIn('gate.json exists already', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
        self.assertEqual(self.read('tools/gate.json'), '{}\n')

    def test_godot_copies_its_tools_files_keeps_an_existing_one_and_names_its_pass_string(self):
        self.fx.write('tools/godot.cmd', 'rem mine\n')
        code, out = self.ps('-Apply', '-Stack', 'godot')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('tools/godot.cmd'), 'rem mine\n')
        self.assertIn('kept', out)
        self.assertIn('UNIT TESTS OK', out)
        self.assertNotIn('Ran N tests', out)
        os.remove(os.path.join(self.root, 'tools', 'godot.cmd'))
        os.remove(os.path.join(self.root, '.kit.json'))
        os.remove(os.path.join(self.root, 'tools', 'gate.json'))
        code, out = self.ps('-Apply', '-Stack', 'godot')
        self.assertEqual(code, 0, out)
        src = os.path.join(stubs.KIT_ROOT, 'templates', 'stacks', 'godot', 'tools', 'godot.cmd')
        with open(src, 'rb') as a, open(os.path.join(self.root, 'tools', 'godot.cmd'), 'rb') as b:
            self.assertEqual(a.read(), b.read())
        self.assertIn('wrote    %s' % os.path.join(self.root, 'tools', 'godot.cmd'), out)

    def test_a_failed_write_stops_with_exit_1_and_never_says_wrote(self):
        path = os.path.join(self.root, '.gitignore')
        self.fx.write('.gitignore', 'build/\n')
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(os.chmod, path, stat.S_IWRITE | stat.S_IREAD)
        code, out = self.ps('-Apply')
        self.assertEqual(code, 1, out)
        self.assertIn('new-repo: FAILED - cannot write %s' % path, out)
        self.assertIn('already written', out)
        self.assertNotIn('new-repo: wrote', out)
        self.assertNotIn('appended %s' % path, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'docs', 'INDEX.md')))

    def test_a_failed_write_names_what_was_written_and_the_target_that_failed(self):
        path = os.path.join(self.root, '.gitignore')
        self.fx.write('.gitignore', 'build/\n')
        os.chmod(path, stat.S_IREAD)
        self.addCleanup(os.chmod, path, stat.S_IWRITE | stat.S_IREAD)
        code, out = self.ps('-Apply')
        self.assertEqual(code, 1, out)
        undo = [ln for ln in out.splitlines() if 'already written' in ln]
        self.assertEqual(len(undo), 1, out)
        self.assertIn(os.path.join(self.root, '.kit.json'), undo[0])
        self.assertIn('(and the stubs listed above)', undo[0])
        self.assertNotIn(path, undo[0])
        self.assertIn('%s may be partly written' % path, out)

    def test_a_locked_ignore_file_refuses_at_plan_time_before_anything_is_written(self):
        path = os.path.join(self.root, '.gitignore')
        self.fx.write('.gitignore', 'build/\n')
        self.lock(path)
        code, out = self.ps()
        self.assertEqual(code, 1, out)
        self.assertIn('new-repo: REFUSED - cannot read %s' % path, out)
        code, out = self.ps('-Apply')
        self.assertEqual(code, 1, out)
        self.assertIn('new-repo: REFUSED - cannot read %s' % path, out)
        self.assertNotIn('new-repo: wrote', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))

    def test_an_unreadable_gate_template_fails_and_never_says_wrote(self):
        kit, script = self.kit_copy()
        self.lock(os.path.join(kit, 'templates', 'stacks', 'python', 'tools', 'gate.json'))
        code, out = self.ps('-Apply', '-Stack', 'python', script=script)
        self.assertEqual(code, 1, out)
        self.assertIn('new-repo: FAILED - cannot read', out)
        self.assertIn('gate.json', out)
        self.assertNotIn('wrote    ', out)
        self.assertNotIn('new-repo: wrote', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools', 'gate.json')))

    def test_an_unreadable_index_template_fails_and_never_says_wrote(self):
        kit, script = self.kit_copy()
        index = os.path.join(kit, 'templates', 'common', 'INDEX.md')
        self.lock(index)
        code, out = self.ps('-Apply', script=script)
        self.assertEqual(code, 1, out)
        self.assertIn('new-repo: FAILED - cannot read %s' % index, out)
        self.assertNotIn('new-repo: wrote', out)
        self.assertNotIn('wrote    %s' % os.path.join(self.root, 'docs', 'INDEX.md'), out)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'docs', 'INDEX.md')))

    def test_a_non_ascii_md_name_is_listed_as_it_is_not_quoted(self):
        name = 'déjà vu.md'
        self.fx.write(name, '# d\n')
        self.fx.git('add', '--', name)
        # -Command with the console set to UTF-8, so the name can be read back from the output.
        command = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; & '%s' -Target '%s' -OwnerEmail %s "
                   "-AgentEmail %s; exit $LASTEXITCODE" % (SCRIPT, self.root, OWNER, AGENT))
        done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=gitfixture.fixture_env(), timeout=300)
        out = (done.stdout or '') + (done.stderr or '')
        self.assertEqual(done.returncode, 0, out)
        self.assertIn('    %s' % name, out.splitlines())
        self.assertNotIn('\\303', out)

    def test_a_stack_tools_file_named_like_a_stub_refuses_at_plan_time(self):
        kit, script = self.kit_copy()
        stack = os.path.join(kit, 'templates', 'stacks', 'clash')
        os.makedirs(os.path.join(stack, 'tools'))
        shutil.copy(os.path.join(kit, 'templates', 'stacks', 'python', 'stack.json'), stack)
        shutil.copy(os.path.join(kit, 'templates', 'stacks', 'python', 'tools', 'gate.json'),
                    os.path.join(stack, 'tools'))
        with open(os.path.join(stack, 'tools', 'Landed.py'), 'w', encoding='utf-8') as fh:
            fh.write("print('not a stub')\n")
        before = sorted(os.listdir(self.root))
        for args in (('-Stack', 'clash'), ('-Apply', '-Stack', 'clash')):
            code, out = self.ps(*args, script=script)
            self.assertEqual(code, 1, out)
            self.assertIn("REFUSED - the clash stack's tools file Landed.py has the name of a kit tool stub", out)
            self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_a_godot_dry_run_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        code, out = self.ps('-Stack', 'godot')
        self.assertEqual(code, 0, out)
        self.assertIn('DRY RUN', out)
        self.assertIn('would create  %s' % os.path.join(self.root, 'tools', 'godot.cmd'), out)
        self.assertEqual(sorted(os.listdir(self.root)), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tools')))

    def test_next_step_1_names_the_stacks_tools_files(self):
        code, out = self.ps('-Apply', '-Stack', 'godot')
        self.assertEqual(code, 0, out)
        self.assertIn('.gitattributes, docs/INDEX.md and tools/godot.cmd as usual', out)

    def test_tracked_md_files_the_starter_index_misses_are_listed_not_refused(self):
        self.fx.write('README.md', '# r\n')
        self.fx.write('docs/guide.md', '# g\n')
        self.fx.git('add', '--', 'README.md', 'docs/guide.md')
        code, out = self.ps()
        self.assertEqual(code, 0, out)
        self.assertIn('add these to docs/INDEX.md', out)
        self.assertIn('    README.md', out)
        self.assertIn('    docs/guide.md', out)
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        self.assertIn('add these to docs/INDEX.md', out)
        os.remove(os.path.join(self.root, '.kit.json'))
        code, out = self.ps()
        self.assertNotIn('add these', out)  # the index exists now: it is the owner's to keep

    def test_the_stubs_cover_the_tools_a_repository_runs(self):
        code, out = self.ps('-Apply')
        self.assertEqual(code, 0, out)
        for name in ('land', 'ledger', 'gate', 'preflight', 'new_worktree'):
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'tools', name + '.py')), name)


class PmData(unittest.TestCase):
    """-PmData/-PrivateRemote (docs/pm-data-branch.md "Other users"). pm_data.py init acts on the
    kit's own repository, so these run a copy of the kit (installer, tools, kitlib, templates) in a
    throwaway repository with a bare origin, a scratch home and scratch remotes: never this kit."""

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.kit = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.kit.cleanup)
        self.kit_root = os.path.realpath(self.kit.root)
        for name in ('installer', 'tools', 'kitlib', 'templates'):
            shutil.copytree(os.path.join(stubs.KIT_ROOT, name), os.path.join(self.kit_root, name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.target = gitfixture.TempRepo()
        self.addCleanup(self.target.cleanup)
        self.target_root = os.path.realpath(self.target.root)
        base = os.path.realpath(self.kit.base)
        self.home = os.path.join(base, 'home')
        os.makedirs(self.home)
        self.data = os.path.join(base, 'data')
        self.private = os.path.join(base, 'private.git')
        self.kit.git('init', '-q', '--bare', self.private, cwd=base)

    def ps(self, *args):
        argv = ['powershell', '-NoProfile', '-NonInteractive', '-File',
                os.path.join(self.kit_root, 'installer', 'new-repo.ps1'),
                '-Target', self.target_root, '-OwnerEmail', OWNER, '-AgentEmail', AGENT]
        env = gitfixture.fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        done = subprocess.run(argv + list(args), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=env, timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def state(self):
        """What a run could change: the kit's refs, its git config, the data folder, the machine
        config (PowerShell itself writes under the scratch home's AppData), the target."""
        refs = self.kit.git('for-each-ref', '--format=%(refname) %(objectname)')
        with open(os.path.join(self.kit.common_dir(), 'config'), encoding='utf-8') as fh:
            cfg = fh.read()
        return (refs, cfg, os.path.exists(self.data), os.path.exists(user.config_file(self.home)),
                sorted(os.listdir(self.target_root)))

    def test_the_dry_run_prints_the_command_and_changes_nothing(self):
        self.data = os.path.join(os.path.dirname(self.data), "pm data's")
        before = self.state()
        code, out = self.ps('-PmData', self.data, '-PrivateRemote', self.private)
        self.assertEqual(code, 0, out)
        # PowerShell single quotes around what is not plain, a ' doubled: it pastes as one argument.
        self.assertIn("would run after the files above:  python %s -C %s init --path '%s' --url %s"
                      % (os.path.join(self.kit_root, 'tools', 'pm_data.py'), self.kit_root,
                         self.data.replace("'", "''"), self.private), out)
        self.assertNotIn('PM data: python', out)
        self.assertEqual(self.state(), before)

    def test_a_drive_root_pm_data_stays_a_root(self):
        root = os.path.splitdrive(self.data)[0] + '\\'
        code, out = self.ps('-PmData', root)
        self.assertEqual(code, 0, out)
        line = [l for l in out.splitlines() if 'would run after the files above' in l]
        self.assertEqual(len(line), 1, out)
        self.assertTrue(line[0].endswith(' init --path %s' % root), line[0])

    def test_a_relative_pm_data_is_taken_from_the_powershell_location(self):
        # -Command with Set-Location: the PowerShell location differs from the process folder.
        base = os.path.dirname(self.data)
        script = os.path.join(self.kit_root, 'installer', 'new-repo.ps1')
        command = ("Set-Location -LiteralPath '%s'; & '%s' -Target '%s' -OwnerEmail %s "
                   "-AgentEmail %s -PmData rel; exit $LASTEXITCODE"
                   % (base, script, self.target_root, OWNER, AGENT))
        env = gitfixture.fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env, timeout=300, cwd=self.target_root)
        out = (done.stdout or '') + (done.stderr or '')
        self.assertEqual(done.returncode, 0, out)
        self.assertIn(' init --path %s' % os.path.join(base, 'rel'), out)

    def test_a_private_remote_path_with_a_space_and_a_trailing_backslash(self):
        spaced = os.path.join(os.path.dirname(self.data), 'pri vate.git')
        self.kit.git('init', '-q', '--bare', spaced, cwd=os.path.dirname(spaced))
        code, out = self.ps('-Apply', '-PmData', self.data, '-PrivateRemote', spaced + '\\')
        self.assertEqual(code, 0, out)
        self.assertIn("--url '%s'" % spaced, out)
        root = self.kit.git('rev-parse', 'refs/heads/pmdata')
        self.assertEqual(self.kit.git('--git-dir', spaced, 'rev-parse', 'refs/heads/pmdata'), root)

    def test_apply_makes_the_data_branch_in_the_kit_after_the_setup(self):
        code, out = self.ps('-Apply', '-PmData', self.data, '-PrivateRemote', self.private)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.target_root, '.kit.json')), out)
        self.assertIn('pm_data.py init exited 0 (done)', out)
        self.assertTrue(out.index('new-repo: wrote .kit.json') < out.index('PM data: python'), out)
        root = self.kit.git('rev-parse', 'refs/heads/pmdata')
        self.assertEqual(self.kit.git('--git-dir', self.private, 'rev-parse', 'refs/heads/pmdata'),
                         root)
        self.assertEqual(self.kit.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=self.data), 'pmdata')
        self.assertTrue(os.path.isfile(user.config_file(self.home)))
        # The target got no data branch: the branch is the kit's.
        self.assertEqual(self.target.git('branch', '--list', 'pmdata'), '')

    def test_a_refusal_of_pm_data_is_reported_with_its_exit_code(self):
        # The private url is the public one: pm_data.py init refuses (1) and changes nothing.
        refs = self.kit.git('for-each-ref', '--format=%(refname) %(objectname)')
        code, out = self.ps('-Apply', '-PmData', self.data, '-PrivateRemote', self.kit.remote)
        self.assertEqual(code, 1, out)
        self.assertIn('pm_data.py init exited 1 (refused, it changed nothing)', out)
        self.assertIn('run the PM-data step alone:  python ', out)
        self.assertTrue(os.path.isfile(os.path.join(self.target_root, '.kit.json')), out)
        self.assertEqual(self.kit.git('for-each-ref', '--format=%(refname) %(objectname)'), refs)
        self.assertFalse(os.path.exists(self.data))

    def test_a_failed_push_is_exit_3(self):
        gone = os.path.join(os.path.realpath(self.kit.base), 'no-such-remote.git')
        code, out = self.ps('-Apply', '-PmData', self.data, '-PrivateRemote', gone)
        self.assertEqual(code, 3, out)
        self.assertIn('pm_data.py init exited 3 (made locally', out)
        self.assertTrue(self.kit.git('rev-parse', 'refs/heads/pmdata'))

    def test_a_crash_of_pm_data_is_not_called_a_refusal(self):
        with open(os.path.join(self.kit_root, 'tools', 'pm_data.py'), 'w', encoding='utf-8') as fh:
            fh.write("raise RuntimeError('boom')\n")
        code, out = self.ps('-Apply', '-PmData', self.data)
        self.assertEqual(code, 1, out)
        self.assertIn('pm_data.py init exited 1 (it crashed;', out)
        self.assertNotIn('changed nothing', out)

    def test_exit_5_of_pm_data_is_reported_as_a_crash(self):
        with open(os.path.join(self.kit_root, 'tools', 'pm_data.py'), 'w', encoding='utf-8') as fh:
            fh.write("import sys\nprint('crashed: ValueError: x')\nsys.exit(5)\n")
        code, out = self.ps('-Apply', '-PmData', self.data)
        self.assertEqual(code, 5, out)
        self.assertIn('  crashed: ValueError: x', out)
        self.assertIn('pm_data.py init exited 5 (it crashed;', out)

    def test_private_remote_without_pm_data_is_usage(self):
        before = self.state()
        code, out = self.ps('-Apply', '-PrivateRemote', self.private)
        self.assertEqual(code, 2, out)
        self.assertIn('-PrivateRemote needs -PmData', out)
        self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
