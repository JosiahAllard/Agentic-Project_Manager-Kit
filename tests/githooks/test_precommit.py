"""kitlib.githooks.precommit: the one dispatch githooks/pre-commit.py calls. It runs the kit's
checks in order, stops at the first refusal, and refuses when a check cannot run (docs/ledger.md
"Pre-commit check"). The entry script refuses a commit when it cannot import the kit."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from kitlib.githooks import precommit
from kitlib.ledger import precommit as ledger_precommit
from tests import gitfixture
from tests.realgit import real_git

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRY = os.path.join(KIT_ROOT, 'githooks', 'pre-commit.py')


def step(seen, name, code=0, message=None):
    def check(commit):
        seen.append((name, commit.root))
        return code, message
    check.__name__ = name
    return check


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.abspath(tempfile.gettempdir())

    def test_the_kit_checks_are_the_ledger_check_only(self):
        self.assertEqual(precommit.CHECKS, (ledger_precommit.check,))

    def test_checks_run_in_order_on_one_commit(self):
        seen = []
        code, message = precommit.main(self.root, checks=(step(seen, 'a', message='note a'),
                                                          step(seen, 'b')))
        self.assertEqual((code, message), (0, 'note a'))
        self.assertEqual(seen, [('a', self.root), ('b', self.root)])

    def test_the_first_refusal_stops_the_run(self):
        seen = []
        code, message = precommit.main(self.root, checks=(step(seen, 'a', message='note a'),
                                                          step(seen, 'b', 3, 'b refused'),
                                                          step(seen, 'c')))
        self.assertEqual(code, 3)
        self.assertEqual(message, 'note a\nb refused')
        self.assertEqual([s[0] for s in seen], ['a', 'b'])

    def test_a_check_that_raises_refuses(self):
        def broken(commit):
            raise RuntimeError('cannot judge')
        code, message = precommit.main(self.root, checks=(broken,))
        self.assertEqual(code, 1)
        self.assertIn('broken could not run: cannot judge', message)
        self.assertIn('commit refused', message)

    def test_no_checks_is_a_pass(self):
        self.assertEqual(precommit.main(self.root, checks=()), (0, None))


@real_git('the staged list is read under the GIT_INDEX_FILE git gives a hook')
class StagedTest(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)

    def test_staged_lists_added_changed_and_deleted_paths_once(self):
        self.repo.write('keep.txt', 'k\n')
        self.repo.write('gone.txt', 'g\n')
        self.repo.commit('base', 'keep.txt', 'gone.txt')
        self.repo.write('keep.txt', 'changed\n')
        self.repo.write('sub/new name.txt', 'n\n')
        self.repo.write('unstaged.txt', 'u\n')
        self.repo.git('add', '--', 'keep.txt', 'sub/new name.txt')
        self.repo.git('rm', '-q', '--', 'gone.txt')
        commit = precommit.Commit(self.repo.root)
        self.assertEqual(sorted(commit.staged()), ['gone.txt', 'keep.txt', 'sub/new name.txt'])
        commit._staged = ('cached',)
        self.assertEqual(commit.staged(), ('cached',))

    def test_git_failing_refuses_through_the_dispatch(self):
        def reads_staged(commit):
            commit.staged()
            return 0, None
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'missing')         # no folder: git cannot run there
            with self.assertRaises(precommit.StagedError):
                precommit.Commit(missing).staged()
            code, message = precommit.main(missing, checks=(reads_staged,))
        self.assertEqual(code, 1)
        self.assertIn('cannot list the staged files', message)


class EntryTest(unittest.TestCase):
    """githooks/pre-commit.py with no kit beside it (a copy alone in a folder) cannot import the
    dispatch and refuses the commit."""

    def test_an_import_failure_refuses(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(gitfixture.rmtree, tmp)
        hooks = os.path.join(tmp, 'githooks')
        os.makedirs(hooks)
        shutil.copy(ENTRY, hooks)
        r = subprocess.run([sys.executable, '-I', os.path.join(hooks, 'pre-commit.py')], cwd=tmp,
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('cannot load the kit hook code', r.stderr)
        self.assertIn('commit refused', r.stderr)


if __name__ == '__main__':
    unittest.main()
