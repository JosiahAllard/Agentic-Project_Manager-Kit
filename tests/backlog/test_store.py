"""The ref store end to end against temporary bare remotes: the compare-and-swap write path, its
races, validation before publish, the no-remote path and reads that never touch a checkout."""
import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from kitlib.fs import replace as fsreplace
from kitlib.backlog.errors import Failure, Usage
from kitlib.git import cmd

from tests.backlog.helpers import ADD_NONE, BRANCH, StoreCase, priced, race, rec
from tests.gitfixture import TempRepo, fixture_env
from tests.home import TempHomeCase
from tests.realgit import real_git


def clone(case, name):
    """A second clone of the bare remote, its own store on it."""
    path = os.path.join(case.repo.base, name)
    cmd.subprocess_run(['git', 'clone', '-q', case.repo.remote, path], check=True,
                       env=fixture_env(), capture_output=True)
    for k, v in (('user.name', 'Kit Test'), ('user.email', 'kit-test@example.invalid')):
        cmd.subprocess_run(['git', '-C', path, 'config', k, v], check=True, env=fixture_env())
    backend = case.make_backend(path)
    return smod.Store(backend, case.rules), path


def add(store, rid, scope=None):
    ctx = pmod.Context(store, store.backend.root, branch=BRANCH)
    return store.write(lambda s: pmod.plan_add(s, ctx, rid, 'summary of %s' % rid,
                                               [scope or 'area/%s/**' % rid.lower()], **ADD_NONE))


def added(rid):
    """The subject `add` writes for a record filed with ADD_NONE."""
    return 'backlog: add %s, found on no branch (or on a branch since landed)' % rid


def wrap_git(case, on):
    """Route every kit git call through `on(args, real)` for the test: it returns the Result to
    use, or None for the real one (`real()` runs the real call and returns its Result)."""
    real_call = cmd.call

    def fake(args, cwd, **kw):
        got = on(list(args), lambda: real_call(args, cwd, **kw))
        return got if got is not None else real_call(args, cwd, **kw)
    patch = mock.patch.object(cmd, 'call', fake)
    patch.start()
    case.addCleanup(patch.stop)


def remote_log(case):
    return case.repo.git('log', '--format=%s', smod.STORE_REF, '--', cwd=case.repo.remote).split('\n')


def store_history(case):
    """[(subject, [(status, path), ...])] of the remote's store, newest first."""
    out = case.repo.git('log', '--no-renames', '--format=%x01%s', '--name-status', smod.STORE_REF,
                        '--', cwd=case.repo.remote)
    history = []
    for block in out.split('\x01')[1:]:
        lines = [x for x in block.split('\n') if x.strip()]
        history.append((lines[0], [tuple(x.split('\t', 1)) for x in lines[1:]]))
    return history


def worktree_state(repo):
    """Everything a read must leave alone: HEAD, branches, the index bytes, status, the files.
    (Not the index mtime: `git status` here refreshes it.)"""
    index = os.path.join(repo.root, '.git', 'index')
    with open(index, 'rb') as f:
        index_hash = hashlib.sha1(f.read()).hexdigest()
    files = sorted(os.path.relpath(os.path.join(d, n), repo.root)
                   for d, dirs, names in os.walk(repo.root) if '.git' not in d.split(os.sep)
                   for n in names)
    return (repo.git('rev-parse', 'HEAD'), repo.git('symbolic-ref', 'HEAD'),
            repo.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads'),
            index_hash, repo.git('status', '--porcelain'), files)


