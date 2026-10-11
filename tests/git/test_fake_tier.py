"""The unit tier runs on the fake git (docs/test-tools.md "Fake git"): a fixture repository, the
kit's git calls and a tool process the test starts all answer git without starting it; what the
fake cannot do fails loudly, never by starting real git."""
import os
import shutil
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

from kitlib.git import batch, cmd
from tests import gitfixture

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SEEN = []          # git launches seen while _WATCH[0] is True
_WATCH = [False]


def _is_git(argv):
    if isinstance(argv, (str, bytes)):
        argv = os.fsdecode(argv).split()
    return bool(argv) and os.path.basename(os.fsdecode(os.fspath(argv[0]))).lower() in (
        'git', 'git.exe')


def _watch(event, args):
    if _WATCH[0] and event == 'subprocess.Popen' and _is_git(args[1]):
        _SEEN.append(list(args[1]) if not isinstance(args[1], (str, bytes)) else args[1])


sys.addaudithook(_watch)

# A tool process: its own git launches, counted from inside it, printed as one number.
CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
n = [0]
def watch(event, args):
    if event == 'subprocess.Popen':
        a = args[1]
        first = a.split()[0] if isinstance(a, (str, bytes)) else a[0]
        if os.path.basename(os.fsdecode(os.fspath(first))).lower() in ('git', 'git.exe'):
            n[0] += 1
