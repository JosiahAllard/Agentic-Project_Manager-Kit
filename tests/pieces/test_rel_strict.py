"""A piece dir (or `was`) with an empty, `.` or `..` part is refused: it resolves to paths git
never prints, and every reader of the table would see it."""
import unittest

from kitlib import pieces


def _table(**over):
    piece = {'id': 'alpha', 'group': 'g', 'kind': 'asset', 'dir': 'a/b'}
    piece.update(over)
    return {'schema': 1, 'pieces': [piece]}


class RelStrictTest(unittest.TestCase):
    def test_bad_dirs_refused_naming_piece_and_dir(self):
        for bad in ('.', 'a//b', 'a/./b', 'a/../b', './a', '..', 'a/.', 'a/..', 'a/'):
            with self.subTest(dir=bad):
                with self.assertRaises(pieces.TableError) as cm:
                    pieces.Table(_table(dir=bad))
                self.assertIn('alpha', str(cm.exception))
                self.assertIn(repr(bad), str(cm.exception))

    def test_bad_was_refused(self):
        for bad in ('a//b', 'a/./b', '.'):
            with self.subTest(was=bad):
                with self.assertRaises(pieces.TableError):
                    pieces.Table(_table(was=bad))

    def test_bad_root_path_refused(self):
        data = _table()
        data['root'] = ['docs/./x']
        with self.assertRaises(pieces.TableError):
            pieces.Table(data)

    def test_valid_dirs_accepted(self):
        for good in ('a', 'a/b', 'dir.with.dots/x', '.hidden/x', 'a..b'):
            with self.subTest(dir=good):
                self.assertEqual(pieces.Table(_table(dir=good)).dir('alpha'), good)


if __name__ == '__main__':
    unittest.main()