class CompareAndSwap(StoreCase):

    def test_a_lost_race_retries_and_both_writes_survive(self):
        self.seed(priced('FOO'))
        other, _ = clone(self, 'other')
        attempts = []

        def interfere(attempt):
            attempts.append(attempt)
            if attempt == 0:
                add(other, 'BAR')          # lands between our read and our push
        self.store.before_publish = interfere
        before = self.remote_tip()
        self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertEqual(attempts, [0, 1], 'the first push must lose and the second win')
        snap = self.store.load()
        self.assertEqual(snap.tip, self.remote_tip())
        self.assertEqual(snap.get('FOO').get('effort'), 's')
        self.assertIsNotNone(snap.get('BAR'), 'the other writer\'s record was overwritten')
        log = self.repo.git('log', '--format=%s', smod.STORE_REF, '--').split('\n')
        self.assertEqual(log, ['backlog: set FOO', added('BAR'), 'seed'])
        self.assertEqual(self.repo.git('rev-list', '--count', '%s..%s' % (before, snap.tip)), '2')

    def test_two_writers_on_one_record_both_changes_survive(self):
        self.seed(priced('FOO'))
        other, _ = clone(self, 'other')
        octx = pmod.Context(other,other.backend.root, branch=BRANCH)

        def interfere(attempt):
            if attempt == 0:
                other.write(lambda s: pmod.plan_set(s, octx, 'FOO', [('summary', ['renamed by B'])]))
        self.store.before_publish = interfere
        self.verb('set', 'FOO', [('effort', ['l'])])
        foo = self.get('FOO')
        self.assertEqual((foo.get('effort'), foo.get('summary')), ('l', 'renamed by B'))

    def test_the_first_write_races_another_first_write(self):
        other, _ = clone(self, 'other')

        def interfere(attempt):
            if attempt == 0:
                add(other, 'BAR')          # creates the ref we expected to be absent
        self.store.before_publish = interfere
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        snap = self.store.load()
        self.assertEqual(sorted(r.id for r in snap.records), ['BAR', 'FOO'])

    def test_retries_are_bounded_and_a_loser_writes_nothing(self):
        self.seed(priced('FOO'))
        other, _ = clone(self, 'other')
        n = []

        def always(attempt):
            n.append(attempt)
            add(other, 'B%d' % attempt)
        self.store.before_publish = always
        with self.assertRaises(Failure) as cm:
            self.verb('set', 'FOO', [('effort', ['m'])])
        self.assertIn('nothing was written', str(cm.exception))
        self.assertEqual(n, list(range(smod.ATTEMPTS)))
        self.assertEqual(self.get('FOO').get('effort'), '-')
        self.assertEqual(sorted(r.id for r in self.store.load().records),
                         ['B0', 'B1', 'B2', 'B3', 'B4', 'FOO'])

    def test_a_replan_sees_the_new_state_and_can_refuse(self):
        """The plan runs again on the fresh snapshot: a pull that lost to another pull refuses."""
        self.seed(priced('FOO'))
        other, _ = clone(self, 'other')
        octx = pmod.Context(other,other.backend.root, branch='other-branch')
        self.repo.git('branch', 'other-branch')

        def interfere(attempt):
            if attempt == 0:
                other.write(lambda s: pmod.plan_pull(s, octx, 'FOO'))
        self.store.before_publish = interfere
        with self.assertRaises(Exception) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('already in flight', str(cm.exception))
        self.assertEqual(self.get('FOO').get('pulled_by'), 'other-branch')

    def writers(self, n):
        return [('W%d' % i, clone(self, 'c%d' % i)[0]) for i in range(n)]

    def assert_every_write_survives(self, names):
        """The store holds every record, in a linear history of one commit per write, and no
        commit deleted a record: a write built on a tree that lost its parent's records shows as
        a D line here even when the final set happens to come out whole."""
        snap = self.store.load()
        history = store_history(self)
        shown = '\n'.join('%s %s' % (subject, changes) for subject, changes in history)
        self.assertEqual(sorted(r.id for r in snap.records), names, 'the store history:\n' + shown)
        self.assertEqual([c for _, changes in history for c in changes if c[0] != 'A'], [],
                         'a write deleted or changed another record:\n' + shown)
        self.assertEqual(sorted(subject for subject, _ in history),
                         sorted(added(n) for n in names),
                         'a linear history, one commit per write:\n' + shown)

    def test_a_writer_that_dies_in_the_race_is_reported_not_lost(self):
        """A writer killed by something that is not an Exception (the fake git's Unsupported is a
        BaseException) is an error naming it, not a record the store seems to have lost."""
        from kitlib.git.fake.errors import Unsupported
        writers = self.writers(3)

        def kill(name, attempt):
            if name == 'W0':
                raise Unsupported('injected: W0 dies after its plan')
        errors, attempts = race(self, writers, add, hook=kill)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('W0: Unsupported('), errors)
        self.assertEqual(attempts['W0'], [0])
        self.assert_every_write_survives(['W1', 'W2'])

    def test_four_writers_planned_on_one_empty_store_publish_in_turn(self):
        """The race without threads or load: each writer's first plan runs the next writer's whole
        write before it publishes, so all four plan against the empty store, the innermost
        publishes first, and every outer one loses the lease once, re-plans on the tip it lost to
        and wins."""
        writers = self.writers(4)
        attempts = {n: [] for n, _ in writers}

        def chain(i):
            name, store = writers[i]

            def planned(attempt):
                attempts[name].append(attempt)
                if attempt == 0 and i + 1 < len(writers):
                    chain(i + 1)
            store.before_publish = planned
            add(store, name)
        chain(0)
        self.assertEqual(attempts, {'W0': [0, 1], 'W1': [0, 1], 'W2': [0, 1], 'W3': [0]})
        self.assertEqual([subject for subject, _ in store_history(self)],
                         [added('W%d' % i) for i in range(4)])
        self.assert_every_write_survives(['W0', 'W1', 'W2', 'W3'])

    def test_a_push_to_a_remote_that_cannot_be_read_fails_without_a_retry(self):
        self.seed(priced('FOO'))
        n = []

        def break_remote(attempt):
            n.append(attempt)
            shutil.move(self.repo.remote, self.repo.remote + '.gone')
        self.store.before_publish = break_remote
        with self.assertRaises(Failure):
            self.verb('set', 'FOO', [('effort', ['m'])])
        self.assertEqual(n, [0])

    def test_a_stale_local_copy_loses_then_refetches(self):
        """`--no-fetch` reads the local copy; a write from it loses the lease and re-reads."""
        self.seed(priced('FOO'))
        old = self.remote_tip()
        other, _ = clone(self, 'other')
        add(other, 'BAR')
        self.repo.git('update-ref', smod.STORE_REF, old)
        self.backend.fetch = False
        self.verb('set', 'FOO', [('effort', ['s'])])
        snap = self.store.load()
        self.assertIsNotNone(snap.get('BAR'))
        self.assertEqual(snap.get('FOO').get('effort'), 's')


@real_git('the remote rejects the push in a pre-receive hook')
class HookRejectedPush(StoreCase):
    """A push the remote refuses (moved out of PushFailures for the real-git tier)."""

    def test_a_rejected_push_fails_without_a_retry(self):
        hook = os.path.join(self.repo.remote, 'hooks', 'pre-receive')
        with open(hook, 'w', newline='\n') as f:
            f.write('#!/bin/sh\necho "refused by the test hook" >&2\nexit 1\n')
        attempts = []
        self.store.before_publish = attempts.append
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertIn('push to origin failed', str(cm.exception))
        self.assertEqual(attempts, [0])
        self.assertEqual(self.remote_tip(), '')


