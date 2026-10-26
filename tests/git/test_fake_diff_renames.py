"""The fake git's rename detection against `diff.renames` (kitlib/git/fake/diffs.py
`renames_wanted`), with a twin on real git: an explicit option beats the config."""
import unittest

from tests.gitfixture import TempRepo
from tests.realgit import real_git


class DiffRenamesConfig(unittest.TestCase):

    def setUp(self):
        r = self.r = TempRepo()
        self.addCleanup(r.cleanup)
        r.write('big.txt', ''.join('line %d\n' % i for i in range(30)))
        self.base = r.commit('add big', 'big.txt')
        r.git('mv', 'big.txt', 'moved.txt')
        self.tip = r.commit('move big', 'moved.txt')

    def status(self, *args):
        out = self.r.git('diff', '--name-status', *args, '%s..%s' % (self.base, self.tip))
        return out.split('\t')[0][:1]

    def test_find_renames_beats_config_off(self):
        self.r.git('config', 'diff.renames', 'false')
        self.assertEqual(self.status(), 'D')
        self.assertEqual(self.status('--find-renames'), 'R')
        self.assertEqual(self.status('-M'), 'R')

    def test_no_renames_beats_config_on(self):
        self.r.git('config', 'diff.renames', 'true')
        self.assertEqual(self.status(), 'R')
        self.assertEqual(self.status('--no-renames'), 'D')


@real_git('fidelity twin: the same option-over-config order answered by git itself')
class DiffRenamesConfigRealGit(DiffRenamesConfig):
    pass


if __name__ == '__main__':
    unittest.main()
