"""kitlib.hubsite.gitsync: the store folder's debounced commit, then fetch, fast-forward and push
with explicit refspecs; what a refused or failed step leaves pending; the guard before each local
write; the refusals; the thread.

The logic runs on the fake git. One class, EndToEndTest, runs on real git: two repositories and a
bare remote in temp folders, `.git/config` byte-identical before and after, and the cases the fake
has no model of (merge.autoStash, a signing program, an identity git would make up, a path in the
wrong case)."""
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

FETCH = ['fetch', '-q', '--no-tags', 'origin', 'refs/heads/pmdata:refs/remotes/origin/pmdata']
PUSH = ['push', '-q', 'origin', 'refs/heads/pmdata:refs/heads/pmdata']
STATUS = ['--no-optional-locks', 'status', '--porcelain', '-z', '--untracked-files=all', '--', 'hub']
LOCK = '?? hub/' + LOCK_NAME
WRITES = ('merge', 'add', 'commit', 'reset', 'push')    # what a pass that stops early never runs
SESSION_ENV = ('GIT_SSH_COMMAND', 'GIT_SSH', 'GIT_ASKPASS', 'SSH_ASKPASS', 'GIT_TERMINAL_PROMPT',
               'GCM_INTERACTIVE', 'EMAIL')              # what the session running the tests may set
