"""kitlib.gate.hookrev.load_module_at: the hook module is run from a commit's blob, yet its
__file__ is the work-tree path, so a hook that finds sibling files from __file__ gets a real
directory in a landing's merged-tree gate. Tracebacks still name the commit."""
import json
import os
import sys
import traceback
import unittest

from kitlib.config import ConfigError, load_at
from kitlib.gate import hookrev
from tests import gitfixture

HOOK = '''import os

HERE = os.path.dirname(__file__)


def boom():
    raise RuntimeError('from the hook')
'''


class LoadModuleAtTest(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('.kit.json', json.dumps({'hook': {'module': 'repo_hook'}}) + '\n')
        self.repo.write('tools/repo_hook.py', HOOK)
        self.rev = self.repo.commit('hook', '.kit.json', 'tools/repo_hook.py')
        self.cfg = load_at(self.repo.root, self.rev)

    def load(self):
        module, options = hookrev.load_module_at(self.cfg, self.repo.root, self.rev)
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return module

    def test_file_dirname_is_the_real_tools_dir(self):
        module = self.load()
        want = os.path.join(self.repo.root, 'tools')
        self.assertTrue(os.path.isdir(module.HERE), module.HERE)
        self.assertEqual(os.path.normcase(os.path.abspath(module.HERE)),
                         os.path.normcase(os.path.abspath(want)))
        self.assertEqual(os.path.normcase(module.__file__),
                         os.path.normcase(os.path.join(self.repo.root, 'tools', 'repo_hook.py')))

    def test_traceback_names_the_commit(self):
        module = self.load()
        try:
            module.boom()
        except RuntimeError:
            text = traceback.format_exc()
        self.assertIn('%s:tools/repo_hook.py' % self.rev[:10], text)

    def test_bad_gate_checks_error_names_the_commit(self):
        self.repo.write('tools/repo_hook.py', HOOK + '\nGATE_CHECKS = {"unit": 1}\n')
        rev = self.repo.commit('bad checks', 'tools/repo_hook.py')
        cfg = load_at(self.repo.root, rev)
        names = set(sys.modules)
        self.addCleanup(lambda: [sys.modules.pop(n) for n in set(sys.modules) - names])
        with self.assertRaises(ConfigError) as cm:
            hookrev.load_gate_checks_at(cfg, self.repo.root, rev)
        self.assertIn('%s:tools/repo_hook.py' % rev[:10], str(cm.exception))


if __name__ == '__main__':
    unittest.main()