sys.addaudithook(watch)
from kitlib.git import cmd
out, why = cmd.run(['rev-parse', 'HEAD'], sys.argv[2])
print(n[0], (out or b'').decode().strip(), why)
'''


class UnitTierStartsNoGit(unittest.TestCase):

    def setUp(self):
        # Only an explicit real-git run (kit_tests.py --real-git) skips: the claim is the unit
        # tier's, and a checkout without the fake (KIT_FAKE_GIT unset) must fail it.
        if os.environ.get('KIT_FAKE_GIT') == '0':
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.r.cleanup)

    def watched(self, fn):
        del _SEEN[:]
        _WATCH[0] = True
        try:
            return fn()
        finally:
            _WATCH[0] = False

    def test_fixture_and_kit_git_calls_start_no_git(self):
        def work():
            with open(os.path.join(self.r.root, 'a.txt'), 'wb') as f:
                f.write(b'a\n')
            self.r.git('add', 'a.txt')
            self.r.git('commit', '-q', '-m', 'a')
            self.r.git('push', '-q', 'origin', 'main')
            head, why = cmd.run(['rev-parse', 'HEAD'], self.r.root)
            self.assertIsNone(why)
            with batch.CatFile(self.r.root) as cat:
                self.assertEqual(cat.read(head.decode().strip(), 'a.txt'), b'a\n')
            return head
        self.watched(work)
        self.assertEqual(_SEEN, [], 'the unit tier started git')

    def test_a_tool_process_the_test_starts_runs_no_git(self):
        # The child inherits KIT_FAKE_GIT from this process's environment (tests/__init__.py).
        p = self.watched(lambda: subprocess.run(
            [sys.executable, '-c', CHILD, ROOT, self.r.root], capture_output=True, text=True,
            timeout=120))
        self.assertEqual(p.returncode, 0, p.stderr)
        count, head, why = p.stdout.split(' ', 2)
        self.assertEqual(int(count), 0, 'the tool process started git %s time(s)' % count)
        self.assertEqual(head, self.r.git('rev-parse', 'HEAD'))
        self.assertEqual(_SEEN, [])


class FakeFailsLoudly(unittest.TestCase):
    """What the fake does not do ends the test; it is never handed to real git."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)

    def test_an_unknown_command_raises_unsupported_naming_the_argv(self):
        from kitlib.git import fake
        del _SEEN[:]
        _WATCH[0] = True
        try:
            with self.assertRaises(fake.Unsupported) as cm:
                cmd.call(['blame', 'README.md'], self.r.root)
        finally:
            _WATCH[0] = False
        self.assertIn('unsupported in fake git:', str(cm.exception))
        self.assertIn('blame', str(cm.exception))
        self.assertEqual(_SEEN, [])

    def test_unsupported_is_not_an_exception_kit_code_could_swallow(self):
        from kitlib.git import fake
        self.assertFalse(issubclass(fake.Unsupported, Exception))

    def test_the_fixture_git_raises_too(self):
        from kitlib.git import fake
        with self.assertRaises(fake.Unsupported):
            self.r.git('blame', 'README.md')

    def test_an_inherited_stream_is_answered_only_while_git_writes_nothing_to_it(self):
        from kitlib.git import fake
        argv = ['git', '-C', self.r.root, 'config', 'kit.probe', 'x']
        self.assertEqual(cmd.subprocess_run(argv, check=True).returncode, 0)    # silent: answered
        with self.assertRaises(fake.Unsupported) as cm:
            cmd.subprocess_run(['git', '-C', self.r.root, 'config', 'kit.probe'])
        self.assertIn('inherited stdout', str(cm.exception))

    def test_a_broken_loose_ref_raises_unsupported(self):
        # git warns "ignoring broken ref" at some readers only (rev-parse, log, for-each-ref:
        # yes; show-ref: no); the fake does not imitate which, so reading one fails loudly
        from kitlib.git import fake
        self.r.git('update-ref', 'refs/backlog/x', 'HEAD')
        with open(os.path.join(self.r.root, '.git', 'refs', 'backlog', 'x'), 'w') as f:
            f.write('garbage\n')
        with self.assertRaises(fake.Unsupported) as cm:
            cmd.call(['rev-parse', '--verify', '--quiet', 'refs/backlog/x^{commit}'], self.r.root)
        self.assertIn('a broken ref (refs/backlog/x)', str(cm.exception))


class FakePushIsACompareAndSwap(unittest.TestCase):
    """A remote ref that changes between push's lease check and its update is a remote rejection,
    as receive-pack's locked compare-and-swap reports it (git 2.55's words, probed 2026-10-10)."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.r.cleanup)
        self.one = self.r.git('rev-parse', 'HEAD')
        self.r.git('commit', '-q', '--allow-empty', '-m', 'two')
        self.two = self.r.git('rev-parse', 'HEAD')
        self.r.git('commit', '-q', '--allow-empty', '-m', 'three')
        self.three = self.r.git('rev-parse', 'HEAD')
        self.r.git('push', '-q', self.r.remote, '%s:refs/heads/side' % self.three)

    def push(self, ref, expect, new, moved_to=None):
        """push --porcelain with a lease; `moved_to` is written to the remote's `ref` after the
        lease is judged, before the update (None: nothing moves)."""
        from kitlib.git.fake import refs as frefs, remote as fremote
        real_copy = fremote.copy_closure

        def copy_then_move(src, dst, tips):
            real_copy(src, dst, tips)
            if moved_to is not None:
                frefs.update(dst, ref, moved_to)
        with mock.patch.object(fremote, 'copy_closure', copy_then_move):
            return cmd.subprocess_run(
                ['git', '-C', self.r.root, 'push', '--porcelain', '--quiet',
                 '--force-with-lease=%s:%s' % (ref, expect), self.r.remote, '%s:%s' % (new, ref)],
                capture_output=True, text=True)

    def remote_tip(self, ref):
        return self.r.git('rev-parse', '--verify', '-q', ref, cwd=self.r.remote, check=False)

    def test_a_ref_moved_after_the_lease_check_is_rejected_and_left_as_moved(self):
        self.r.git('push', '-q', self.r.remote, '%s:refs/backlog/x' % self.one)
        p = self.push('refs/backlog/x', self.one, self.two, moved_to=self.three)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, 'To %s\n!\t%s:refs/backlog/x\t[remote rejected] (incorrect old '
                                   'value provided)\nDone\n' % (self.r.remote, self.two))
        self.assertEqual(p.stderr, "remote: error: cannot lock ref 'refs/backlog/x': is at %s but "
                                   "expected %s        \nerror: failed to push some refs to '%s'\n"
                         % (self.three, self.one, self.r.remote))
        self.assertEqual(self.remote_tip('refs/backlog/x'), self.three)

    def test_a_ref_created_after_the_lease_check_is_rejected(self):
        p = self.push('refs/backlog/x', '', self.two, moved_to=self.three)
        self.assertEqual(p.returncode, 1)
        self.assertIn('\t[remote rejected] (reference already exists)\n', p.stdout)
        self.assertIn("remote: error: cannot lock ref 'refs/backlog/x': reference already exists",
                      p.stderr)
        self.assertEqual(self.remote_tip('refs/backlog/x'), self.three)

    def test_a_held_ref_lock_on_the_remote_is_rejected(self):
        self.r.git('push', '-q', self.r.remote, '%s:refs/backlog/x' % self.one)
        with open(os.path.join(self.r.remote, 'refs', 'backlog', 'x.lock'), 'w'):
            pass
        p = self.push('refs/backlog/x', self.one, self.two)
        self.assertEqual(p.returncode, 1)
        self.assertIn('\t[remote rejected] (reference already exists)\n', p.stdout)
        self.assertIn("remote: error: cannot lock ref 'refs/backlog/x': Unable to create '", p.stderr)
        self.assertIn("/./refs/backlog/x.lock': File exists.", p.stderr)
        self.assertEqual(self.remote_tip('refs/backlog/x'), self.one)

    def test_an_unraced_push_still_lands(self):
        self.r.git('push', '-q', self.r.remote, '%s:refs/backlog/x' % self.one)
        p = self.push('refs/backlog/x', self.one, self.two)
        self.assertEqual((p.returncode, p.stderr), (0, ''))
        self.assertEqual(self.remote_tip('refs/backlog/x'), self.two)


def index_sizes(git_dir):
    """{path: the size field} of a version 2 index, read without git."""
    with open(os.path.join(git_dir, 'index'), 'rb') as f:
        data = f.read()
    count, pos, sizes = struct.unpack('>I', data[8:12])[0], 12, {}
    for _ in range(count):
        size = struct.unpack('>I', data[pos + 36:pos + 40])[0]
        n = struct.unpack('>H', data[pos + 60:pos + 62])[0] & 0xfff
        sizes[data[pos + 62:pos + 62 + n].decode()] = size
        pos += 62 + n + (8 - (62 + n) % 8)
    return sizes


class FakeIndexStatData(unittest.TestCase):
    """The fake writes stat data real git trusts: only an entry whose stat data matches a file
    with other content is smudged (size 0), as git's ce_smudge_racily_clean_entry does. A clean
    entry smudged reads as changed to a later real git that does not refresh first (reset --keep:
    "Entry 'a.py' not uptodate"; probed 2026-10-10)."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)

    def test_a_clean_entry_just_added_keeps_its_size(self):
        self.r.write('a.py', 'x = 1\n')
        self.r.git('add', 'a.py')
        self.assertEqual(index_sizes(self.r.path('.git'))['a.py'], 6)

    def test_an_entry_whose_stat_matches_other_content_is_smudged(self):
        self.r.write('a.py', 'x = 1\n')
        self.r.git('add', 'a.py')
        st = os.stat(self.r.path('a.py'))
        self.r.write('a.py', 'x = 2\n')                              # same size, same mtime
        os.utime(self.r.path('a.py'), ns=(st.st_atime_ns, st.st_mtime_ns))
        self.r.write('b.py', 'y\n')
        self.r.git('add', 'b.py')                                    # rewrites the index
        self.assertEqual(index_sizes(self.r.path('.git'))['a.py'], 0)
        self.assertEqual(self.r.git('status', '--porcelain'), 'AM a.py\nA  b.py')

    def test_a_new_ctime_alone_leaves_the_index_when_ctime_is_not_trusted(self):
        """A copied repository's files have a new creation time (what the fake records as ctime);
        with core.trustctime false, as the fixture sets it, git's refresh ignores it and status
        leaves the index bytes alone."""
        self.assertEqual(self.r.git('config', 'core.trustctime'), 'false')
        self.r.write('a.py', 'x = 1\n')
        old = time.time_ns() - 10_000_000_000                        # not racily clean
        os.utime(self.r.path('a.py'), ns=(old, old))
        self.r.git('add', 'a.py')
        copy = os.path.join(self.r.base, 'copy')
        shutil.copytree(self.r.root, copy)                           # new files: new creation times
        index = os.path.join(copy, '.git', 'index')
        with open(index, 'rb') as f:
            before = f.read()
        recorded = struct.unpack('>II', before[12:20])
        born = os.stat(os.path.join(copy, 'a.py')).st_birthtime_ns
        self.assertNotEqual(recorded, divmod(born, 1_000_000_000), 'the copy kept its ctime')
        self.assertEqual(self.r.git('status', '--porcelain', cwd=copy), 'A  a.py')
        with open(index, 'rb') as f:
            self.assertEqual(f.read(), before)


class FakePatchNames(unittest.TestCase):
    """A patch names a path as git does: prefix and path C-quoted together per core.quotePath,
    and a `---`/`+++` name with a space ends in a tab (the bytes real git 2.x printed for the
    same repository, 2026-10-10)."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.r.write('docs/naïve 0 b.md', 'PM\n')
        self.r.commit('add', 'docs')

    def headers(self, quotepath):
        out, why = cmd.run(['-c', 'core.quotePath=' + quotepath, 'diff', '--no-color',
                            self.r.initial], self.r.root)
        self.assertIsNone(why)
        return [line for line in out.split(b'\n') if line.startswith((b'diff', b'---', b'+++'))]

    def test_a_quoted_name(self):
        self.assertEqual(self.headers('true'), [
            b'diff --git "a/docs/na\\303\\257ve 0 b.md" "b/docs/na\\303\\257ve 0 b.md"',
            b'--- /dev/null', b'+++ "b/docs/na\\303\\257ve 0 b.md"\t'])

    def test_a_name_with_quoting_off(self):
        name = 'docs/naïve 0 b.md'.encode('utf-8')
        self.assertEqual(self.headers('false'), [
            b'diff --git a/' + name + b' b/' + name, b'--- /dev/null', b'+++ b/' + name + b'\t'])


class FakeRefReadRace(unittest.TestCase):
    """Windows denies opening a ref file while another thread's replace lands on it, and creating
    a ref lock while the last holder's lock file is still being deleted (both seen as
    PermissionError in backlog test_store CompareAndSwap under load); the fake waits for that
    window, as its write waits for a reader, instead of failing the git call."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.r.write('a.txt', 'a\n')
        self.sha = self.r.commit('add', 'a.txt')
        self.loose = os.path.normcase(os.path.join(self.r.root, '.git', 'refs', 'heads', 'main'))
        self.assertTrue(os.path.isfile(self.loose), 'main is a loose ref')

    def denied(self, times):
        from kitlib.git.fake import refs
        real_open, denials = open, []

        def opener(path, *a, **kw):
            if os.path.normcase(os.fspath(path)) == self.loose and len(denials) < times:
                denials.append(path)
                raise PermissionError(13, 'Permission denied', path)
            return real_open(path, *a, **kw)
        return mock.patch.object(refs, 'open', opener, create=True), denials

    def test_a_read_during_a_replace_waits_for_it(self):
        patch, denials = self.denied(3)
        with patch:
            out, why = cmd.run(['rev-parse', 'refs/heads/main'], self.r.root)
        self.assertEqual(len(denials), 3)
        self.assertIsNone(why)
        self.assertEqual(out.decode().strip(), self.sha)

    def test_a_lock_taken_while_the_last_one_is_still_being_deleted_waits_for_it(self):
        lock = os.path.normcase(os.path.join(self.r.root, '.git', 'refs', 'heads', 'other.lock'))
        real_open, denials = os.open, []

        def opener(path, *a, **kw):
            if os.path.normcase(os.fspath(path)) == lock and len(denials) < 3:
                denials.append(path)
                raise PermissionError(13, 'Permission denied', path)
            return real_open(path, *a, **kw)
        with mock.patch.object(os, 'open', opener):
            out, why = cmd.run(['update-ref', 'refs/heads/other', self.sha], self.r.root)
        self.assertEqual(len(denials), 3)
        self.assertIsNone(why)
        self.assertEqual(cmd.run(['rev-parse', 'refs/heads/other'], self.r.root)[0].decode().strip(),
                         self.sha)

    def test_a_ref_update_whose_replace_fails_leaves_no_lock_behind(self):
        """A lock left behind would fail every later update of the ref ("cannot lock ref")."""
        from kitlib.git.fake import refs

        def refused(src, dst):
            raise PermissionError(13, 'Permission denied', dst)
        with mock.patch.object(refs, '_replace', refused):
            with self.assertRaises(PermissionError):
                cmd.call(['update-ref', 'refs/heads/other', self.sha], self.r.root)
        self.assertFalse(os.path.exists(os.path.join(self.r.root, '.git', 'refs', 'heads',
                                                     'other.lock')))
        out, why = cmd.run(['update-ref', 'refs/heads/other', self.sha], self.r.root)
        self.assertIsNone(why)


class FakeUnreadableObjects(unittest.TestCase):
    """An object that exists but cannot be opened for a moment (Windows denies the open while
    another holder excludes readers) is waited for, and a tree that is missing ends the command
    as git's does. Taking either for an empty tree made `read-tree` drop the records under it:
    the store write built on that index lost every earlier record (backlog test_store
    CompareAndSwap)."""

    def setUp(self):
        if not cmd.fake_active():
            self.skipTest('the run is on real git (kit_tests.py --real-git)')
        self.r = gitfixture.TempRepo()
        self.addCleanup(self.r.cleanup)
        self.r.write('d/a.txt', 'a\n')
        self.r.write('b.txt', 'b\n')
        self.r.commit('add', 'd', 'b.txt')
        self.sub = self.r.git('rev-parse', 'HEAD:d')
        self.blob = self.r.git('rev-parse', 'HEAD:d/a.txt')

    def loose(self, oid):
        return os.path.join(self.r.root, '.git', 'objects', oid[:2], oid[2:])

    def read_tree(self):
        index = os.path.join(self.r.base, 'other-index')
        r = cmd.call(['read-tree', 'HEAD'], self.r.root,
                     env=dict(cmd.scrubbed_env(), GIT_INDEX_FILE=index))
        return r, index

    def test_an_object_read_during_a_replace_waits_for_it(self):
        from kitlib.git.fake import objects
        path, real_open, denials = os.path.normcase(self.loose(self.sub)), open, []

        def opener(p, *a, **kw):
            if os.path.normcase(os.fspath(p)) == path and len(denials) < 3:
                denials.append(p)
                raise PermissionError(13, 'Permission denied', p)
            return real_open(p, *a, **kw)
        with mock.patch.object(objects, 'open', opener, create=True):
            r, index = self.read_tree()
        self.assertEqual((r.rc, r.err), (0, ''))
        self.assertEqual(len(denials), 3)
        out, why = cmd.run(['ls-files', '-s'], self.r.root,
                           env=dict(cmd.scrubbed_env(), GIT_INDEX_FILE=index))
        self.assertIsNone(why)
        self.assertIn(('%s 0\td/a.txt' % self.blob), out.decode())

    def test_read_tree_of_a_tree_whose_subtree_is_missing_dies_as_git_does(self):
        os.chmod(self.loose(self.sub), 0o666)
        os.remove(self.loose(self.sub))
        r, index = self.read_tree()
        # git 2.55: fatal: unable to read tree (<oid>), exit 128, and no index written
        self.assertEqual((r.rc, r.err), (128, 'fatal: unable to read tree (%s)' % self.sub))
        self.assertFalse(os.path.exists(index))
        self.assertFalse(os.path.exists(index + '.lock'))

    def test_a_path_through_a_missing_tree_is_missing_not_fatal(self):
        """git 2.55 (probed 2026-10-10): `rev-parse --verify -q HEAD:d/a` exits 1 saying nothing,
        and `cat-file --batch-check` answers "HEAD:d/a missing" and goes on."""
        os.chmod(self.loose(self.sub), 0o666)
        os.remove(self.loose(self.sub))
        r = cmd.call(['rev-parse', '--verify', '-q', 'HEAD:d/a.txt'], self.r.root)
        self.assertEqual((r.rc, r.out, r.err), (1, b'', ''))
        r = cmd.call(['cat-file', '--batch-check'], self.r.root, stdin=b'HEAD:d/a.txt\nHEAD:b.txt\n')
        self.assertEqual(r.rc, 0, r.err)
        lines = r.out.decode().splitlines()
        self.assertEqual(lines[0], 'HEAD:d/a.txt missing')
        self.assertTrue(lines[1].endswith(' blob 2'), lines)

    def test_the_empty_tree_needs_no_object(self):
        from kitlib.git.fake import objects
        empty = objects.EMPTY_TREE['sha1']
        self.assertFalse(os.path.exists(self.loose(empty)))
        from kitlib.git.fake import repo as frepo, revs
        repo, _ = frepo.discover(self.r.root, dict(os.environ), [], None)
        self.assertEqual(revs.tree_entries(repo, empty), [])
        self.assertEqual(revs.flatten_tree(repo, empty), {})


if __name__ == '__main__':
    unittest.main()
