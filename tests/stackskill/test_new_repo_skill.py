"""installer/new-repo.ps1 -Stack godot (the committed repo setup, kitlib.machinesetup.repo) writes the
render-check skill with the kit's path in it, and refuses (writing nothing) when the kit root cannot
be written. A throwaway repository and a copy of
the kit under a folder with a space in its name (tests/gitfixture.py); fake identities only."""
import os
import re
import shlex
import shutil
import subprocess
import unittest
from unittest import mock

from kitlib import stubs
from tests import gitfixture

SKILL = os.path.join('.claude', 'skills', 'godot-render-check', 'SKILL.md')


class NewRepoSkill(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.fx = gitfixture.TempRepo()
        self.addCleanup(self.fx.cleanup)
        self.root = os.path.realpath(self.fx.root)
        self.kit = os.path.join(os.path.realpath(self.fx.base), 'kit copy')
        for name in ('installer', 'tools', 'kitlib', 'templates'):
            shutil.copytree(os.path.join(stubs.KIT_ROOT, name), os.path.join(self.kit, name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(os.path.join(stubs.KIT_ROOT, 'hosts.json'), self.kit)    # the host registry kitlib reads

    def ps(self, *args):
        argv = ['powershell', '-NoProfile', '-NonInteractive', '-File',
                os.path.join(self.kit, 'installer', 'new-repo.ps1'), '-Target', self.root,
                '-OwnerEmail', 'owner@example.invalid', '-AgentEmail', 'agent@example.invalid']
        done = subprocess.run(argv + list(args), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=gitfixture.fixture_env(), timeout=300)
        return done.returncode, (done.stdout or '') + (done.stderr or '')

    def test_apply_writes_the_skill_with_the_kit_path_as_one_quoted_argument(self):
        code, out = self.ps('-Apply', '-Stack', 'godot')
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.root, SKILL), encoding='utf-8') as fh:
            text = fh.read()
        self.assertNotIn('{kit_root}', text)
        want = os.path.normpath(self.kit).replace('\\', '/') + '/tools/machine_lock.py'
        self.assertIn(' ', want)
        self.assertEqual(re.findall(r'python "([^"]+)" run', text), [want, want])
        self.assertEqual(shlex.split('python "%s" run' % want)[1], want)
        self.assertIn('wrote    %s' % os.path.join(self.root, SKILL), out)

    def test_dry_run_lists_it_and_writes_nothing(self):
        code, out = self.ps('-Stack', 'godot')
        self.assertEqual(code, 0, out)
        self.assertIn('would create  %s' % os.path.join(self.root, SKILL), out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))

    def test_an_unresolvable_kit_root_refuses_before_anything_is_written(self):
        os.remove(os.path.join(self.kit, 'tools', 'machine_lock.py'))
        for args in (['-Stack', 'godot'], ['-Apply', '-Stack', 'godot']):
            code, out = self.ps(*args)
            self.assertEqual(code, 1, out)
            self.assertIn('stack_skill refused', out)
            self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
            self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))
            self.assertFalse(os.path.exists(os.path.join(self.root, 'tools', 'godot.cmd')))

    def test_no_python_on_path_refuses_before_anything_is_written(self):
        git_dir = os.path.dirname(shutil.which('git'))
        ps_dir = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0')
        env = gitfixture.fixture_env({'PATH': os.pathsep.join([ps_dir, git_dir])})
        self.assertIsNone(shutil.which('python', path=env['PATH']), 'python is beside git on this machine')
        argv = ['powershell', '-NoProfile', '-NonInteractive', '-File',
                os.path.join(self.kit, 'installer', 'new-repo.ps1'), '-Target', self.root,
                '-OwnerEmail', 'owner@example.invalid', '-AgentEmail', 'agent@example.invalid',
                '-Apply', '-Stack', 'godot']
        done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=env, timeout=300)
        out = (done.stdout or '') + (done.stderr or '')
        self.assertEqual(done.returncode, 2, out)
        self.assertIn('python is not on PATH', out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
        self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))

    def test_a_stack_without_skills_writes_none(self):
        code, out = self.ps('-Apply', '-Stack', 'python')
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))


if __name__ == '__main__':
    unittest.main()
