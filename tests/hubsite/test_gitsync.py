"""kitlib.hubsite.gitsync: the store folder's debounced commit, then fetch, fast-forward and push
with explicit refspecs; what a refused or failed step leaves pending; the refusals; the thread.

The logic runs on the fake git. One class, EndToEndTest, runs on real git: two repositories and a
bare remote in temp folders, and `.git/config` byte-identical before and after."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import commit as kcommit
from kitlib import pmdata
from kitlib.git import cmd as gitcmd
from kitlib.hubsite import gitsync
from kitlib.hubsite.store import LOCK_NAME, Store
from tests.gitfixture import EMAIL, NAME, TempRepo, fixture_env
from tests.realgit import real_git

FETCH = ['fetch', '-q', 'origin', 'refs/heads/pmdata:refs/remotes/origin/pmdata']
PUSH = ['push', '-q', 'origin', 'refs/heads/pmdata:refs/heads/pmdata']
LOCK = '?? hub/' + LOCK_NAME


def tmpdir(test):
    d = tempfile.mkdtemp(prefix='kit-gitsync-')
    test.addCleanup(shutil.rmtree, d, True)
    return d


class Case(unittest.TestCase):
    """A repository with a bare remote `origin` and a linked work tree on `pmdata` (the data
    worktree's shape: docs/pm-data-branch.md "Layout") holding the store folder `hub`. The
    session's git identity and global config never reach git, the kit root is a folder outside
    git, the clock is a number the test moves, and every git command of a pass is recorded."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
            os.environ.pop(k, None)
        os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
        os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
        root = mock.patch.object(kcommit, 'KIT_ROOT', tmpdir(self))     # not the kit's repository
        root.start()
        self.addCleanup(root.stop)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.wt = self.repo.worktree('data', branch='pmdata')
        self.hub = os.path.join(self.wt, 'hub')
        self.env = fixture_env()
        self.now = [1000.0]
        self.calls = []             # the argv of every git command a pass ran
        self.envs = []              # (subcommand, env) of each
        self.answers = {}           # subcommand -> a Result given instead of running git
        self.before = {}            # subcommand -> called just before that command runs

    def runner(self, args, cwd, **kw):
        sub = gitcmd.subcommand(args)
        self.calls.append(list(args))
        self.envs.append((sub, kw.get('env')))
        if sub in self.before:
            self.before[sub]()
        forced = self.answers.get(sub)
        if isinstance(forced, BaseException):
            raise forced
        if forced is not None:
            return forced
        return gitcmd.call(args, cwd, **kw)

    def sync(self, every=60, remote='origin', store=None, **kw):
        kw.setdefault('clock', lambda: self.now[0])
        kw.setdefault('git', self.runner)
        s = gitsync.GitSync(store or self.hub, every, remote=remote, env=self.env, **kw)
        self.addCleanup(s.stop)
        return s

    def store(self, root=None):
        s = Store.open(root or self.hub)
        self.addCleanup(s.close)
        return s

    def write(self, rel, text='{}\n', root=None):
        return self.repo.write(rel, text, root=root or self.wt)

    def git(self, *args, **kw):
        kw.setdefault('cwd', self.wt)
        return self.repo.git(*args, **kw)

    def head(self):
        return self.repo.head(self.wt)

    def remote_tip(self):
        return self.repo.git('rev-parse', '--verify', 'refs/heads/pmdata', cwd=self.repo.remote,
                             check=False)

    def committed(self, rev='HEAD'):
        return sorted(self.git('show', '--name-only', '--format=', rev).split())

    def hub_status(self):
        return self.git('status', '--porcelain', '--untracked-files=all', '--', 'hub')

    def subs(self):
        return [gitcmd.subcommand(a) for a in self.calls]

    def config_bytes(self, root=None):
        with open(os.path.join(root or self.repo.root, '.git', 'config'), 'rb') as f:
            return f.read()

    def clone(self, name='other'):
        """A second repository cloned from the bare remote, on pmdata, with the fixture's
        identity in its own config (the test's setup, not the sync's)."""
        path = os.path.join(self.repo.base, name)
        self.repo.git('clone', '-q', '-b', 'pmdata', self.repo.remote, path, cwd=self.repo.base)
        self.repo.git('config', 'user.name', NAME, cwd=path)
        self.repo.git('config', 'user.email', EMAIL, cwd=path)
        return path

    def move_remote(self, clone, rel='backlog/note.json', text='{"n": 1}\n'):
        """Another machine's commit: `rel` written in `clone`, committed and pushed. Its hash."""
        self.write(rel, text, root=clone)
        self.repo.git('add', '--', rel, cwd=clone)
        self.repo.git('commit', '-q', '-m', 'elsewhere', cwd=clone)
        self.repo.git('push', '-q', 'origin', 'refs/heads/pmdata:refs/heads/pmdata', cwd=clone)
        return self.repo.head(clone)


class PassTest(Case):

    def test_a_store_write_is_committed_and_pushed_once_the_period_has_passed(self):
        before = self.config_bytes()
        sync = self.sync(every=60)
        first = self.head()
        self.store().put('pages', 'home', {'title': 'one'})
        self.assertIsNone(sync.poll())                      # written, but the period has not passed
        self.now[0] += 59
        self.assertIsNone(sync.poll())
        self.assertEqual((self.calls, self.head()), ([], first))
        self.assertIsNone(sync.status()['ok'])
        self.now[0] += 1
        st = sync.poll()
        self.assertEqual((st['ok'], st['failed'], st['detail']), (True, '', ''))
        self.assertEqual(self.git('log', '-1', '--format=%B').strip(), gitsync.SUBJECT)
        self.assertEqual(self.git('log', '-1', '--format=%an <%ae>'), '%s <%s>' % (NAME, EMAIL))
        self.assertEqual(self.git('log', '-1', '--format=%P'), first)
        self.assertEqual(self.committed(), ['hub/_meta.json', 'hub/pages/home.json'])
        self.assertEqual(self.remote_tip(), self.head())    # the push made the branch on the remote
        self.assertEqual(self.hub_status(), LOCK)           # the lock file is never committed
        self.assertEqual((st['pending_commit'], st['pending_push'], st['diverged']),
                         (False, False, False))
        self.assertEqual((st['last_commit'], st['last_commit_at'], st['last_push_at'],
                          st['last_pass_at'], st['failing_since']),
                         (self.head(), 1060.0, 1060.0, 1060.0, None))
        self.assertEqual((st['store'], st['branch'], st['remote'], st['every'], st['running']),
                         ('hub', 'pmdata', 'origin', 60, False))
        self.assertIsNone(sync.poll())                      # at most one pass a period
        self.assertEqual(self.config_bytes(), before)

    def test_the_next_pass_is_due_a_period_after_the_last_one_ended(self):
        sync = self.sync(every=60)
        self.write('hub/pages/a.json')

        def slow():
            self.now[0] += 100                              # a fetch that took 100 seconds
        self.before['fetch'] = slow
        self.now[0] += 60
        self.assertTrue(sync.poll()['ok'])                  # began at 1060, ended at 1160
        self.now[0] = 1219
        self.assertIsNone(sync.poll())
        self.now[0] = 1220
        self.assertTrue(sync.poll()['ok'])

    def test_every_fetch_and_push_names_its_refspec_and_nothing_is_configured_or_forced(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        self.assertEqual([a for a in self.calls if a[0] == 'fetch'], [FETCH])
        self.assertEqual([a for a in self.calls if a[0] == 'push'], [PUSH])
        self.assertEqual([a for a in self.calls if a[0] == 'commit'],
                         [['commit', '-q', '-m', gitsync.SUBJECT, '--', 'hub']])
        self.assertEqual([a for a in self.calls if a[0] == 'add'], [['add', '--', 'hub/pages/a.json']])
        self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse', 'fetch', 'add',
                                            'commit', 'push'})
        for argv in self.calls:
            for arg in argv:
                self.assertFalse(arg.startswith('+') or arg in ('-f', '--force', '--no-verify', '-n')
                                 or arg.startswith(('--force', '--author', '-c')), argv)
        push_env = [e for s, e in self.envs if s == 'push'][0]
        self.assertEqual((push_env['GIT_TERMINAL_PROMPT'], push_env['GCM_INTERACTIVE']),
                         ('0', 'never'))

    def test_a_remote_that_moved_is_fast_forwarded_before_the_push(self):
        sync = self.sync()
        store = self.store()
        store.put('pages', 'home', {'title': 'one'})
        self.assertTrue(sync.sync_once()['ok'])
        moved = self.move_remote(self.clone())              # another machine pushed
        self.assertNotEqual(moved, self.head())
        store.put('pages', 'two', {'title': 'two'})
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['detail']), (True, '', ''))
        self.assertEqual(self.git('log', '-1', '--format=%P'), moved)   # one parent: no merge commit
        self.assertEqual(self.committed(), ['hub/_meta.json', 'hub/pages/two.json'])
        self.assertTrue(os.path.isfile(os.path.join(self.wt, 'backlog', 'note.json')))
        self.assertEqual(self.remote_tip(), self.head())
        subs = self.subs()
        self.assertLess(subs.index('fetch'), subs.index('merge'))
        self.assertLess(subs.index('merge'), subs.index('commit'))
        self.assertLess(subs.index('commit'), subs.index('push'))
        self.assertIn(['merge', '-q', '--ff-only', 'refs/remotes/origin/pmdata'], self.calls)

    def test_a_remote_that_moves_between_the_fetch_and_the_push_is_reported_not_forced(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        clone = self.clone()
        moved = []

        def race():                                         # another machine pushes just now
            if not moved:
                moved.append(self.move_remote(clone))
        self.before['push'] = race
        self.write('hub/pages/b.json')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed']), (False, 'push'))
        self.assertTrue(st['detail'])
        self.assertEqual((st['pending_commit'], st['pending_push'], st['last_push_at']),
                         (False, True, 1000.0))             # the first pass's push, not this one
        mine = self.head()
        self.assertEqual(self.committed(), ['hub/pages/b.json'])
        self.assertEqual(self.remote_tip(), moved[0])       # the remote keeps the other commit
        st = sync.sync_once()                               # the next pass sees two histories
        self.assertEqual((st['failed'], st['diverged'], st['pending_push']),
                         ('fast-forward', True, True))
        self.assertEqual((self.head(), self.remote_tip()), (mine, moved[0]))

    def test_a_remote_that_moved_with_nothing_to_commit_but_a_push_waiting(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        self.answers['push'] = gitcmd.Result(1, b'', 'error: failed to push some refs')
        self.write('hub/pages/b.json')
        self.assertEqual(sync.sync_once()['failed'], 'push')
        mine = self.head()
        del self.answers['push']
        moved = self.move_remote(self.clone())
        st = sync.sync_once()                               # mine and the remote's have diverged
        self.assertEqual((st['failed'], st['diverged'], st['pending_push']),
                         ('fast-forward', True, True))
        self.assertEqual((self.head(), self.remote_tip()), (mine, moved))

    def test_diverged_histories_are_reported_and_nothing_is_committed_or_forced(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        moved = self.move_remote(self.clone())
        self.write('local.txt', 'made here, never pushed\n')
        mine = self.repo.commit('local', 'local.txt', cwd=self.wt)
        self.write('hub/pages/b.json')
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['diverged']), (False, 'fast-forward', True))
        self.assertEqual((st['pending_commit'], st['pending_push'], st['failing_since']),
                         (True, True, 1000.0))
        self.assertTrue(st['detail'])
        self.assertEqual((self.head(), self.remote_tip()), (mine, moved))
        self.assertEqual(self.hub_status(), '?? hub/pages/b.json')
        self.assertEqual([s for s in self.subs() if s in ('add', 'commit', 'push', 'reset')], [])
        self.assertEqual([a for a in self.calls if a[0] == 'fetch'], [FETCH])
        self.now[0] += 60
        st = sync.sync_once()                               # still diverged a pass later
        self.assertEqual((st['failed'], st['diverged'], st['failing_since'], st['last_pass_at']),
                         ('fast-forward', True, 1000.0, 1060.0))
        self.assertEqual((self.head(), self.remote_tip()), (mine, moved))

    def test_a_fast_forward_the_stores_own_changes_block_is_not_a_divergence(self):
        sync = self.sync()
        self.write('hub/_meta.json', '{"revision": 1}\n')
        self.assertTrue(sync.sync_once()['ok'])
        first = self.head()
        moved = self.move_remote(self.clone(), 'hub/_meta.json', '{"revision": 2}\n')
        self.write('hub/_meta.json', '{"revision": 3}\n')   # this store wrote too
        # git's refusal, given here: the fake git does not model a fast-forward that local changes
        # block (EndToEndTest runs the same case on real git).
        self.answers['merge'] = gitcmd.Result(
            1, b'', 'error: Your local changes to the following files would be overwritten by '
                    'merge:\n\thub/_meta.json\nAborting\n')
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['diverged']), (False, 'fast-forward', False))
        self.assertTrue(st['detail'].startswith('error: Your local changes'))
        self.assertEqual((st['pending_commit'], st['pending_push']), (True, False))
        self.assertEqual([s for s in self.subs() if s in ('add', 'commit', 'push', 'reset')], [])
        self.assertEqual((self.head(), self.remote_tip()), (first, moved))
        with open(os.path.join(self.hub, '_meta.json'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '{"revision": 3}\n')     # the store's text is untouched

    def test_a_refused_commit_stays_pending_and_is_retried(self):
        sync = self.sync()
        first = self.head()
        self.write('hub/pages/a.json')
        self.answers['commit'] = gitcmd.Result(1, b'', 'hint: fix it\nerror: the hook said no\n')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['detail']),
                         (False, 'commit', 'error: the hook said no'))
        self.assertEqual((st['pending_commit'], st['last_commit'], st['failing_since']),
                         (True, '', 1000.0))
        self.assertEqual(self.head(), first)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')   # nothing left staged
        self.assertEqual(self.hub_status(), '?? hub/pages/a.json')
        self.assertEqual(self.calls[-1][:2], ['push', '-q'])    # the branch itself still went
        del self.answers['commit']
        self.now[0] += 60
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['pending_commit'], st['failing_since']), (True, False, None))
        self.assertEqual(self.committed(), ['hub/pages/a.json'])
        self.assertEqual(self.remote_tip(), self.head())

    def test_a_failed_add_leaves_nothing_staged(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.answers['add'] = gitcmd.Result(128, b'', "fatal: pathspec 'x' did not match any files")
        st = sync.sync_once()
        self.assertEqual((st['failed'], st['pending_commit']), ('commit', True))
        self.assertNotIn('commit', self.subs())
        self.assertIn(['reset', '-q', '--', 'hub'], self.calls)

    def test_a_failed_push_stays_pending_and_is_retried_with_nothing_new_written(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.answers['push'] = gitcmd.Result(None, b'', 'git push timed out after 120 seconds')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['detail']),
                         (False, 'push', 'git push timed out after 120 seconds'))
        self.assertEqual((st['pending_commit'], st['pending_push'], st['last_push_at']),
                         (False, True, None))
        self.assertEqual(self.committed(), ['hub/pages/a.json'])    # committed here all the same
        self.assertEqual(self.remote_tip(), '')
        del self.answers['push']
        self.now[0] += 60
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['pending_push'], st['last_push_at']), (True, False, 1060.0))
        self.assertEqual(self.remote_tip(), self.head())

    def test_with_no_network_the_commit_is_made_and_the_push_waits(self):
        sync = self.sync()
        first = self.head()
        self.write('hub/pages/a.json')
        self.answers['fetch'] = gitcmd.Result(128, b'', 'fatal: unable to access the remote')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['detail']),
                         (False, 'fetch', 'fatal: unable to access the remote'))
        self.assertEqual((st['pending_commit'], st['pending_push']), (False, True))
        self.assertNotEqual(self.head(), first)
        self.assertNotIn('push', self.subs())
        del self.answers['fetch']
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['pending_push']), (True, False))
        self.assertEqual(self.remote_tip(), self.head())

    def test_only_the_store_folder_is_committed(self):
        sync = self.sync()
        self.store().put('pages', 'home', {'title': 'one'})
        self.write('other.txt', 'staged by somebody else\n')
        self.git('add', '--', 'other.txt')
        self.write('loose.txt', 'not staged\n')
        self.write('hub/pages/.home.json.4242.tmp', 'a write in flight\n')
        self.assertTrue(sync.sync_once()['ok'])
        self.assertEqual(self.committed(), ['hub/_meta.json', 'hub/pages/home.json'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'other.txt')
        self.assertEqual(sorted(self.git('status', '--porcelain', '--untracked-files=all')
                                .splitlines()),
                         ['?? hub/_store.lock', '?? hub/pages/.home.json.4242.tmp',
                          '?? loose.txt', 'A  other.txt'])

    def test_a_changed_and_a_deleted_document_are_committed(self):
        sync = self.sync()
        store = self.store()
        store.put('pages', 'home', {'title': 'one'})
        store.put('pages', 'gone', {'title': 'soon'})
        self.assertTrue(sync.sync_once()['ok'])
        store.update('pages', 'home', {'title': 'changed'})
        store.delete('pages', 'gone')
        self.assertTrue(sync.sync_once()['ok'])
        self.assertEqual(self.committed(), ['hub/_meta.json', 'hub/pages/gone.json',
                                            'hub/pages/home.json'])
        self.assertEqual(self.git('ls-files', 'hub').split(), ['hub/_meta.json', 'hub/pages/home.json'])
        self.assertEqual(self.hub_status(), LOCK)
        self.assertEqual(self.remote_tip(), self.head())

    def test_an_idle_pass_runs_no_fetch_and_no_push(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        tip = self.head()
        del self.calls[:]
        self.now[0] += 60
        st = sync.poll()
        self.assertEqual((st['ok'], st['pending_commit'], st['pending_push']), (True, False, False))
        self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse'})
        self.assertEqual(self.head(), tip)

    def test_a_branch_switched_under_the_sync_ends_the_pass_before_a_write(self):
        sync = self.sync()
        self.git('checkout', '-q', '-b', 'side')
        tip = self.head()
        self.write('hub/pages/a.json')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed']), (False, 'branch'))
        self.assertIn("'side'", st['detail'])
        self.assertEqual(self.calls, [['branch', '--show-current']])
        self.assertEqual((self.head(), self.hub_status()), (tip, '?? hub/pages/a.json'))

    def test_a_pass_that_raises_is_recorded_not_raised(self):
        sync = self.sync()
        self.answers['status'] = RuntimeError('boom')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['detail']),
                         (False, 'internal', 'RuntimeError: boom'))
        del self.answers['status']
        self.assertTrue(sync.sync_once()['ok'])

    def test_a_failed_status_is_a_failed_pass(self):
        sync = self.sync()
        self.answers['status'] = gitcmd.Result(None, b'', 'git status timed out after 30 seconds')
        st = sync.sync_once()
        self.assertEqual((st['failed'], st['detail']),
                         ('status', 'git status timed out after 30 seconds'))
        self.assertEqual(self.subs(), ['branch', 'status'])

    def test_without_a_remote_or_with_push_off_it_commits_and_stops_there(self):
        for kw in ({'remote': None}, {'remote': 'origin', 'push': False}):
            with self.subTest(kw=kw):
                del self.calls[:]
                sync = self.sync(**kw)
                self.write('hub/pages/%d.json' % len(kw))
                st = sync.sync_once()
                self.assertEqual((st['ok'], st['remote'], st['pending_commit'], st['pending_push']),
                                 (True, '', False, False))
                self.assertEqual(st['last_commit'], self.head())
                self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse', 'add', 'commit'})
        self.assertEqual(self.remote_tip(), '')

    def test_many_new_files_are_added_in_runs(self):
        self.assertEqual(list(gitsync._chunks(['aaaa', 'bbbb', 'cccc'], limit=10)),
                         [['aaaa', 'bbbb'], ['cccc']])
        self.assertEqual(list(gitsync._chunks(['a' * 50], limit=10)), [['a' * 50]])
        self.assertEqual(list(gitsync._chunks([], limit=10)), [])
        sync = self.sync()
        for n in range(3):
            self.write('hub/pages/p%d.json' % n)
        with mock.patch.object(gitsync, 'ADD_CHARS', 40):
            self.assertTrue(sync.sync_once()['ok'])
        self.assertEqual([a for a in self.calls if a[0] == 'add'],
                         [['add', '--', 'hub/pages/p0.json', 'hub/pages/p1.json'],
                          ['add', '--', 'hub/pages/p2.json']])
        self.assertEqual(self.committed(), ['hub/pages/p0.json', 'hub/pages/p1.json',
                                            'hub/pages/p2.json'])


