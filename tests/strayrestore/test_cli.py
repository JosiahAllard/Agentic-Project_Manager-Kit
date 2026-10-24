"""tools/stray_restore.py: its arguments and exit codes."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from kitlib import strayrestore as sr
from tests import gitfixture
from tests.realgit import real_git
from tests.strayrestore.helpers import Fixture
from tools import stray_restore

REASON = 'git restore, check-attr and attributes: the fake git lacks them'


def run(argv, environ=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = stray_restore.main(argv, {} if environ is None else environ)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue() + err.getvalue()


@real_git(REASON)
class TheCommandLine(Fixture):

    def test_a_worker_mark_is_refused_and_touches_nothing(self):
        self.branch('kit/a', {'docs/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')
        code, out = run(['-C', self.top, '--apply'], {'PM_KIT_ROLE': 'deep-worker'})
        self.assertEqual(code, 2)
        self.assertIn('REFUSED', out)
        self.assertEqual(self.get('docs/new.md'), b'new text\n')

    def test_unmarked_and_pm_run(self):
        self.branch('kit/a', {'docs/new.md': b'new text\n'})
        self.put('docs/new.md', b'new text\n')
        code, out = run(['-C', self.top])
        self.assertEqual(code, 0, out)
        self.assertIn('DELETE    docs/new.md = refs/heads/kit/a', out)
        code, out = run(['-C', self.top, '--apply'], {'PM_KIT_ROLE': 'pm'})
        self.assertEqual(code, 0, out)
        self.assertIn('deleted   docs/new.md', out)
        self.assertFalse(os.path.exists(os.path.join(self.top, 'docs', 'new.md')))

    def test_a_leave_exits_1_and_leaves_the_file(self):
        self.put('README.md', b'mine\n')
        code, out = run(['-C', self.top, '--apply'])
        self.assertEqual(code, 1, out)
        self.assertIn('LEAVE     README.md', out)
        self.assertIn("left: README.md (git status ' M')", out)
        self.assertEqual(self.get('README.md'), b'mine\n')

    def test_a_git_failure_exits_2(self):
        self.put('README.md', b'mine\n')
        with mock.patch.object(sr._Repo, 'setup', lambda self: 'git for-each-ref failed: boom'):
            code, out = run(['-C', self.top, '--apply'])
        self.assertEqual(code, 2, out)
        self.assertIn('ERROR: git for-each-ref failed: boom', out)
        self.assertEqual(self.get('README.md'), b'mine\n')

    def test_an_error_part_way_lists_the_rest_as_left_and_exits_1(self):
        self.put('b.md', b'bee\n')
        self.r.git('add', 'b.md')
        self.r.git('commit', '-q', '-m', 'b')
        self.branch('kit/a', {'README.md': b'branch text\n', 'b.md': b'branch bee\n',
                              'z/new.md': b'new\n'})
        self.put('README.md', b'branch text\n')
        self.put('b.md', b'branch bee\n')
        self.put('z/new.md', b'new\n')
        with mock.patch.object(sr, '_restore_head', lambda *a: 'boom'):
            code, out = run(['-C', self.top, '--apply'])
        self.assertEqual(code, 1, out)
        self.assertIn('ERROR: boom', out)
        self.assertNotIn('restored', out)
        self.assertNotIn('deleted', out)
        self.assertIn('LEAVE     b.md: not reached', out)
        self.assertIn('LEAVE     z/new.md: not reached', out)
        for p in ('README.md', 'b.md', 'z/new.md'):
            self.assertIn('left: %s (git status ' % p, out)
        self.assertEqual(self.get('z/new.md'), b'new\n')


@real_git(REASON)
class Arguments(unittest.TestCase):

    def test_not_a_repository_exits_2(self):
        d = tempfile.mkdtemp(dir=gitfixture.fixture_parent())
        self.addCleanup(gitfixture.rmtree, d)
        code, out = run(['-C', d])
        self.assertEqual(code, 2, out)
        self.assertIn('ERROR: could not find the main checkout', out)

    def test_an_unknown_argument_is_a_usage_error(self):
        code, out = run(['--force'])
        self.assertEqual(code, 2)
        self.assertIn('USAGE:', out)


if __name__ == '__main__':
    unittest.main()