class PushFailures(StoreCase):
    """`publish` when the push exits non-zero: what the remote now holds decides."""

    def fail_first_push(self, then=None, really_push=True, out=b'', err='fatal: the remote end '
                        'hung up unexpectedly'):
        """The first push runs for real (unless `really_push` is False), then `then()`, and is
        reported as failed with `out` and `err`."""
        done = []

        def on(args, real):
            if args[0] != 'push' or done:
                return None
            done.append(1)
            r = real() if really_push else None
            if r is not None:
                self.assertEqual(r.rc, 0, r.err)
            if then is not None:
                then()
            return cmd.Result(1, out, err)
        wrap_git(self, on)
        attempts = []
        self.store.before_publish = attempts.append
        return attempts

    def test_a_push_reported_failed_that_landed_is_a_success(self):
        attempts = self.fail_first_push()
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(attempts, [0])
        self.assertEqual(remote_log(self), [added('FOO')])

    def test_a_push_reported_failed_that_landed_under_a_later_write_is_a_success(self):
        """Our commit landed and another writer pushed on top before we looked: ours is an
        ancestor of the remote's tip, so the write succeeded and is not re-planned (a re-plan
        would refuse `add FOO` as a duplicate of itself)."""
        other, _ = clone(self, 'other')
        attempts = self.fail_first_push(then=lambda: add(other, 'BAR'))
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(attempts, [0])
        self.assertEqual(remote_log(self), [added('BAR'), added('FOO')])
        self.assertEqual(self.repo.git('rev-parse', smod.STORE_REF), self.remote_tip(),
                         'the local cache follows the remote forward')

    def test_cannot_lock_ref_in_the_porcelain_status_is_a_conflict(self):
        """`push --porcelain` reports a ref's status on stdout, not stderr."""
        status = (b'To origin\n!\trefs/backlog/main:refs/backlog/main\t[remote rejected] (cannot '
                  b"lock ref 'refs/backlog/main': reference already exists)\nDone\n")
        attempts = self.fail_first_push(really_push=False, out=status,
                                        err='error: failed to push some refs to origin')
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(remote_log(self), [added('FOO')])

    def test_the_local_cache_never_moves_backwards(self):
        """A second writer in the same repository moves the shared cache past our new tip between
        our push and our cache update: the update is a fast-forward only, so it is skipped."""
        self.seed(priced('FOO'))
        second = smod.Store(self.make_backend(), self.rules)
        sctx = pmod.Context(second, self.repo.root, branch=BRANCH)
        done = []

        def on(args, real):
            if args[0] != 'push' or done:
                return None
            done.append(1)
            r = real()
            second.write(lambda s: pmod.plan_add(s, sctx, 'BAR', 'other', ['area/bar/**'],
                                                           **ADD_NONE))
            return r
        wrap_git(self, on)
        self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertEqual(remote_log(self), [added('BAR'), 'backlog: set FOO', 'seed'])
        self.assertEqual(self.repo.git('rev-parse', smod.STORE_REF), self.remote_tip())

    def test_a_failed_verification_fetch_says_the_write_may_have_landed(self):
        """The push landed, another writer pushed on top, the push was reported failed, and the
        fetch that would tell whether ours is under the new tip fails: the ERROR must not claim
        nothing was written, and the write is not re-planned."""
        other, _ = clone(self, 'other')
        state = []

        def on(args, real):
            if args[0] == 'push' and not state:
                state.append('pushing')
                r = real()
                self.assertEqual(r.rc, 0, r.err)
                add(other, 'BAR')
                state.append('pushed')
                return cmd.Result(1, b'', 'fatal: the remote end hung up unexpectedly')
            if args[0] == 'fetch' and len(state) == 2:
                return cmd.Result(128, b'', "fatal: unable to access 'origin': no route to host")
            return None
        wrap_git(self, on)
        attempts = []
        self.store.before_publish = attempts.append
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertIn('may have landed', str(cm.exception))
        self.assertIn('backlog.py check', str(cm.exception))
        self.assertEqual(attempts, [0])
        self.assertEqual(remote_log(self), [added('BAR'), added('FOO')])

    def test_a_remote_that_cannot_be_read_after_a_failed_push_says_the_write_may_have_landed(self):
        state = []

        def on(args, real):
            if args[0] == 'push' and not state:
                state.append(1)
                r = real()
                self.assertEqual(r.rc, 0, r.err)
                return cmd.Result(1, b'', 'fatal: the remote end hung up unexpectedly')
            if args[0] == 'ls-remote' and state:
                return cmd.Result(None, b'', 'git ls-remote origin refs/backlog/main timed out')
            return None
        wrap_git(self, on)
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertIn('may have landed', str(cm.exception))
        self.assertEqual(remote_log(self), [added('FOO')])

    def failed_push_then(self, step, result):
        """The push lands, another writer pushes on top, the push is reported failed, and the
        first `step` git call after the verification fetch returns `result`."""
        other, _ = clone(self, 'other')
        state = []

        def on(args, real):
            if args[0] == 'push' and not state:
                state.append('pushing')
                self.assertEqual(real().rc, 0)
                add(other, 'BAR')
                state.append('pushed')
                return cmd.Result(1, b'', 'fatal: the remote end hung up unexpectedly')
            if args[0] == 'fetch' and state == ['pushing', 'pushed']:
                state.append('fetched')
            elif step(args) and state == ['pushing', 'pushed', 'fetched']:
                state.append('faked')
                return result
            return None
        wrap_git(self, on)
        attempts = []
        self.store.before_publish = attempts.append
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(state[-1], 'faked')
        self.assertEqual(attempts, [0])
        self.assertEqual(remote_log(self), [added('BAR'), added('FOO')])
        return str(cm.exception)

    def test_an_unreadable_tip_after_the_verification_fetch_says_the_write_may_have_landed(self):
        # The store ref's read (RefBackend._local_tip, through refs.ref_commit).
        said = self.failed_push_then(lambda a: a[0] == 'for-each-ref' and a[-1] == smod.STORE_REF,
                                     cmd.Result(128, b'', 'fatal: bad object'))
        self.assertIn('the store cannot be fetched', said)
        self.assertIn('may have landed', said)

    def test_an_ancestry_git_cannot_tell_says_the_write_may_have_landed(self):
        # kitlib.git.refs.is_ancestor's one git: `rev-list -n 1 <descendant>..<ancestor>`.
        said = self.failed_push_then(lambda a: a[:3] == ['rev-list', '-n', '1'],
                                     cmd.Result(128, b'', 'fatal: not a valid commit'))
        self.assertIn('whether it landed cannot be told', said)
        self.assertIn('may have landed', said)


