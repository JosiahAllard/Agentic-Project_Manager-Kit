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
                with self.assertRaises(pieces.TableError) as cm:
                    pieces.Table(_table(was=bad))
                self.assertIn('was %r' % bad, str(cm.exception))
                self.assertNotIn('dir', str(cm.exception).split('must be')[0])

    def test_bad_root_paths_refused(self):
        cases = (('root', ['docs/./x']), ('root', ['.']), ('root', ['a//b']),
                 ('dotnet', {'root_files': ['a/../b']}), ('dotnet', {'solution': 'x//y.sln'}),
                 ('dotnet', {'solution': '.'}),
                 ('dotnet', {'was': {'old/./f': 'new'}}), ('dotnet', {'was': {'old': 'a//b'}}))
        for key, val in cases:
            with self.subTest(key=key, val=val):
                data = _table()
                data[key] = val
                with self.assertRaises(pieces.TableError) as cm:
                    pieces.Table(data)
                self.assertIn('repo-relative', str(cm.exception))
                self.assertIn("empty, `.` or `..`", str(cm.exception))

    def test_valid_dotnet_paths_accepted(self):
        data = _table()
        data['dotnet'] = {'solution': 'F.slnx', 'root_files': ['F.slnx'], 'was': {'fleet/F.slnx': 'F.slnx'}}
        pieces.Table(data)

    def test_valid_dirs_accepted(self):
        for good in ('a', 'a/b', 'dir.with.dots/x', '.hidden/x', 'a..b'):
            with self.subTest(dir=good):
                self.assertEqual(pieces.Table(_table(dir=good)).dir('alpha'), good)


if __name__ == '__main__':
    unittest.main()