MARKERS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply',
           'index.lock')                                # what another git operation leaves behind


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
        self.env = {k: v for k, v in fixture_env().items()  # nor the session's ssh and prompt
                    if k.upper() not in SESSION_ENV}        # choices, nor its EMAIL
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
        if gitcmd.fake_active():
            # The fake git's merge has no --no-autostash (and no autostash to turn off). The flag
            # is recorded above and runs on real git in EndToEndTest.
            args = [a for a in args if a != '--no-autostash']
        return gitcmd.call(args, cwd, **kw)

    def sync(self, every=60, remote='origin', store=None, **kw):
        kw.setdefault('clock', lambda: self.now[0])
        kw.setdefault('git', self.runner)
        kw.setdefault('env', self.env)
        s = gitsync.GitSync(store or self.hub, every, remote=remote, **kw)
        self.addCleanup(s.stop)
        return s

    def git_path(self, name):
        """Where the data work tree's own git directory keeps `name` (MERGE_HEAD, index.lock)."""
        return self.git('rev-parse', '--path-format=absolute', '--git-path', name)

    def mark(self, name):
        """Leave `name` behind as another git operation would; its path."""
        path = self.git_path(name)
        if name.startswith('rebase-'):
            os.mkdir(path)
        else:
            with open(path, 'w', encoding='utf-8'):
                pass
        return path

    def unmark(self, path):
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)

    def busy_pass(self, sync, markers):
        """With each of `markers` left in the git directory in turn, a pass over a store with one
        new document ends at `busy` and writes nothing; with none left, the next pass commits.
        `sync` records its git commands through self.runner."""
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        tip = self.head()
        self.write('hub/pages/b.json')
        for name in markers:
            with self.subTest(marker=name):
                path = self.mark(name)
                del self.calls[:]
                try:
                    st = sync.sync_once()
                finally:
                    self.unmark(path)
                self.assertEqual((st['ok'], st['failed']), (False, 'busy'))
                self.assertIn('(%s)' % name, st['detail'])
                self.assertEqual((st['pending_commit'], st['pending_push']), (True, False))
                self.assertEqual(self.wrote(), [])
                self.assertEqual((self.head(), self.hub_status()), (tip, '?? hub/pages/b.json'))
        st = sync.sync_once()                               # the other operation is over
        self.assertEqual((st['ok'], st['failed'], st['pending_commit']), (True, '', False))
        self.assertEqual(self.committed(), ['hub/pages/b.json'])

    def read(self, rel, root=None):
        with open(os.path.join(root or self.wt, rel), encoding='utf-8', newline='') as f:
            return f.read()

    def wrote(self):
        """The subcommands of the recorded calls that write to the work tree, the index or the
        remote."""
        return [s for s in self.subs() if s in WRITES]

    def env_of(self, sub):
        """The environment the first recorded `sub` ran in."""
        return [e for s, e in self.envs if s == sub][0]

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
                         [['commit', '-q', '--no-gpg-sign', '-m', gitsync.SUBJECT, '--', 'hub']])
        self.assertEqual([a for a in self.calls if a[0] == 'add'], [['add', '--', 'hub/pages/a.json']])
        self.assertEqual([a for a in self.calls if 'status' in a], [STATUS])
        # config and diff are reads: the identity and the ssh command, and what is staged already
        self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse', 'config', 'diff',
                                            'fetch', 'add', 'commit', 'push'})
        self.assertEqual([a for a in self.calls if a[0] in ('config', 'diff')],
                         [['config', '--get', 'core.sshCommand'],
                          ['config', '--get-regexp', r'^(user|author|committer)\.(name|email)$'],
                          ['diff', '--cached', '--name-only', '-z', '--', 'hub']])
        for argv in self.calls:
            for arg in argv:
                self.assertFalse(arg.startswith('+') or arg in ('-f', '--force', '--no-verify', '-n')
                                 or arg.startswith(('--force', '--author', '-c')), argv)

    def test_a_fetch_and_a_push_can_wait_for_nobody(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        for sub in ('fetch', 'push'):
            with self.subTest(sub=sub):
                env = self.env_of(sub)
                self.assertEqual([env.get(k) for k in ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE',
                                                       'GIT_ASKPASS', 'SSH_ASKPASS',
                                                       'GIT_SSH_COMMAND')],
                                 ['0', 'never', '', '', 'ssh -o BatchMode=yes'])
        self.assertNotIn('GIT_SSH_COMMAND', self.env_of('commit'))  # the network commands only
        self.assertNotIn('GIT_ASKPASS', self.env)                   # and the caller's own is untouched

    def test_the_owners_own_ssh_command_is_never_replaced(self):
        for n, (extra, config, want) in enumerate((
                ({'GIT_SSH_COMMAND': 'ssh -i key'}, None, 'ssh -i key'),
                ({'Git_Ssh': 'plink'}, None, None),
                ({}, 'ssh -i key', None))):
            with self.subTest(extra=extra, config=config):
                del self.envs[:]
                if config:
                    self.git('config', 'core.sshCommand', config)
                sync = self.sync(env=dict(self.env, **extra))
                self.write('hub/pages/%d.json' % n)
                self.assertTrue(sync.sync_once()['ok'])
                for sub in ('fetch', 'push'):
                    env = self.env_of(sub)
                    self.assertEqual((env.get('GIT_SSH_COMMAND'), env['GIT_ASKPASS'],
                                      env['SSH_ASKPASS']), (want, '', ''))

    def test_an_askpass_named_in_another_case_does_not_survive(self):
        sync = self.sync(env=dict(self.env, Git_AskPass='a-program', ssh_askpass='another'))
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        env = self.env_of('push')
        self.assertEqual({k: v for k, v in env.items() if k.upper().endswith('_ASKPASS')},
                         {'GIT_ASKPASS': '', 'SSH_ASKPASS': ''})

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
        self.assertIn(['merge', '-q', '--ff-only', '--no-autostash', 'refs/remotes/origin/pmdata'],
                      self.calls)

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
        self.assertEqual([a for a in self.calls if a[0] == 'reset'],
                         [['reset', '-q', '--', 'hub/pages/a.json']])

    def test_a_failed_commit_unstages_only_what_this_pass_staged(self):
        sync = self.sync()
        self.write('hub/pages/theirs.json')
        self.git('add', '--', 'hub/pages/theirs.json')      # staged by somebody else, not committed
        self.write('hub/pages/mine.json')
        self.answers['commit'] = gitcmd.Result(1, b'', 'error: the hook said no\n')
        st = sync.sync_once()
        self.assertEqual((st['failed'], st['detail'], st['pending_commit']),
                         ('commit', 'error: the hook said no', True))
        self.assertEqual([a for a in self.calls if a[0] == 'reset'],
                         [['reset', '-q', '--', 'hub/pages/mine.json']])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'hub/pages/theirs.json')
        self.assertEqual(sorted(self.hub_status().splitlines()),
                         ['?? hub/pages/mine.json', 'A  hub/pages/theirs.json'])
        del self.answers['commit']
        self.assertTrue(sync.sync_once()['ok'])             # the store's commit takes both
        self.assertEqual(self.committed(), ['hub/pages/mine.json', 'hub/pages/theirs.json'])

    def test_a_failed_unstage_is_reported(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.answers['commit'] = gitcmd.Result(1, b'', 'error: the hook said no\n')
        self.answers['reset'] = gitcmd.Result(
            128, b'', "fatal: Unable to create 'index.lock': File exists.\n")
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['pending_commit']), (False, 'commit', True))
        self.assertTrue(st['detail'].startswith('error: the hook said no; and what this pass '
                                                'staged under the store could not be unstaged'),
                        st['detail'])
        self.assertIn("fatal: Unable to create 'index.lock': File exists.", st['detail'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'hub/pages/a.json')

    def test_another_git_operation_under_way_ends_the_pass_before_a_write(self):
        # The two the fake git can hold; EndToEndTest runs every one of MARKERS on real git.
        self.busy_pass(self.sync(), ('MERGE_HEAD', 'index.lock'))

    def test_another_git_operation_under_way_stops_the_fast_forward_too(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        tip = self.head()
        moved = self.move_remote(self.clone())
        self.write('hub/pages/b.json')
        self.mark('MERGE_HEAD')
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['diverged'], st['pending_push']),
                         (False, 'busy', False, False))
        self.assertIn('(MERGE_HEAD)', st['detail'])
        self.assertEqual(self.wrote(), [])
        self.assertEqual((self.head(), self.remote_tip()), (tip, moved))
        self.assertEqual(self.hub_status(), '?? hub/pages/b.json')

    def test_a_branch_switched_during_the_fetch_stops_the_fast_forward(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        tip = self.head()
        moved = self.move_remote(self.clone())
        self.write('hub/pages/b.json')
        self.before['fetch'] = lambda: self.git('checkout', '-q', '-b', 'side')
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed']), (False, 'branch'))
        self.assertIn("'side'", st['detail'])
        self.assertEqual(self.wrote(), [])
        self.assertIn('fetch', self.subs())                 # the pass had got that far
        self.assertEqual(self.git('branch', '--show-current'), 'side')
        self.assertEqual((self.head(), self.git('rev-parse', 'refs/heads/pmdata'), self.remote_tip()),
                         (tip, tip, moved))                 # neither branch moved
        self.assertEqual(self.hub_status(), '?? hub/pages/b.json')

    def test_a_branch_switched_during_the_fetch_stops_the_commit(self):
        sync = self.sync()
        tip = self.head()
        self.write('hub/pages/a.json')
        self.before['fetch'] = lambda: self.git('checkout', '-q', '-b', 'side')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['pending_commit'], st['pending_push']),
                         (False, 'branch', True, True))
        self.assertIn("'side'", st['detail'])
        self.assertEqual(self.wrote(), [])
        self.assertIn('fetch', self.subs())
        self.assertEqual((self.head(), self.hub_status(), self.remote_tip()),
                         (tip, '?? hub/pages/a.json', ''))

    def test_a_commit_stopped_at_its_time_limit_may_leave_the_index_lock_which_is_never_removed(self):
        sync = self.sync()
        tip = self.head()
        self.write('hub/pages/a.json')
        lock = self.git_path('index.lock')
        self.before['commit'] = lambda: self.mark('index.lock')     # what the stopped git left
        self.answers['commit'] = gitcmd.Result(
            None, b'', 'git commit -q --no-gpg-sign timed out after 30 seconds')
        self.answers['reset'] = gitcmd.Result(
            128, b'', "fatal: Unable to create 'index.lock': File exists.\n")
        st = sync.sync_once()
        self.assertEqual((st['failed'], st['pending_commit']), ('commit', True))
        self.assertTrue(st['detail'].startswith('git commit -q --no-gpg-sign timed out after 30 '
                                                'seconds; git was stopped at its time limit and '
                                                'may have left index.lock behind'), st['detail'])
        self.assertTrue(os.path.isfile(lock))
        self.before.clear()
        self.answers.clear()
        del self.calls[:]
        self.now[0] += 60
        st = sync.sync_once()                               # the lock is still there: hands off
        self.assertEqual((st['failed'], st['pending_commit']), ('busy', True))
        self.assertIn('(index.lock)', st['detail'])
        self.assertEqual(self.wrote(), [])
        self.assertTrue(os.path.isfile(lock))
        self.assertEqual(self.head(), tip)
        os.remove(lock)                                     # a person did; never the sync
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['pending_commit']), (True, False))
        self.assertEqual(self.committed(), ['hub/pages/a.json'])

    def test_a_fast_forward_stopped_at_its_time_limit_says_the_index_lock_may_be_left(self):
        sync = self.sync()
        self.write('hub/pages/a.json')
        self.assertTrue(sync.sync_once()['ok'])
        tip = self.head()
        self.move_remote(self.clone())
        self.write('hub/pages/b.json')
        self.answers['merge'] = gitcmd.Result(
            None, b'', 'git merge -q --ff-only timed out after 30 seconds')
        del self.calls[:]
        st = sync.sync_once()
        self.assertEqual((st['failed'], st['diverged']), ('fast-forward', False))
        self.assertEqual(st['detail'], 'git merge -q --ff-only timed out after 30 seconds; git was '
                         'stopped at its time limit and may have left index.lock behind in the git '
                         'directory (nothing here removes it)')
        self.assertEqual([s for s in self.wrote() if s != 'merge'], [])
        self.assertEqual(self.head(), tip)

    def test_without_a_git_identity_nothing_is_committed(self):
        sync = self.sync()
        tip = self.head()
        self.write('hub/pages/a.json')
        self.git('config', '--unset', 'user.email')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['pending_commit']), (False, 'commit', True))
        self.assertTrue(st['detail'].startswith('git identity not set: no user.email for'),
                        st['detail'])
        self.assertNotIn('user.name', st['detail'])
        self.assertEqual([s for s in self.subs() if s in ('add', 'commit', 'reset')], [])
        self.assertEqual((self.head(), self.hub_status()), (tip, '?? hub/pages/a.json'))
        # GIT_AUTHOR_* and GIT_COMMITTER_* are an identity too, and the one git reads first
        named = self.sync(env=dict(self.env, GIT_AUTHOR_EMAIL='author@example.invalid',
                                   GIT_COMMITTER_EMAIL='committer@example.invalid'))
        st = named.sync_once()
        self.assertEqual((st['ok'], st['detail'], st['pending_commit']), (True, '', False))
        self.assertEqual(self.git('log', '-1', '--format=%ae %ce'),
                         'author@example.invalid committer@example.invalid')
        half = self.sync(env=dict(self.env, GIT_AUTHOR_EMAIL='author@example.invalid'))
        self.write('hub/pages/b.json')
        st = half.sync_once()                               # the committer has none
        self.assertEqual(st['failed'], 'commit')
        self.assertTrue(st['detail'].startswith('git identity not set: no user.email for'))

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

    def test_with_push_off_it_commits_and_stops_there_and_says_nothing(self):
        for kw in ({'remote': 'origin', 'push': False}, {'remote': None, 'push': False}):
            with self.subTest(kw=kw):
                del self.calls[:]
                sync = self.sync(**kw)
                st = sync.status()
                self.assertEqual((st['ok'], st['failed'], st['detail'], st['pending_push']),
                                 (None, '', '', False))
                self.write('hub/pages/%s.json' % kw['remote'])
                st = sync.sync_once()
                self.assertEqual((st['ok'], st['remote'], st['pending_commit'], st['pending_push']),
                                 (True, '', False, False))
                self.assertEqual(st['last_commit'], self.head())
                self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse', 'config',
                                                    'diff', 'add', 'commit'})
        self.assertEqual(self.remote_tip(), '')

    def test_push_on_and_no_remote_is_reported_from_the_start(self):
        sync = self.sync(remote=None)
        st = sync.status()                                  # before any pass
        self.assertEqual((st['ok'], st['failed'], st['pending_push'], st['remote']),
                         (None, 'push', True, ''))
        self.assertTrue(st['detail'].startswith('no remote to push to'), st['detail'])
        self.write('hub/pages/a.json')
        st = sync.sync_once()
        self.assertEqual((st['ok'], st['failed'], st['pending_commit'], st['pending_push']),
                         (False, 'push', False, True))
        self.assertTrue(st['detail'].startswith('no remote to push to'), st['detail'])
        self.assertEqual((st['last_commit'], self.committed()), (self.head(), ['hub/pages/a.json']))
        self.assertEqual(set(self.subs()), {'branch', 'status', 'rev-parse', 'config', 'diff',
                                            'add', 'commit'})
        self.now[0] += 60
        st = sync.sync_once()                               # nothing to commit: still said
        self.assertEqual((st['ok'], st['failed'], st['pending_push'], st['failing_since']),
                         (False, 'push', True, 1000.0))
        self.answers['commit'] = gitcmd.Result(1, b'', 'error: the hook said no\n')
        self.write('hub/pages/b.json')
        st = sync.sync_once()                               # an earlier failure is the one named
        self.assertEqual((st['failed'], st['detail'], st['pending_push']),
                         ('commit', 'error: the hook said no', True))
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
                (self.hub, {'remote': 'nowhere'}, "has no remote named 'nowhere'"),
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
        self.assertIn('the data branch cannot be determined', str(cm.exception))

    def test_the_store_folder_is_named_as_it_is_on_disk(self):
        self.write('hub/pages/a.json')
        plan = gitsync.prepare(os.path.join(self.wt, 'HUB'), remote='origin', branch='pmdata')
        self.assertEqual(plan.rel, 'hub')                   # the folder's spelling, not the caller's
        self.write('data/Store/pages/a.json')
        self.assertEqual(gitsync.prepare(os.path.join(self.wt, 'DATA', 'store')).rel, 'data/Store')
        # a folder that is not there yet is made by the store under the caller's own spelling
        self.assertEqual(gitsync.prepare(os.path.join(self.wt, 'DATA', 'New', 'Hub')).rel,
                         'data/New/Hub')

    def test_a_store_git_tracks_under_another_spelling_is_refused(self):
        self.write('hub/pages/a.json')
        self.repo.commit('tracked as hub', 'hub/pages/a.json', cwd=self.wt)
        os.rename(self.hub, self.hub + '-moving')           # the folder renamed outside git
        os.rename(self.hub + '-moving', os.path.join(self.wt, 'Hub'))
        with self.assertRaises(gitsync.Refused) as cm:
            gitsync.prepare(self.hub, remote='origin')
        self.assertIn("git tracks the hub store folder as 'hub' and the folder on disk is 'Hub'",
                      str(cm.exception))

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

    def test_two_starts_at_once_start_one_thread(self):
        sync = self.sync(every=3600, clock=time.monotonic)
        real, started = threading.Thread, []
        entered, go = threading.Event(), threading.Event()

        class Slow(real):                                   # a sync thread that is slow to start
            def start(thread):
                if thread.name == 'hub-gitsync':
                    started.append(thread)
                    entered.set()
                    go.wait(30)
                real.start(thread)

        results = []
        callers = [real(target=lambda: results.append(sync.start())) for _ in range(2)]
        with mock.patch.object(threading, 'Thread', Slow):
            callers[0].start()
            self.assertTrue(entered.wait(30))               # the first caller is starting the thread
            callers[1].start()
            self.wait_for(lambda: len(started) == 2, seconds=0.5)   # a second thread, if one is made
            go.set()
            for caller in callers:
                caller.join(30)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(started), 1)
        self.assertEqual([t.name for t in threading.enumerate()].count('hub-gitsync'), 1)
        self.assertTrue(sync.stop())
        self.assertNotIn('hub-gitsync', [t.name for t in threading.enumerate()])

    def test_stop_waits_for_the_pass_in_flight_and_no_other_pass_starts(self):
        in_pass, release = threading.Event(), threading.Event()

        def hold():
            in_pass.set()
            release.wait(30)
        self.before['status'] = hold
        sync = self.sync(every=0.05, clock=time.monotonic)
        self.write('hub/pages/a.json')
        sync.start()
        self.assertTrue(in_pass.wait(30))
        stopped = []
        stopper = threading.Thread(target=lambda: stopped.append(sync.stop()))
        stopper.start()
        stopper.join(0.3)
        self.assertTrue(stopper.is_alive())                 # stop() waits for the pass
        self.assertEqual((stopped, self.subs().count('status')), ([], 1))
        release.set()
        stopper.join(30)
        self.assertEqual(stopped, [True])
        st = sync.status()                                  # the pass in flight ran to its end
        self.assertEqual((st['running'], st['ok'], st['pending_commit']), (False, True, False))
        self.assertEqual(self.committed(), ['hub/pages/a.json'])
        time.sleep(0.3)                                     # six periods
        self.assertEqual(self.subs().count('status'), 1)
        self.assertNotIn('hub-gitsync', [t.name for t in threading.enumerate()])