class RemoteDetection(StoreCase):
    """Only git's "no such remote" (rc 2) in a repository with no remote at all means no remote."""

    def test_a_misnamed_remote_is_refused_when_the_repository_has_others(self):
        with self.assertRaises(Failure) as cm:
            smod.RefBackend(self.repo.root, 'upstream', smod.STORE_REF, smod.STORE_REF)
        self.assertIn('origin', str(cm.exception))

    def test_a_remote_lookup_that_fails_is_a_failure_not_no_remote(self):
        no_such = cmd.Result(2, b'', "error: No such remote 'origin'")
        cases = {
            'timeout': {'get-url': cmd.Result(None, b'', 'git remote get-url timed out after 30 seconds')},
            'rc 128': {'get-url': cmd.Result(128, b'', 'fatal: bad config line 1')},
            'remote list fails': {'get-url': no_such, 'list': cmd.Result(None, b'', 'timed out')},
        }
        for name, answers in cases.items():
            with self.subTest(name):
                def on(args, real, answers=answers):
                    if args[:2] == ['remote', 'get-url']:
                        return answers['get-url']
                    if args == ['remote']:
                        return answers.get('list')
                    return None
                with mock.patch.object(cmd, 'call', lambda a, c, _r=cmd.call, _on=on, **kw:
                                       _on(list(a), lambda: _r(a, c, **kw)) or _r(a, c, **kw)):
                    with self.assertRaises(Failure):
                        smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)

    def test_a_local_store_the_remote_does_not_have_is_refused(self):
        """The remote has no store but the local ref holds one: writing would start a new store
        on the remote without those records."""
        self.seed(priced('FOO'))
        self.repo.git('update-ref', '-d', smod.STORE_REF, cwd=self.repo.remote)
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'BAR', 'a summary', ['area/bar/**'], **ADD_NONE)
        self.assertIn(smod.STORE_REF, str(cm.exception))
        self.assertEqual(self.remote_tip(), '')


class Sha256(StoreCase):
    object_format = 'sha256'

    def test_a_delete_writes_in_a_sha256_repository(self):
        self.seed(priced('FOO'), priced('BAR'))
        self.store.write(lambda s: smod.Change({'BAR': None}, 'backlog: drop BAR'))
        snap = self.store.load()
        self.assertEqual(([r.id for r in snap.records], len(snap.tip)), (['FOO'], 64))


class NoRemoteSha256(StoreCase):
    remote = False
    object_format = 'sha256'

    def test_a_first_write_creates_the_ref_with_the_sha256_null_id(self):
        """Without a remote the first write is `update-ref <ref> <new> <null id>`; the null id is
        as long as the repository's hashes (64 here), or git refuses it."""
        seen = []

        def on(args, real):
            if args[0] == 'update-ref':
                seen.append(args)
            return None
        wrap_git(self, on)
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual([a[-1] for a in seen], ['0' * 64])
        snap = self.store.load()
        self.assertEqual(([r.id for r in snap.records], len(snap.tip)), (['FOO'], 64))


class NothingInvalidIsPublished(StoreCase):

    def test_a_write_that_fails_validation_pushes_nothing(self):
        self.seed(priced('FOO'))
        before = self.remote_tip()
        bad = rec('FOO', priority='2', priority_by='agent', state='sideways')
        with self.assertRaises(Usage):
            self.store.write(lambda s: smod.Change(bad, 'backlog: set FOO'))
        self.assertEqual(self.remote_tip(), before)
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_plan_that_refuses_pushes_nothing(self):
        self.seed(priced('FOO'))
        before = self.remote_tip()
        with self.assertRaises(Usage):
            self.verb('set', 'FOO', [('state', ['sideways'])])
        with self.assertRaises(Usage):
            self.verb('block', 'FOO', ['item:NOPE'])
        self.assertEqual(self.remote_tip(), before)

    def test_a_write_that_breaks_another_record_is_refused(self):
        """Store-wide checks run on the planned store: deleting a blocker by hand is caught."""
        self.seed(priced('FOO'), rec('BAR', state='blocked', blocked_on='item:FOO'))
        before = self.remote_tip()
        with self.assertRaises(Usage):
            self.store.write(lambda s: smod.Change({'FOO': None}, 'backlog: drop FOO'))
        self.assertEqual(self.remote_tip(), before)

    def test_an_unchanged_write_publishes_nothing(self):
        self.seed(priced('FOO', body='## Why\n\nbecause'))
        before = self.remote_tip()
        change = self.verb('set', 'FOO', [('effort', ['-'])])
        self.assertEqual(change.writes, {})
        self.assertEqual(self.remote_tip(), before)

    def test_a_write_that_adds_a_check_failure_is_refused(self):
        """`check` FAILs that no single record's validation sees: a resting `blocked_on:
        unmapped`, a pre-emption with no `displaced:` line, an unpriced pull with no pre-emption."""
        pulled = dict(state='pulled', pulled_at='2026-10-01', pulled_by=BRANCH)
        bad = {
            'resting unmapped': rec('BAR', state='blocked', blocked_on='unmapped'),
            'preempt without displaced': priced('BAR', preempt='the owner said go',
                                                preempt_by='pm', preempt_at='2026-10-01', **pulled),
            'unpriced pull': rec('BAR', **pulled),
        }
        self.seed(priced('FOO'))
        before = self.remote_tip()
        for name, writes in bad.items():
            with self.subTest(name):
                with self.assertRaises(Usage):
                    self.store.write(lambda s, w=writes: smod.Change(w, 'backlog: add BAR'))
                self.assertEqual(self.remote_tip(), before)

    def test_a_check_failure_the_store_already_had_does_not_block_other_writes(self):
        self.seed(priced('FOO'), rec('BAR', state='pulled', pulled_at='2026-10-01',
                                     pulled_by=BRANCH))
        self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertEqual(self.get('FOO').get('effort'), 's')

    def test_an_unreadable_store_refuses_every_write(self):
        self.seed(priced('FOO'), {'BAD': 'no separator here\n'})
        before = self.remote_tip()
        with self.assertRaises(Exception):
            self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertEqual(self.remote_tip(), before)


