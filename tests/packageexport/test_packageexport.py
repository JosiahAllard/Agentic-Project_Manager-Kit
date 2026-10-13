import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kitlib import packageexport as P
from tests import gitfixture


class Export(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('host/plugin.json', '{"name":"fixture"}\n')
        self.repo.write('host/scripts/a.py', 'pass\n')
        self.repo.write('kitlib/private.py', 'never exported\n')
        self.repo.commit('package', 'host/plugin.json', 'host/scripts/a.py', 'kitlib/private.py')
        self.sha = self.repo.head()
        self.scratch = tempfile.TemporaryDirectory(prefix='package-export-')
        self.addCleanup(self.scratch.cleanup)
        self.destination = Path(self.scratch.name) / 'packages' / self.sha

    def test_only_reviewed_subtree_commit_bytes(self):
        self.repo.write('host/scripts/a.py', 'uncommitted\n')
        self.repo.write('host/untracked.py', 'local\n')
        files = P.read_subtree(self.repo.root, self.sha, 'host')
        self.assertEqual(files, {'plugin.json': b'{"name":"fixture"}\n', 'scripts/a.py': b'pass\n'})

    def test_requires_full_commit_and_safe_subtree(self):
        for sha, subtree in [('HEAD', 'host'), (self.sha[:12], 'host'), (self.sha, '../host')]:
            with self.subTest(sha=sha, subtree=subtree), self.assertRaises(P.PackageError):
                P.read_subtree(self.repo.root, sha, subtree)

    def test_missing_subtree_refused(self):
        with self.assertRaises(P.PackageError):
            P.read_subtree(self.repo.root, self.sha, 'absent')

    def test_read_paths_preserves_selected_prefixes_and_optional_absence(self):
        files = P.read_paths(self.repo.root, self.sha, ('host', 'kitlib/private.py', 'absent'))
        self.assertEqual(files, {'host/plugin.json': b'{"name":"fixture"}\n',
                                 'host/scripts/a.py': b'pass\n',
                                 'kitlib/private.py': b'never exported\n'})
        self.assertEqual(P.read_paths(self.repo.root, self.sha, ('absent',)), {})
        with self.assertRaises(P.PackageError):
            P.read_subtree(self.repo.root, self.sha, 'kitlib/private.py')

    def test_read_paths_requires_safe_distinct_nonempty_selection(self):
        for paths in ((), ('host', 'HOST'), ('../host',), ('host/*',)):
            with self.subTest(paths=paths), self.assertRaises(P.PackageError):
                P.read_paths(self.repo.root, self.sha, paths)

    def test_links_submodules_and_windows_collisions_refused(self):
        for rows in [b'120000 blob x\thost/link\0', b'160000 commit x\thost/submodule\0',
                     b'100644 blob x\thost/NAME\x00100644 blob y\thost/name\0']:
            with mock.patch.object(P.cmd, 'run', return_value=(rows, None)), self.assertRaises(P.PackageError):
                P.read_subtree(self.repo.root, self.sha, 'host')

    def test_invalid_portable_paths(self):
        for path in ('/absolute', '../escape', 'a\\b', 'C:/x', 'a:stream', 'nul.txt', 'CON .txt', 'COM¹', 'a.', 'a ',
                     'a//b', 'a/./b', 'a\x01b', 'a?b', 'a\nb'):
            with self.subTest(path=path), self.assertRaises(P.PackageError):
                P.relative(path)

    def test_complete_publish_and_verified_idempotent_reuse(self):
        files = P.read_subtree(self.repo.root, self.sha, 'host')
        self.assertEqual(P.publish(files, self.sha, 'host', self.destination), self.destination)
        self.assertEqual(P.verify(self.destination), P.metadata(files, self.sha, 'host'))
        self.assertEqual(P.publish(files, self.sha, 'host', self.destination), self.destination)
        self.assertEqual(sorted(item.name for item in self.destination.parent.iterdir()), [self.sha])

    def test_changed_partial_and_unowned_destination_never_overwritten(self):
        self.destination.mkdir(parents=True)
        target = self.destination / 'owner.txt'
        target.write_bytes(b'keep')
        with self.assertRaises(P.PackageError):
            P.publish({'plugin.json': b'{}'}, self.sha, 'host', self.destination)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_corrupted_existing_package_and_extra_files_refused(self):
        for mutation in ('changed', 'extra'):
            with self.subTest(mutation=mutation):
                destination = self.destination.with_name(mutation)
                P.publish({'a': b'original'}, self.sha, 'host', destination)
                (destination / ('a' if mutation == 'changed' else 'extra')).write_bytes(b'modified')
                with self.assertRaises(P.PackageError):
                    P.verify(destination)

    def test_wrong_identity_marker_refused(self):
        P.publish({'a': b'a'}, self.sha, 'host', self.destination)
        expected = P.metadata({'a': b'a'}, 'f' * 40, 'host')
        with self.assertRaises(P.PackageError):
            P.verify(self.destination, expected)

    def test_failed_rename_cleans_only_owned_staging(self):
        self.destination.parent.mkdir(parents=True)
        keep = self.destination.parent / '.kit-package-unowned'
        keep.mkdir()
        with mock.patch.object(P.os, 'rename', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                P.publish({'a': b'a'}, self.sha, 'host', self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [keep])

    def test_links_never_followed(self):
        fake = mock.Mock(st_mode=0, st_file_attributes=0x400)
        with mock.patch.object(Path, 'lstat', return_value=fake), self.assertRaises(P.PackageError):
            P.no_links(self.destination)

    def test_writer_lock_is_exclusive_and_released_on_failure(self):
        root = Path(self.scratch.name)
        with self.assertRaises(RuntimeError):
            with P.exclusive(root):
                with self.assertRaises(P.PackageError):
                    with P.exclusive(root):
                        self.fail('second writer acquired lock')
                raise RuntimeError('operation failed')
        self.assertFalse((root / '.kit-prepare.lock').exists())

    def test_unknown_marker_fields_refused(self):
        P.publish({'a': b'a'}, self.sha, 'host', self.destination)
        marker = P.verify(self.destination)
        marker['trusted'] = True
        (self.destination / P.MARKER).write_text(json.dumps(marker))
        with self.assertRaises(P.PackageError):
            P.verify(self.destination)
