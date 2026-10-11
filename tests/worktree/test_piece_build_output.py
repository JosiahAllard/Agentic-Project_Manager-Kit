"""kitlib.worktree.remove: a `worktree_remove.build_output` entry {"piece", "path"} is the literal
folder the pieces table (kitlib.pieces) puts at that piece's dir, and one that cannot be resolved
holds nothing (docs/worktree-remove.md). Throwaway repositories only (tests/gitfixture.py).
"""
import os
import unittest

from kitlib.worktree import holders
from tests.worktree.test_worktree_remove import RemoveCase

TABLE = {'schema': 1, 'pieces': [{'id': 'pvp', 'group': 'g', 'kind': 'spec', 'dir': 'models'}]}
ENTRY = {'piece': 'pvp', 'path': 'env/.build/'}


def config(build, table=TABLE):
    data = {'worktree_remove': {'build_output': build}}
    if table is not None:
        data['pieces'] = table
    return data


class PieceBuildOutput(RemoveCase):

    CONFIG = config([ENTRY])

    def setUp(self):
        super().setUp()
        self.fx.write('.gitignore', '.build/\n.buildx/\nbin/\n')
        self.fx.commit('ignore', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')

    def landed(self, slug, *files):
        path = self.workstream(slug)
        self.land('kit/' + slug)
        for f in files:
            self.fx.write(f, 'built\n', root=path)
        return path

    def assert_kept(self, slug, path, rel, out):
        self.assertTrue(os.path.isfile(os.path.join(path, *rel.split('/'))), out)
        self.assertIn(os.path.normcase(path), self.registered(), out)
        self.assertIn('kit/' + slug, self.branches())

    def test_piece_folder_goes_with_the_tree(self):
        path = self.landed('pvp', 'models/env/.build/x.bin', 'models/env/.build/sub/y.o')
        rc, out = self.run_verb('remove', 'pvp')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn('kit/pvp', self.branches())

    def test_same_name_outside_the_piece_refuses(self):
        path = self.landed('elsewhere', 'other/env/.build/x.bin')
        rc, out = self.run_verb('remove', 'elsewhere')
        self.assertEqual(rc, 1, out)
        self.assertIn('!! other/env/.build/', out)
        self.assert_kept('elsewhere', path, 'other/env/.build/x.bin', out)

    def test_look_alike_sibling_refuses(self):
        path = self.landed('sibling', 'models/env/.buildx/x.bin')
        rc, out = self.run_verb('remove', 'sibling')
        self.assertEqual(rc, 1, out)
        self.assertIn('!! models/env/.buildx/', out)
        self.assert_kept('sibling', path, 'models/env/.buildx/x.bin', out)

    def test_keeper_inside_the_piece_folder_refuses(self):
        path = self.landed('weights', 'models/env/.build/w.pt')
        rc, out = self.run_verb('remove', 'weights')
        self.assertEqual(rc, 1, out)
        self.assertIn('models/env/.build/w.pt', out)
        self.assert_kept('weights', path, 'models/env/.build/w.pt', out)

    def test_unreadable_table_refuses(self):
        path = self.landed('table', 'models/env/.build/x.bin')
        cases = [
            ('no table', config([ENTRY], table=None), 'no pieces table'),
            ('malformed', config([ENTRY], table={'schema': 2, 'pieces': []}), 'schema must be 1'),
            ('unknown piece', config([{'piece': 'nope', 'path': 'env/.build/'}]), "no piece 'nope'"),
        ]
        for name, data, words in cases:
            with self.subTest(name):
                self.configure(data)
                rc, out = self.run_verb('remove', 'table')
                self.assertEqual(rc, 1, out)
                self.assertIn('!! models/env/.build/ (', out)
                self.assertIn(words, out)
                self.assert_kept('table', path, 'models/env/.build/x.bin', out)

    def test_missing_pieces_file_is_a_config_refusal(self):
        path = self.landed('file', 'models/env/.build/x.bin')
        self.configure(dict(config([ENTRY], table=None), pieces='tools/pieces.json'))
        rc, out = self.run_verb('remove', 'file')
        self.assertEqual(rc, 2, out)
        self.assertIn('tools/pieces.json, which does not exist', out)
        self.assert_kept('file', path, 'models/env/.build/x.bin', out)

    def test_bad_piece_path_refuses(self):
        path = self.landed('badpath', 'models/env/.build/x.bin')
        for bad in ('/', '../env/.build/', 'env/*/', 'env\\.build/', '/env/.build/', 'env//.build/',
                    './env/.build/', 'C:/env/.build/'):
            with self.subTest(bad):
                self.configure(config([{'piece': 'pvp', 'path': bad}]))
                rc, out = self.run_verb('remove', 'badpath')
                self.assertEqual(rc, 1, out)
                self.assertIn('"path" must be a folder relative to the piece', out)
                self.assert_kept('badpath', path, 'models/env/.build/x.bin', out)

    def test_globs_still_apply_when_the_table_is_unreadable(self):
        self.configure(config(['bin', ENTRY], table=None))
        path = self.landed('globs', 'bin/app.exe')
        rc, out = self.run_verb('remove', 'globs')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))

    def test_a_path_without_trailing_slash_resolves_the_same(self):
        self.configure(config([{'piece': 'pvp', 'path': 'env/.build'}]))
        path = self.landed('noslash', 'models/env/.build/x.bin')
        rc, out = self.run_verb('remove', 'noslash')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))


class IsBuildOutputDirs(unittest.TestCase):

    def test_exact_or_under_never_a_sibling_or_parent(self):
        dirs = ['models/env/.build']
        self.assertTrue(holders.is_build_output('models/env/.build/', [], dirs))
        self.assertTrue(holders.is_build_output('models/env/.build/sub/x.o', [], dirs))
        self.assertFalse(holders.is_build_output('models/env/.buildx/', [], dirs))
        self.assertFalse(holders.is_build_output('models/env/', [], dirs))
        self.assertFalse(holders.is_build_output('other/env/.build/', [], dirs))
        self.assertFalse(holders.is_build_output('.build/', [], dirs))

    def test_globs_unchanged_without_dirs(self):
        self.assertTrue(holders.is_build_output('src/bin/', ['bin']))
        self.assertFalse(holders.is_build_output('bin/x.o', ['bin']))


if __name__ == '__main__':
    unittest.main()