class PrepareTest(Case):

    def test_the_plan(self):
        self.assertEqual(tuple(gitsync.prepare(self.hub, remote='origin', branch='pmdata'))[1:],
                         ('hub', 'pmdata', 'origin'))
        plan = gitsync.prepare(os.path.join(self.wt, 'data', 'hub'))    # no folder yet, no remote
        self.assertEqual((plan.rel, plan.branch, plan.remote), ('data/hub', 'pmdata', None))
        self.assertTrue(os.path.samefile(plan.root, self.wt))

    def test_a_checkout_of_the_kit_is_refused(self):
        self.write('kitlib/hubmirror/__init__.py', '')      # the kit's code
        with self.assertRaises(gitsync.Refused) as cm:
            self.sync()
        self.assertIn('a checkout of the kit itself', str(cm.exception))

    def test_the_kits_repository_syncs_only_its_recorded_data_branch_and_remote(self):
        with mock.patch.object(kcommit, 'KIT_ROOT', self.repo.root):    # this repository is the kit's
            with self.assertRaises(gitsync.Refused) as cm:              # no kit-pm-data.json
                gitsync.prepare(self.hub, remote='origin')
            self.assertIn('not its data branch (no kit-pm-data.json', str(cm.exception))
            pmdata.write(self.repo.common_dir(), 'pmdata', 'origin', self.head())
            self.assertEqual(gitsync.prepare(self.hub).remote, 'origin')    # the recorded remote
            with self.assertRaises(gitsync.Refused) as cm:
                gitsync.prepare(self.hub, remote='elsewhere')
            self.assertIn('not elsewhere', str(cm.exception))
            side = self.repo.worktree('side')                           # a code branch of the kit
            with self.assertRaises(gitsync.Refused) as cm:
                gitsync.prepare(os.path.join(side, 'hub'), remote='origin')
            self.assertIn("private hub data in the kit's history", str(cm.exception))

    def test_refusals(self):
        for store, kw, text in (
                (self.wt, {}, 'the root of its work tree'),
                (os.path.join(tmpdir(self), 'hub'), {}, 'in no git work tree'),
                (self.hub, {'branch': 'other'}, "is on 'pmdata', not the data branch other"),
                (self.hub, {'remote': '--upload-pack=x'}, 'must be a remote name'),
                (self.hub, {'remote': 'a b'}, 'must be a remote name'),
                (os.path.join(self.wt, 'h[ub]'), {}, 'git reads as a pattern'),
                (os.path.join(self.wt, '.git', 'hub'), {}, 'inside the git directory')):
            with self.subTest(text=text):
                with self.assertRaises(gitsync.Refused) as cm:
                    gitsync.prepare(store, **kw)
                self.assertIn(text, str(cm.exception))
        self.git('checkout', '-q', '--detach')
        with self.assertRaises(gitsync.Refused) as cm:
            gitsync.prepare(self.hub)
        self.assertIn('detached', str(cm.exception))

    def test_the_period_must_be_a_positive_number(self):
        for bad in (0, -1, True, '60', None, float('inf'), float('nan')):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gitsync.GitSync(self.hub, bad, remote='origin')

    def test_what_is_never_committed(self):
        self.assertTrue(gitsync.skipped('hub/' + LOCK_NAME))
        self.assertTrue(gitsync.skipped('hub/pages/.home.json.4242.tmp'))
        for kept in ('hub/_meta.json', 'hub/pages/home.json', 'hub/pages/a.tmp',
                     'hub/pages/.hidden.json'):
            self.assertFalse(gitsync.skipped(kept), kept)


