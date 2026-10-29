"""kitlib.fs.rmtree: a temp git repository with read-only object files is removed, not left in
TEMP; the handler goes to shutil.rmtree as onexc or onerror by Python version."""
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from kitlib.fs import rmtree as R


def read_only_repo(parent):
    """A folder shaped like a throwaway git repository whose object files are read-only (as git
    writes them): <parent>/repo/.git/objects/ab/cdef..., plus a writable file beside it."""
    repo = os.path.join(parent, 'repo')
    objects = os.path.join(repo, '.git', 'objects', 'ab')
    os.makedirs(objects)
    for name in ('cdef0123', 'cdef4567'):
        path = os.path.join(objects, name)
        with open(path, 'wb') as f:
            f.write(b'x')
        os.chmod(path, stat.S_IREAD)
    with open(os.path.join(repo, 'README.md'), 'w') as f:
        f.write('hello\n')
    return repo


class RmtreeForce(unittest.TestCase):
    """A temp git repository with read-only object files is removed, not left in TEMP."""

    def setUp(self):
        self.parent = tempfile.mkdtemp(prefix='kit-rmforce-')
        self.addCleanup(R.rmtree_force, self.parent, True)

    def test_a_repo_with_read_only_object_files_is_removed(self):
        repo = read_only_repo(self.parent)
        R.rmtree_force(repo)
        self.assertFalse(os.path.exists(repo))
        self.assertTrue(os.path.isdir(self.parent))             # only the folder asked for

    def test_plain_rmtree_with_ignore_errors_leaves_it(self):
        # What the runner and the fixtures did before: the error is swallowed and the repo stays.
        repo = read_only_repo(self.parent)
        shutil.rmtree(repo, ignore_errors=True)
        self.assertTrue(os.path.exists(repo))
        R.rmtree_force(repo)
        self.assertFalse(os.path.exists(repo))

    def test_a_missing_folder_is_not_an_error(self):
        R.rmtree_force(os.path.join(self.parent, 'none'))

    def test_the_handler_is_passed_as_onexc_or_onerror_by_python_version(self):
        for has_onexc, keyword in ((True, 'onexc'), (False, 'onerror')):
            repo = read_only_repo(self.parent)
            seen = {}
            real = shutil.rmtree

            def spy(path, **kw):
                seen.update(kw)
                return real(path, **kw)
            with mock.patch.object(R, '_HAS_ONEXC', has_onexc), \
                    mock.patch.object(R.shutil, 'rmtree', spy):
                R.rmtree_force(repo)
            self.assertEqual(list(seen), [keyword])
            self.assertFalse(os.path.exists(repo))


if __name__ == '__main__':
    unittest.main()