class NoRemote(StoreCase):
    remote = False

    def test_a_write_moves_the_local_ref_with_update_ref(self):
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        tip = self.repo.git('rev-parse', smod.STORE_REF)
        self.assertEqual(self.store.load().tip, tip)
        self.assertEqual(self.backend.describe(), smod.STORE_REF)
        self.assertIn('backlog: add FOO', self.repo.git('log', '-1', '--format=%s', tip))

    def test_update_ref_with_a_stale_old_value_retries(self):
        self.seed(priced('FOO'))
        second = smod.Store(self.make_backend(), self.rules)
        sctx = pmod.Context(second, self.repo.root, branch=BRANCH)

        def interfere(attempt):
            if attempt == 0:
                second.write(lambda s: pmod.plan_add(s, sctx, 'BAR', 'other', ['area/bar/**'],
                                                           **ADD_NONE))
        self.store.before_publish = interfere
        self.verb('set', 'FOO', [('effort', ['s'])])
        snap = self.store.load()
        self.assertIsNotNone(snap.get('BAR'))
        self.assertEqual(snap.get('FOO').get('effort'), 's')

    def test_a_first_write_needs_the_ref_absent(self):
        second = smod.Store(self.make_backend(), self.rules)
        sctx = pmod.Context(second, self.repo.root, branch=BRANCH)

        def interfere(attempt):
            if attempt == 0:
                second.write(lambda s: pmod.plan_add(s, sctx, 'BAR', 'other', ['area/bar/**'],
                                                           **ADD_NONE))
        self.store.before_publish = interfere
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(sorted(r.id for r in self.store.load().records), ['BAR', 'FOO'])

    def test_an_update_ref_that_timed_out_after_the_ref_moved_is_a_success(self):
        """`update-ref` moved the ref and was then killed (rc None): the write landed, so it is not
        re-planned (a re-plan would refuse `add FOO` as a duplicate of itself)."""
        done = []

        def on(args, real):
            if args[0] != 'update-ref' or done:
                return None
            done.append(1)
            r = real()
            self.assertEqual(r.rc, 0, r.err)
            return cmd.Result(None, b'', 'git update-ref -m backlog: timed out after 30 seconds')
        wrap_git(self, on)
        attempts = []
        self.store.before_publish = attempts.append
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(attempts, [0])
        self.assertEqual([r.id for r in self.store.load().records], ['FOO'])

    def test_an_update_ref_that_timed_out_before_the_ref_moved_is_not_a_success(self):
        """rc None with the ref still at the snapshot tip: nothing landed, so it is an ERROR, never
        reported as written."""
        tip = self.seed(priced('FOO'))

        def on(args, real):
            if args[0] == 'update-ref':
                return cmd.Result(None, b'', 'git update-ref: timed out after 30 seconds')
            return None
        wrap_git(self, on)
        with self.assertRaises(Failure) as cm:
            self.verb('add', 'BAR', 'a summary', ['area/bar/**'], **ADD_NONE)
        self.assertIn('timed out', str(cm.exception))
        self.assertEqual(self.repo.git('rev-parse', smod.STORE_REF), tip)
        self.assertEqual([r.id for r in self.store.load().records], ['FOO'])


    def test_a_held_ref_lock_is_a_conflict_and_retries(self):
        """Another writer holding the ref's lock file makes `update-ref` fail with "cannot lock
        ref": the store is busy, not broken."""
        if self.repo.git('rev-parse', '--show-ref-format', check=False) not in ('', 'files'):
            self.skipTest('the lock file is a files-backend detail')
        self.seed(priced('FOO'))
        lock = os.path.join(self.repo.common_dir(), *(smod.STORE_REF + '.lock').split('/'))
        attempts = []

        def hold(attempt):
            attempts.append(attempt)
            if attempt == 0:
                open(lock, 'w').close()
            elif os.path.exists(lock):
                os.remove(lock)
        self.store.before_publish = hold
        self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(self.get('FOO').get('effort'), 's')


def kill_store_reads(when=lambda: True):
    """A patch of cmd._run under which every read of the store ref (`rev-parse` or `for-each-ref`
    naming it) exits 1 having printed nothing, a git ended from outside (refs.KILLED_SHAPE), while
    `when()` holds; every other git runs as before."""
    real = cmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        if (argv[1] in ('rev-parse', 'for-each-ref') and any(smod.STORE_REF in a for a in argv)
                and when()):
            return 1, b'', b''
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(cmd, '_run', side_effect=run)