@real_git('end to end on the git binary: the wire between two repositories and a bare remote, '
          "the store's lock held while git reads the folder, .git/config as git leaves it, and "
          'what the fake has none of: merge.autoStash, commit signing, the identity git makes up '
          'and a path matched in the wrong case')
class EndToEndTest(Case):
    """Two repositories and one bare remote, all in temp folders; the default git runner."""

    def sync(self, store=None, **kw):
        kw.setdefault('env', self.env)
        s = gitsync.GitSync(store or self.hub, 60, remote='origin', branch='pmdata',
                            clock=lambda: self.now[0], **kw)
        self.addCleanup(s.stop)
        return s

    def test_the_fast_forward_never_stashes_an_uncommitted_change(self):
        self.write('notes.txt', 'one\n')
        self.repo.commit('notes', 'notes.txt', cwd=self.wt)
        mine = self.sync()
        self.write('hub/pages/home.json')
        self.assertTrue(mine.sync_once()['ok'])
        self.git('config', 'merge.autoStash', 'true')       # the owner's own setting
        before = self.config_bytes()
        other = self.clone()

        self.write('notes.txt', 'mine, not committed\n')    # outside the store, tracked, changed
        moved = self.move_remote(other)                     # touches another file: nothing in the way
        self.write('hub/pages/two.json')
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        self.assertEqual(self.git('log', '-1', '--format=%P'), moved)
        self.assertEqual(self.git('stash', 'list'), '')
        self.assertEqual(self.read('notes.txt'), 'mine, not committed\n')

        first = self.head()
        self.repo.git('pull', '-q', '--ff-only', 'origin', 'pmdata', cwd=other)
        moved = self.move_remote(other, 'notes.txt', 'theirs\n')    # the very file changed here
        self.write('hub/pages/three.json')
        st = mine.sync_once()                               # refused by git, never stashed
        self.assertEqual((st['ok'], st['failed'], st['diverged']), (False, 'fast-forward', False))
        self.assertEqual(self.git('stash', 'list'), '')
        self.assertEqual(self.read('notes.txt'), 'mine, not committed\n')   # no conflict markers
        self.assertEqual((self.head(), self.remote_tip()), (first, moved))
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all').splitlines(),
                         ['M notes.txt', '?? hub/pages/three.json'])
        self.assertEqual(self.config_bytes(), before)

    def test_every_other_git_operation_under_way_ends_the_pass_before_a_write(self):
        self.busy_pass(self.sync(git=self.runner), MARKERS)
        self.assertEqual(gitsync.MARKERS, MARKERS)

    def test_the_commit_is_never_signed(self):
        self.git('config', 'commit.gpgSign', 'true')        # and a signing program that cannot run
        self.git('config', 'gpg.program', 'kit-no-such-signing-program')
        mine = self.sync()
        self.write('hub/pages/home.json')
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        self.assertEqual(self.committed(), ['hub/pages/home.json'])
        self.assertEqual(self.git('log', '-1', '--format=%G?'), 'N')

    def test_without_a_git_identity_no_commit_is_made(self):
        mine = self.sync()
        first = self.head()
        self.write('hub/pages/home.json')
        self.git('config', '--unset', 'user.name')
        self.git('config', '--unset', 'user.email')
        st = mine.sync_once()                               # git would make one up: account@host
        self.assertEqual((st['ok'], st['failed'], st['pending_commit']), (False, 'commit', True))
        self.assertTrue(st['detail'].startswith('git identity not set: no user.email and no '
                                                'user.name for'), st['detail'])
        self.assertEqual((self.head(), self.git('diff', '--cached', '--name-only')), (first, ''))
        self.assertEqual(self.hub_status(), '?? hub/pages/home.json')

    def test_a_store_named_in_another_case_is_committed_all_the_same(self):
        self.write('hub/pages/home.json')
        mine = self.sync(store=os.path.join(self.wt, 'HUB'))
        self.assertEqual(mine.plan.rel, 'hub')
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['pending_commit'], st['pending_push']), (True, False, False))
        self.assertEqual(self.committed(), ['hub/pages/home.json'])
        self.write('hub/pages/two.json')
        self.write('hub/pages/home.json', '{"title": "changed"}\n')
        self.assertTrue(mine.sync_once()['ok'])
        self.assertEqual(self.committed(), ['hub/pages/home.json', 'hub/pages/two.json'])
        self.assertEqual((self.remote_tip(), self.hub_status()), (self.head(), ''))

    def test_a_file_staged_outside_the_store_stays_staged_and_out_of_the_commit(self):
        mine = self.sync()
        self.write('hub/pages/home.json')
        self.write('other.txt', 'staged by somebody else\n')
        self.git('add', '--', 'other.txt')
        st = mine.sync_once()
        self.assertEqual((st['ok'], st['detail']), (True, ''))
        self.assertEqual(self.committed(), ['hub/pages/home.json'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'other.txt')
        self.assertEqual(self.remote_tip(), self.head())
        self.assertNotIn('other.txt', self.repo.git('ls-tree', '-r', '--name-only',
                                                    'refs/heads/pmdata', cwd=self.repo.remote).split())
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), 'A  other.txt')

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
