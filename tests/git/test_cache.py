"""kitlib.git.cache: in a test process a full hash's resolve is asked of git once, and every
write (a kitlib git child that is not a read, a fixture git child) forgets it; any other process
keeps nothing."""
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib.git import cache, cmd, refs
from kitlib.proc import job
from tests import gitfixture
from tests.realgit import real_git


def raw_git(cwd, *args):
    """A git child neither kitlib nor the fixture sees (another process, as far as the cache knows)."""
    r = subprocess.run(('git',) + args, cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', env=gitfixture.fixture_env())
    if r.returncode != 0:
        raise AssertionError('git %s failed: %s' % (' '.join(args), r.stderr.strip()))
    return r.stdout.strip()


@real_git('counts the real git children kitlib starts; the fake starts none')
class Launches(unittest.TestCase):
    """Counts the git children kitlib starts (kitlib.proc.job.Job.start), leaving cmd genuine."""

    def setUp(self):
        cache.clear()
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.r.write('x.txt', 'x\n')
        self.x = self.r.commit('x', 'x.txt')
        self.started = []
        real = job.Job.start

        def spy(j, argv, *a, **kw):
            self.started.append(list(argv))
            return real(j, argv, *a, **kw)
        patcher = mock.patch.object(job.Job, 'start', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_full_hash_is_asked_of_git_once(self):
        self.assertEqual(refs.resolve(self.x, self.r.root), (self.x, None))
        self.assertEqual(refs.resolve(self.x, self.r.root), (self.x, None))
        self.assertEqual(len(self.started), 1)
        tree = refs.tree_of(self.x, self.r.root)[0]
        self.assertEqual(refs.tree_of(self.x, self.r.root), (tree, None))
        self.assertEqual(tree, raw_git(self.r.root, 'rev-parse', self.x + '^{tree}'))
        self.assertEqual(len(self.started), 2)

    def test_a_ref_head_and_an_abbreviation_are_asked_every_time(self):
        for rev in ('HEAD', 'refs/heads/main', self.x[:12]):
            before = len(self.started)
            self.assertEqual(refs.resolve(rev, self.r.root), (self.x, None))
            self.assertEqual(refs.resolve(rev, self.r.root), (self.x, None))
            self.assertEqual(len(self.started), before + 2, rev)

    def test_a_missing_object_is_asked_again(self):
        other = gitfixture.TempRepo()
        self.addCleanup(other.cleanup)
        self.assertIsNone(refs.resolve(self.x, other.root)[0])
        raw_git(other.root, 'fetch', '-q', self.r.root, 'main')
        self.assertEqual(refs.resolve(self.x, other.root), (self.x, None))

    def test_a_ref_moved_by_another_process_is_seen(self):
        self.assertEqual(refs.resolve('refs/heads/main', self.r.root), (self.x, None))
        raw_git(self.r.root, 'update-ref', 'refs/heads/main', self.r.initial)
        self.assertEqual(refs.resolve('refs/heads/main', self.r.root), (self.r.initial, None))

    def replaced(self, make_new_repository):
        """resolve(x) in a repository replaced by `make_new_repository()`, after x was kept."""
        self.assertEqual(refs.resolve(self.x, self.r.root), (self.x, None))
        gitfixture.rmtree(self.r.root)
        make_new_repository()
        return refs.resolve(self.x, self.r.root)

    def test_a_kitlib_write_forgets_what_was_kept(self):
        sha, why = self.replaced(lambda: cmd.call(['init', '-q', self.r.root], self.r.base))
        self.assertIsNone(sha)
        self.assertIn('does not name a commit', why)

    def test_a_fixture_git_forgets_what_was_kept(self):
        sha, why = self.replaced(lambda: self.r.git('init', '-q', self.r.root, cwd=self.r.base))
        self.assertIsNone(sha)
        self.assertIn('does not name a commit', why)


KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A process that never imports tests/gitfixture.py (a tool, a hook, the lock MCP server): it
# resolves an object, a git child it did not start through cmd.call prunes it, it resolves again.
PRUNED_UNDER_IT = '''
import subprocess, sys
from kitlib.git import refs
root, sha = sys.argv[1], sys.argv[2]
print(refs.resolve(sha, root)[0])
subprocess.run(('git', 'prune', '--expire=now'), cwd=root, check=True)
print(refs.resolve(sha, root)[0])
'''


@real_git('a separate process prunes an object with real git; the fake runs in this process only')
class OffOutsideTests(unittest.TestCase):

    def test_a_process_that_does_not_turn_it_on_sees_an_object_pruned_under_it(self):
        r = gitfixture.TempRepo()
        self.addCleanup(r.cleanup)
        tree = raw_git(r.root, 'rev-parse', 'HEAD^{tree}')
        loose = raw_git(r.root, 'commit-tree', tree, '-m', 'unreachable')   # no ref, no reflog
        out = subprocess.run((sys.executable, '-c', PRUNED_UNDER_IT, r.root, loose), cwd=KIT_ROOT,
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             env=gitfixture.fixture_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [loose, 'None'])

    def test_off_keeps_nothing(self):
        self.assertTrue(cache.enabled(), 'tests/gitfixture.py turns it on in a test process')
        with mock.patch.object(cache, '_enabled', False):
            cache.put(('k',), cache.generation(), 'v')
            self.assertIsNone(cache.get(('k',)))
        self.assertIsNone(cache.get(('k',)))


class OnTheFakeGit(unittest.TestCase):
    """The fake git answers in this process and its writes do not pass cache.child, so the cache
    keeps none of its answers."""

    def test_a_write_by_the_fake_git_is_never_hidden_by_the_cache(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        cache.clear()
        self.addCleanup(cache.clear)
        r = gitfixture.TempRepo()
        self.addCleanup(r.cleanup)
        r.write('x.txt', 'x\n')
        x = r.commit('x', 'x.txt')
        self.assertEqual(refs.resolve(x, r.root), (x, None))
        gitfixture.rmtree(r.root)
        cmd.call(['init', '-q', r.root], r.base)
        sha, why = refs.resolve(x, r.root)
        self.assertIsNone(sha)
        self.assertIn('does not name a commit', why)


class Generations(unittest.TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_read_keeps_the_memo_and_anything_else_forgets_it(self):
        cache.put(('k',), cache.generation(), 'v')
        cache.child('rev-parse')
        self.assertEqual(cache.get(('k',)), 'v')
        for sub in ('commit', 'fetch', 'update-ref', 'worktree', 'status', ''):
            cache.put(('k',), cache.generation(), 'v')
            cache.child(sub)
            self.assertIsNone(cache.get(('k',)), sub)

    def test_an_answer_taken_before_a_write_is_not_kept(self):
        gen = cache.generation()
        cache.child('commit')
        cache.put(('k',), gen, 'v')
        self.assertIsNone(cache.get(('k',)))

    def test_cmd_call_forgets_before_and_after_a_write(self):
        seen = []

        def fake(argv, **kw):
            seen.append(cache.generation())
            return 0, b'', b''
        gen = cache.generation()
        with mock.patch.object(cmd, '_run', side_effect=fake):
            cmd.call(['update-ref', 'refs/heads/x', 'a' * 40], os.getcwd())
            during = cache.generation()
            cmd.call(['rev-parse', 'HEAD'], os.getcwd())
        self.assertGreater(seen[0], gen)
        self.assertGreater(during, seen[0])
        self.assertEqual(cache.generation(), during)

    def test_a_faked_git_keeps_nothing(self):
        sha = 'b' * 40
        with mock.patch.object(cmd, '_run', return_value=(0, (sha + '\n').encode(), b'')) as run:
            self.assertEqual(refs.resolve(sha, os.getcwd()), (sha, None))
            self.assertEqual(refs.resolve(sha, os.getcwd()), (sha, None))
        self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