class AKilledStoreReadIsNoAnswer(StoreCase):
    """`rev-parse --verify --quiet` says "no such ref" as exit 1 with no
    output, which a git ended from outside also leaves; the store must not read a kill as an
    empty store."""
    remote = False

    def test_a_killed_store_read_is_a_failure_not_an_empty_store(self):
        self.seed(priced('FOO'))
        with kill_store_reads(), self.assertRaises(Failure) as cm:
            self.store.load()
        self.assertIn('cannot read %s' % smod.STORE_REF, str(cm.exception))

    def test_at_landing_reads_a_killed_store_read_as_no_answer(self):
        from kitlib.backlog import at_landing
        self.seed(priced('FOO'))
        with kill_store_reads():
            got, why = at_landing._local_tip(smod.STORE_REF, self.repo.root)
        self.assertIsNone(got, 'a killed git was read as "no store ref"')
        self.assertTrue(why)

    def test_landable_reads_a_killed_branch_read_as_no_answer_not_no_branch(self):
        """`rev-parse --verify --quiet refs/heads/<b>` said "no such
        branch" as exit 1 with no output, the shape a kill leaves."""
        from kitlib.backlog import landable as L
        snap = self.store.load()
        self.repo.git('branch', 'tools/held', 'main')
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'rev-parse' and argv[-1].startswith('refs/heads/tools/held'):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(cmd, '_run', side_effect=run):
            got = L.check(self.backend, snap, self.repo.root, ['tools/held'])
        self.assertEqual([v.verdict for v in got], [L.CANNOT_TELL])
        self.assertNotIn('no local branch', got[0].detail)
        self.assertIn('cannot read refs/heads/tools/held', got[0].detail)

    def test_a_failed_update_ref_whose_reread_is_killed_may_have_landed(self):
        tip = self.seed(priced('FOO'))
        moved = []

        def on(args, real):
            if args[0] == 'update-ref':
                moved.append(1)
                return cmd.Result(1, b'', 'fatal: boom')
            return None
        wrap_git(self, on)
        with kill_store_reads(lambda: bool(moved)), self.assertRaises(Failure) as cm:
            self.verb('set', 'FOO', [('effort', ['s'])])
        self.assertIn('the write may have landed', str(cm.exception))
        self.assertEqual(moved, [1])
        self.assertEqual(self.repo.git('rev-parse', smod.STORE_REF), tip)


class TheRefAndTheCheckout(StoreCase):

    def test_the_librarys_git_runs_without_global_config_or_auto_gc(self):
        from kitlib.git import cmd
        for key, want in (('gc.auto', '0'), ('maintenance.auto', 'false')):
            self.assertEqual(cmd.run_text(['config', '--get', key], self.repo.root), (want, None))
        self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], os.devnull)

    def test_reads_never_touch_the_working_tree(self):
        self.seed(priced('FOO'), rec('BAR'), priced('BAZ', state='pulled', pulled_at='2026-10-01',
                                                   pulled_by=BRANCH))
        before = worktree_state(self.repo)
        snap = self.store.load()
        from kitlib.backlog import check, landable, offer
        holding, dead, _ = offer.in_flight_split(snap, self.repo.root, 'origin')
        offer.next_offer(snap, holding)
        check.check_store(snap)
        landable.check(self.backend, snap, self.repo.root, [BRANCH, 'main'])
        self.assertEqual(worktree_state(self.repo), before)

    def test_check_takes_a_ruling_id_on_its_word(self):
        """No check reads the ledger: a cited id no ledger holds is not a finding."""
        self.seed(priced('FOO', priority_by='owner', priority_ruling='R-9999'),
                  rec('BAR', hold_ruling='R-9997'))
        from kitlib.backlog import check
        results = check.check_store(self.store.load())
        self.assertEqual([r.name for r in results if r.red], [])
        self.assertFalse({'priority rulings', 'hold rulings'} & {r.name for r in results})

    def test_writes_never_touch_the_working_tree(self):
        before = worktree_state(self.repo)
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.verb('set', 'FOO', [('priority', ['2']), ('priority_by', ['agent'])])
        self.verb('pull', 'FOO')
        self.verb('close', 'FOO', 'done and landed, closes it')
        self.assertEqual(worktree_state(self.repo), before)

    def test_the_store_shares_no_history_with_main(self):
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        r = cmd.subprocess_run(['git', 'merge-base', 'main', smod.STORE_REF], cwd=self.repo.root,
                               capture_output=True, env=fixture_env())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.repo.git('ls-tree', '--name-only', '-r', smod.STORE_REF),
                         'backlog/FOO.md')

    def test_the_tool_adds_the_fetch_refspec_once(self):
        self.seed(rec('FOO'))
        self.store.load()
        self.store.load()
        specs = self.repo.git('config', '--get-all', 'remote.origin.fetch').split('\n')
        self.assertEqual(specs.count('+%s:%s' % (smod.STORE_REF, smod.STORE_REF)), 1)
        other, path = clone(self, 'other')
        add(other, 'BAR')
        self.repo.git('fetch', '-q', 'origin')
        self.assertIn('backlog: add BAR', self.repo.git('log', '-1', '--format=%s', smod.STORE_REF))

    def test_the_commit_names_verb_id_and_session(self):
        self.store.session = 'kit'
        # FOO is outside the offer (BAR ranks above it), so the pull takes --reason
        self.seed(priced('FOO', priority='5'), priced('BAR', priority='1'))
        self.verb('pull', 'FOO', reason='the owner said go')
        msg = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertIn('backlog: pull FOO onto %s' % BRANCH, msg)
        self.assertIn('Session: kit', msg)
        self.assertNotIn('Ruling:', msg)

    def test_a_read_of_a_remote_with_no_store_adds_no_refspec(self):
        """A refspec naming a ref the remote lacks breaks every plain `git fetch` there."""
        self.store.load()
        fetch = self.repo.git('config', '--get-all', 'remote.origin.fetch', check=False)
        self.assertNotIn('backlog', fetch)
        self.repo.git('fetch', '-q', 'origin')

    def test_an_empty_remote_reads_as_an_empty_store(self):
        snap = self.store.load()
        self.assertEqual((snap.records, snap.tip), ([], None))


class BranchStoreBase(StoreCase):
    """The `branch` store as `.kit.json` names it: `landed()` names the rev and folder that hold
    what the store published, with no fetch (the hub and the lint read it there)."""

    def make_backend(self, root=None):
        from kitlib import config
        root = root or self.repo.root
        self.repo.write('.kit.json', '{"backlog": {"store": "branch"}}', root=root)
        return smod.open_backend(root, config.load(root))

    def landed_ids(self):
        rev, folder = self.backend.landed('main')
        records, _, why = smod.records_in_tree(self.repo.root, rev, folder)
        self.assertIsNone(why)
        return rev, folder, sorted(r.id for r in records)