class ThreadTest(Case):
    """The thread, in real time with a short period."""

    def wait_for(self, cond, seconds=30):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_the_thread_commits_a_store_write_and_stops_cleanly(self):
        sync = self.sync(every=0.1, clock=time.monotonic)
        first = self.head()
        self.assertTrue(sync.start())
        self.assertFalse(sync.start())                      # already running
        self.assertTrue(sync.status()['running'])
        self.store().put('pages', 'home', {'title': 'one'})
        self.assertTrue(self.wait_for(lambda: sync.status()['last_commit']))
        self.assertTrue(self.wait_for(lambda: sync.status()['last_push_at'] is not None))
        self.assertTrue(sync.stop())
        st = sync.status()
        self.assertEqual((st['running'], st['ok'], st['pending_commit'], st['pending_push']),
                         (False, True, False, False))
        self.assertNotEqual(self.head(), first)
        self.assertEqual(self.head(), st['last_commit'])
        self.assertEqual(self.git('log', '-1', '--format=%s'), gitsync.SUBJECT)
        self.assertEqual(self.remote_tip(), self.head())
        self.assertNotIn('hub-gitsync', [t.name for t in threading.enumerate()])

    def test_stop_before_start_and_a_second_start(self):
        sync = self.sync(every=0.05, clock=time.monotonic)
        self.assertTrue(sync.stop())                        # nothing to stop
        for _ in range(2):
            self.assertTrue(sync.start())
            self.assertTrue(self.wait_for(lambda: sync.status()['ok'] is not None))
            self.assertTrue(sync.stop())
            self.assertFalse(sync.status()['running'])
        self.assertTrue(sync.stop())

    def test_stop_ends_a_thread_that_is_waiting_out_a_long_period(self):
        sync = self.sync(every=3600, clock=time.monotonic)
        sync.start()
        self.assertTrue(self.wait_for(lambda: sync.status()['ok'] is not None))    # the pass at start
        began = time.monotonic()
        self.assertTrue(sync.stop())
        self.assertLess(time.monotonic() - began, 10)

    def test_the_thread_outlives_a_pass_that_raises(self):
        self.answers['status'] = RuntimeError('boom')
        sync = self.sync(every=0.05, clock=time.monotonic)
        sync.start()
        self.assertTrue(self.wait_for(lambda: sync.status()['failed'] == 'internal'))
        del self.answers['status']
        self.assertTrue(self.wait_for(lambda: sync.status()['ok']))
        self.assertTrue(sync.status()['running'])
        self.assertTrue(sync.stop())


