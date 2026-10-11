"""installer/new-repo.ps1 -Stack godot writes the render-check skill with the kit's path in it, and
refuses (writing nothing) when the kit root cannot be written. A throwaway repository and a copy of
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
            self.assertIn('stack_skill.py refused', out)
            self.assertFalse(os.path.exists(os.path.join(self.root, '.kit.json')))
            self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))
            self.assertFalse(os.path.exists(os.path.join(self.root, 'tools', 'godot.cmd')))

    def test_a_stack_without_skills_writes_none(self):
        code, out = self.ps('-Apply', '-Stack', 'python')
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.claude')))


if __name__ == '__main__':
    unittest.main()