class BranchStore(BranchStoreBase):

    def test_landed_is_the_remote_tracking_branch_after_a_write(self):
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(self.landed_ids(), ('refs/remotes/origin/backlog', 'backlog', ['FOO']))
        out = self.repo.git('ls-remote', 'origin', 'refs/heads/backlog')
        self.assertEqual(out.split('\t')[0], self.repo.git('rev-parse', 'refs/remotes/origin/backlog'))
        self.assertEqual(self.repo.git('rev-parse', '--verify', '--quiet', 'refs/heads/backlog',
                                       check=False), '', 'with a remote no local branch is made')


class BranchStoreNoRemote(BranchStoreBase):
    remote = False

    def test_landed_is_the_local_branch_without_a_remote(self):
        self.verb('add', 'FOO', 'a summary', ['area/foo/**'], **ADD_NONE)
        self.assertEqual(self.landed_ids(), ('refs/heads/backlog', 'backlog', ['FOO']))


class OtherBackends(TempHomeCase):
    """Under a temp home: a verb's machine-path refusal reads the machine config."""

    def test_open_backend_follows_the_store_key(self):
        from kitlib import config
        repo = TempRepo(bare_remote=True)
        self.addCleanup(repo.cleanup)

        def opened(store):
            if store is not None:
                repo.write('.kit.json', '{"backlog": {"store": "%s"}}' % store)
            return smod.open_backend(repo.root, config.load(repo.root))
        ref = opened(None)
        self.assertEqual((ref.remote_ref, ref.local_ref), (smod.STORE_REF, smod.STORE_REF))
        br = opened('branch')
        self.assertEqual((br.remote_ref, br.local_ref),
                         ('refs/heads/backlog', 'refs/remotes/origin/backlog'))
        self.assertEqual(opened('main').rel, 'docs/backlog')
        self.assertEqual(opened('ref').local_ref, smod.STORE_REF)
        self.assertEqual(opened('elsewhere/records').rel, 'elsewhere/records')

    def main_store(self):
        """A main-backend store in a linked worktree of a fresh repository: (repo, root, store, ctx)."""
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        root = repo.worktree('w')
        store = smod.Store(smod.MainBackend(root, 'docs/backlog'), rmod.Rules())
        ctx = pmod.Context(store,root, branch=BRANCH)
        return repo, root, store, ctx

    def test_the_main_backend_writes_files_and_commits_nothing(self):
        repo, root, store, ctx = self.main_store()
        head = repo.git('rev-parse', 'HEAD', cwd=root)
        store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary', ['area/foo/**'],
                                                     **ADD_NONE))
        self.assertTrue(os.path.isfile(os.path.join(root, 'docs', 'backlog', 'FOO.md')))
        self.assertEqual(repo.git('rev-parse', 'HEAD', cwd=root), head)
        foo = store.load().get('FOO')
        self.assertEqual(foo.get('summary'), 'a summary')
        self.assertIn('Found on no branch', foo.body)

    def test_the_main_backend_refuses_to_write_in_a_main_checkout(self):
        """It has no compare-and-swap, so it writes only where one agent works: a linked
        worktree. A main checkout is the landers'."""
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        store = smod.Store(smod.MainBackend(repo.root, 'docs/backlog'), rmod.Rules())
        ctx = pmod.Context(store,repo.root, branch=BRANCH)
        with self.assertRaises(Failure) as cm:
            store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary', ['area/foo/**'],
                                                     **ADD_NONE))
        self.assertIn('main checkout', str(cm.exception))
        self.assertFalse(os.path.exists(repo.path('docs', 'backlog')))

    def test_the_main_backend_re_plans_when_a_record_changed_under_it(self):
        repo, root, store, ctx = self.main_store()
        store.write(lambda s: pmod.plan_add(s, ctx, 'FOO', 'a summary', ['area/foo/**'],
                                                     **ADD_NONE))
        path = os.path.join(root, 'docs', 'backlog', 'FOO.md')
        attempts = []

        def edit(attempt):
            attempts.append(attempt)
            if attempt == 0:
                with open(path, 'a', encoding='utf-8', newline='\n') as f:
                    f.write('\nedited by hand\n')
        store.before_publish = edit
        store.write(lambda s: pmod.plan_set(s, ctx, 'FOO', [('summary', ['renamed'])]))
        self.assertEqual(attempts, [0, 1])
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('summary: renamed', text)
        self.assertIn('edited by hand', text)

    def test_landable_on_a_main_store_reads_each_branchs_own_tip(self):
        from kitlib.backlog import landable as L
        from tests.gitfixture import TempRepo
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        for branch, over in (('tools/held', dict(state='pulled', pulled_at='2026-10-01',
                                                 pulled_by='tools/held')),
                             ('tools/free', {})):
            repo.git('checkout', '-q', '-b', branch, 'main')
            repo.write('docs/backlog/FOO.md', priced('FOO', **over)['FOO'])
            repo.write('docs/backlog/deeper/BAR.md', priced('BAR')['BAR'])
            repo.commit(branch, 'docs/backlog')
        got = L.check(smod.MainBackend(repo.root, 'docs/backlog'), None, repo.root,
                      ['tools/held', 'tools/free', 'main', 'a..b'])
        self.assertEqual([(v.verdict, v.held, v.detail) for v in got],
                         [(L.NOT_LANDABLE, ('FOO',), '1 records read'),
                          (L.LANDABLE, (), '1 records read'),
                          (L.CANNOT_TELL, (), 'no record under docs/backlog/ at its tip'),
                          (L.CANNOT_TELL, (), '`a..b` is not a branch name')])


def refused(real, times):
    """A stand-in for os.replace or os.remove that raises PermissionError (a sharing refusal)
    `times` times, then calls `real`; `calls` counts every call."""
    calls = []

    def fake(*args):
        calls.append(args)
        if len(calls) <= times:
            raise PermissionError(13, 'sharing violation')
        return real(*args)
    return fake, calls


class FakeTime:
    """Stands in for kitlib.fs.replace's `time`: monotonic() reads `t` and sleep(s) adds s to it,
    so the retry loop runs on counted pauses, never on the machine's speed."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class ReplaceRetry(unittest.TestCase):
    """_replace_file, _IndexLock.install and _IndexLock.release keep their retry on the shared
    kitlib.fs.replace loop: a sharing refusal (PermissionError) is retried every PAUSE for
    REPLACE_WAIT seconds, any other OSError is not, and what is given at the end is unchanged.
    The loop runs on a fake clock: REPLACE_WAIT 0.25 and a 0.0625 pause (exact in binary) give
    tries at 0, 1/16, 1/8, 3/16 and 1/4 s, so a refusal that never ends is tried exactly 5 times."""

    def setUp(self):
        self.clock = FakeTime()
        for patcher in (mock.patch.object(fsreplace, 'time', self.clock),
                        mock.patch.object(fsreplace, 'PAUSE', 0.0625),
                        mock.patch.object(smod, 'REPLACE_WAIT', 0.25)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dir = tempfile.mkdtemp(prefix='kit-backlog-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.full = os.path.join(self.dir, 'FOO.md')
        with open(self.full, 'wb') as f:
            f.write(b'old\n')
        self.index = os.path.join(self.dir, 'index')
        with open(self.index, 'wb') as f:
            f.write(b'old index')

    def content(self, path):
        with open(path, 'rb') as f:
            return f.read()

    def held_lock(self):
        lock = smod._IndexLock(self.index)
        self.assertTrue(lock.take(0))
        with open(lock.path, 'wb') as f:
            f.write(b'new index')
        return lock

    def test_replace_file_retries_a_refused_move_until_it_goes_through(self):
        fake, calls = refused(os.replace, 3)
        with mock.patch('os.replace', side_effect=fake):
            smod._replace_file(self.full, b'new\n')
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.clock.sleeps, [0.0625] * 3)
        self.assertEqual(self.content(self.full), b'new\n')
        self.assertEqual(sorted(os.listdir(self.dir)), ['FOO.md', 'index'])

    def test_replace_file_raises_the_refusal_after_REPLACE_WAIT_and_leaves_the_record(self):
        with mock.patch('os.replace', side_effect=PermissionError(13, 'sharing')) as m:
            with self.assertRaises(PermissionError) as got:
                smod._replace_file(self.full, b'new\n')
        self.assertEqual(m.call_count, 5)
        self.assertEqual(self.clock.sleeps, [0.0625] * 4)
        self.assertEqual(got.exception.errno, 13)
        self.assertEqual(self.content(self.full), b'old\n')
        self.assertEqual(sorted(os.listdir(self.dir)), ['FOO.md', 'index'])

    def test_replace_file_raises_another_error_at_once(self):
        with mock.patch('os.replace', side_effect=OSError(5, 'io')) as m:
            with self.assertRaises(OSError) as got:
                smod._replace_file(self.full, b'new\n')
        self.assertEqual(m.call_count, 1)
        self.assertNotIsInstance(got.exception, PermissionError)
        self.assertEqual(self.content(self.full), b'old\n')

    def test_install_retries_a_refused_move_until_it_goes_through(self):
        lock = self.held_lock()
        fake, calls = refused(os.replace, 3)
        with mock.patch('os.replace', side_effect=fake):
            self.assertEqual(lock.install(), '')
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.clock.sleeps, [0.0625] * 3)
        self.assertFalse(lock.held)
        self.assertEqual(self.content(self.index), b'new index')
        self.assertFalse(os.path.exists(lock.path))

    def test_install_names_the_refusal_after_REPLACE_WAIT_and_keeps_the_lock(self):
        lock = self.held_lock()
        with mock.patch('os.replace', side_effect=PermissionError(13, 'sharing')) as m:
            why = lock.install()
        self.assertEqual(m.call_count, 5)
        self.assertEqual(self.clock.sleeps, [0.0625] * 4)
        self.assertTrue(why.startswith('cannot move %s over the index: ' % lock.path), why)
        self.assertIn('sharing', why)
        self.assertTrue(lock.held)
        self.assertEqual(self.content(self.index), b'old index')

    def test_install_names_another_error_at_once(self):
        lock = self.held_lock()
        with mock.patch('os.replace', side_effect=OSError(5, 'io')) as m:
            why = lock.install()
        self.assertEqual(m.call_count, 1)
        self.assertTrue(why.startswith('cannot move %s over the index: ' % lock.path), why)
        self.assertTrue(lock.held)

    def test_release_retries_a_refused_removal_until_it_goes_through(self):
        lock = self.held_lock()
        fake, calls = refused(os.remove, 3)
        with mock.patch('os.remove', side_effect=fake):
            self.assertEqual(lock.release(), '')
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.clock.sleeps, [0.0625] * 3)
        self.assertFalse(lock.held)
        self.assertFalse(os.path.exists(lock.path))

    def test_release_names_the_refusal_after_REPLACE_WAIT_and_keeps_the_lock(self):
        lock = self.held_lock()
        with mock.patch('os.remove', side_effect=PermissionError(13, 'sharing')) as m:
            why = lock.release()
        self.assertEqual(m.call_count, 5)
        self.assertEqual(self.clock.sleeps, [0.0625] * 4)
        self.assertTrue(why.startswith("git's index lock %s could not be removed (" % lock.path),
                        why)
        self.assertTrue(lock.held)

    def test_release_names_another_error_at_once_and_a_gone_lock_is_success(self):
        lock = self.held_lock()
        with mock.patch('os.remove', side_effect=OSError(5, 'io')) as m:
            why = lock.release()
        self.assertEqual(m.call_count, 1)
        self.assertIn('could not be removed', why)
        self.assertTrue(lock.held)
        os.remove(lock.path)
        self.assertEqual(lock.release(), '')
        self.assertFalse(lock.held)


if __name__ == '__main__':
    unittest.main()