@real_git('end to end on the git binary: the wire between two repositories and a bare remote, '
          "the store's lock held while git reads the folder, and .git/config as git leaves it")
class EndToEndTest(Case):
    """Two repositories and one bare remote, all in temp folders; the default git runner."""

    def sync(self, store=None, **kw):
        s = gitsync.GitSync(store or self.hub, 60, remote='origin', branch='pmdata',
                            clock=lambda: self.now[0], env=self.env, **kw)
        self.addCleanup(s.stop)
        return s

    def test_write_commit_push_and_a_second_clone_fast_forwards(self):
        mine = self.sync()
        store = self.store()                                # holds hub/_store.lock throughout
        before = self.config_bytes()
        store.put('pages', 'home', {'title': 'one'})
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        first = self.head()
        self.assertEqual(self.git('log', '-1', '--format=%B').strip(), gitsync.SUBJECT)
        self.assertEqual(self.committed(), ['hub/_meta.json', 'hub/pages/home.json'])
        self.assertEqual(self.remote_tip(), first)
        self.assertEqual(self.hub_status(), LOCK)

        other = self.clone()                                # another machine
        other_before = self.config_bytes(other)
        moved = self.move_remote(other)
        store.put('pages', 'two', {'title': 'two'})
        st = mine.sync_once()                               # the remote moved: fast-forward first
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        self.assertEqual(self.git('log', '-1', '--format=%P'), moved)
        self.assertEqual(self.git('rev-list', '--merges', '--count', 'HEAD'), '0')
        self.assertEqual(self.remote_tip(), self.head())
        self.assertTrue(os.path.isfile(os.path.join(self.wt, 'backlog', 'note.json')))

        theirs = self.sync(store=os.path.join(other, 'hub'))    # the second clone's own sync
        self.write('hub/pages/three.json', '{"title": "three"}\n', root=other)
        st = theirs.sync_once()
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        self.assertEqual(self.repo.git('log', '-1', '--format=%P', cwd=other), self.head())
        self.assertTrue(os.path.isfile(os.path.join(other, 'hub', 'pages', 'two.json')))
        self.assertEqual(self.remote_tip(), self.repo.head(other))
        self.assertEqual(self.repo.git('rev-list', '--merges', '--count', 'HEAD', cwd=other), '0')

        st = mine.sync_once()                               # idle here: nothing fetched
        self.assertEqual((st['ok'], st['pending_push']), (True, False))
        self.assertEqual(self.config_bytes(), before)
        self.assertEqual(self.config_bytes(other), other_before)

    def test_diverged_and_blocked_fast_forwards_are_reported_and_force_nothing(self):
        mine = self.sync()
        store = self.store()
        store.put('pages', 'home', {'title': 'one'})
        self.assertTrue(mine.sync_once()['ok'])
        before = self.config_bytes()
        other = self.clone()
        moved = self.move_remote(other, 'hub/_meta.json', '{"counters": {}, "revision": 9}\n')
        first = self.head()
        store.put('pages', 'two', {'title': 'two'})         # rewrites hub/_meta.json here too
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['diverged']), (False, 'fast-forward', False))
        self.assertEqual((st['pending_commit'], st['pending_push']), (True, False))
        self.assertEqual((self.head(), self.remote_tip()), (first, moved))
        self.assertTrue(os.path.isfile(os.path.join(self.hub, 'pages', 'two.json')))

        self.git('add', '--', 'hub')                        # a person commits by hand: now diverged
        self.git('commit', '-q', '-m', 'by hand', '--', 'hub/_meta.json', 'hub/pages/two.json')
        local = self.head()
        st = mine.sync_once()
        self.assertEqual((st['failed'], st['diverged'], st['pending_push']),
                         ('fast-forward', True, True))
        self.assertEqual((self.head(), self.remote_tip()), (local, moved))
        self.assertEqual(self.config_bytes(), before)


if __name__ == '__main__':
    unittest.main()
