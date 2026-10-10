"""installer/new-repo.ps1 against a throwaway repository (tests/gitfixture.py), never a real one:
its dry run, -Apply, and its refusals. Fake identities only (example.invalid)."""
import json
import os
import shutil
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

    def ps(self, *args, target=None, owner=OWNER, agent=AGENT):
        argv = ['powershell', '-NoProfile', '-NonInteractive', '-File', SCRIPT,
                '-Target', target or self.root, '-OwnerEmail', owner, '-AgentEmail', agent]
        done = subprocess.run(argv + list(args), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

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
        self.data = os.path.join(os.path.dirname(self.data), 'pm data')
        before = self.state()
        code, out = self.ps('-PmData', self.data, '-PrivateRemote', self.private)
        self.assertEqual(code, 0, out)
        self.assertIn('would run after the files above:  python %s -C %s init --path "%s" --url %s'
                      % (os.path.join(self.kit_root, 'tools', 'pm_data.py'), self.kit_root,
                         self.data, self.private), out)
        self.assertNotIn('PM data: python', out)
        self.assertEqual(self.state(), before)

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
        self.assertTrue(os.path.isfile(os.path.join(self.home, '.claude-pm-kit', 'config.json')))
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
