"""kitlib.hubsite.gitsync: the hub store's own git sync (docs/pm-data-branch.md "Layout" and
"Never configure a refspec").

The hub store (kitlib.hubsite.store) is a folder of JSON documents inside the PM data worktree.
GitSync commits that folder to the data branch at most once every `commit_every_s` seconds and
sends the branch to the private remote. It runs in the hub server's process, on one thread of its
own; nothing here prints, parses arguments, exits or reads a settings file (the caller passes
plain values).

One pass (sync_once), in this order:

  1. branch   `branch --show-current` must still answer the branch prepare() saw. Anything else
              ends the pass before a write.
  2. status   `status --porcelain -z --untracked-files=all -- <store>`: what differs from HEAD
              under the store folder. The store's lock file and its temp files are never counted
              and never added.
  3. read     the branch's commit and the remote-tracking ref's commit (`rev-parse --verify`).
     Nothing to commit and nothing to push ends the pass here: an idle sync runs no fetch.
  4. fetch    `fetch -q <remote> refs/heads/<b>:refs/remotes/<remote>/<b>`, never with `+`.
              A remote that has no such branch yet is not a failure: the push creates it.
  5. fast-forward
              `merge -q --ff-only refs/remotes/<remote>/<b>` when the two commits differ.
  6. commit   `add -- <untracked store files>`, then `commit -q -m SUBJECT -- <store>`.
  7. push     `push -q <remote> refs/heads/<b>:refs/heads/<b>`, never forced.

The fetch and the fast-forward run BEFORE the commit. That order is what makes a remote that
moved cost nothing: the branch is fast-forwarded with the store's changes still uncommitted in the
work tree, the commit lands on top of the remote's commit, and the push is a fast-forward. Commit
first and the same case is two diverged histories.

What a failure leaves, step by step:

  - fetch fails (no network, a remote that is down): the commit is still made, nothing is pushed,
    the pass fails at `fetch` with `pending_push` set.
  - the fast-forward fails: NOTHING is committed and nothing is pushed in that pass. The cause may
    pass by itself (another tool held the index for a moment), and a commit made then, on the old
    tip, would turn a remote that only moved into a real divergence. `diverged` is True when git
    says the remote's commit does not contain the branch's (refs.is_ancestor, a definite NO only):
    the two histories have to be joined by a person. gitsync never merges, rebases or forces.
  - add or commit fails or is refused (a hook, no git identity, the index held): the store's
    paths are unstaged again (`reset -q -- <store>`, the index only, as kitlib.ledger.vcs does) so
    no other commit in the data worktree finds them staged, `pending_commit` stays set, and the
    next pass tries again. Commits made earlier are still pushed.
  - push fails or is refused: `pending_push` stays set and the next pass tries again.

Every pass ends in the status object (GitSync.status), which the server's /health reads.

The commit is a plain data commit: the machine's own git identity (no --author, no -c), the fixed
subject SUBJECT, no body, no agent trailer, limited by pathspec to the store folder, hooks left
on. It does not go through kitlib.commit: that is the path for an agent's change to tracked code,
with a model trailer and a staged machine-path scan, and neither fits data the owner wrote in a
browser. Other paths staged in the data worktree stay staged and out of the commit.

Machine paths. A hub document is the owner's own text and may hold a path of this machine. Nothing
here scans or changes it. The machine-path check (kitlib.machinepaths) keeps running for
everything else in the data worktree; the data worktree's own `.kit.json` leaves the store out
with an allow-list entry, for a store folder `hub`:

    "machine_paths": {"allow": [{"glob": "hub/*", "line": ".",
                                 "why": "hub documents are the owner's own text"}]}

(machinepaths matches the glob with fnmatch, whose `*` crosses `/`, so `hub/*` covers every
collection.) That entry is an allow-list, never a scrub of the owner's text. gitsync reads and
writes no `.kit.json`.

No git config is written, ever: no `config`, no `remote`, no upstream. Every fetch and push names
its refspec on the command line, so `.git/config` is byte-identical after any number of passes
(tests/hubsite/test_gitsync.py asserts it on real git).

prepare() refuses, before anything runs: a store folder in no git work tree, a detached HEAD, a
branch other than the one the caller named, a store folder that is the work tree's root (the
pathspec would then cover the whole data worktree) or whose path git would read as a pattern, a
remote that is not a plain name, and a work tree of the kit itself, through
kitlib.hubmirror's own check (_kit_checkout_error): a checkout holding the kit's code, a work tree
of the kit's repository that is not on its recorded data branch, or a remote other than the
recorded one. The hub holds the owner's private data and the kit's history is public.

Limits, stated so nobody has to find them:

  - An idle sync does not fetch. What another machine pushed arrives with the next pass that has
    something to commit or push.
  - The Store never reloads from disk. A fast-forward that brings store documents written on
    another machine changes files under a running Store, which will not see them until it is
    opened again. Two machines writing one store is outside what this module makes safe.
  - Two machines that both commit before either pushes have diverged; that is reported, not
    repaired. The same holds for a remote that moves between one pass's fetch and its push when
    that pass committed: the push is refused (pass failed at `push`) and the next pass reports
    `diverged`.
  - A fast-forward that would overwrite a store file this store has changed and not committed
    (the other machine's commit and this store both rewrote `_meta.json`, say) is refused by git
    and stays refused: every pass fails at `fast-forward` with `pending_commit` set and `diverged`
    False, until a person settles it. Commits from elsewhere that touch no uncommitted store file
    (backlog records, most of the data branch) fast-forward under the store's changes.
  - A pass can take as long as git takes: stop() waits for the pass in flight (NET_TIMEOUT at
    most for a fetch or a push, kitlib.git.cmd.GIT_TIMEOUT for anything else).
  - The push runs with git's credential prompts off (kitlib.git.cmd.NETWORK_ENV's two prompt
    switches): a thread nobody watches must fail, not wait for a password.
"""
import collections
import os
import re
import threading
import time

from kitlib import hubmirror
from kitlib.git import cmd, refs, repo

from . import store as _store

SUBJECT = 'hub: store sync'     # the whole commit message: no body, no trailer
NET_TIMEOUT = 120               # seconds one fetch or one push may take
ADD_CHARS = 6000                # the most path text one `git add` is given (a Windows command line)

# The steps a pass can fail at, in the order they run; `internal` is a pass that raised.
STEPS = ('branch', 'status', 'read', 'fetch', 'fast-forward', 'commit', 'push', 'internal')

_NO_REMOTE_REF = "couldn't find remote ref"     # git's stderr (C locale) for a branch the remote lacks
_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\Z')
_PROMPTS_OFF = ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE')

# root: the work tree's top level; rel: the store folder below it, forward slashes; branch: the
# data branch; remote: the remote's name, or None when there is none to push to.
Plan = collections.namedtuple('Plan', 'root rel branch remote')


class Refused(Exception):
    """The store folder cannot be synced from where it is; the message says why."""


def skipped(path):
    """True for a store path that is never committed: the single-writer lock file and the temp
    file of a write in flight (kitlib.hubsite.store names them `.<name>.<pid>.tmp`)."""
    name = path.rsplit('/', 1)[-1]
    return name == _store.LOCK_NAME or (name.startswith('.') and name.endswith(_store.TMP_SUFFIX))


def _name_ok(value):
    return (isinstance(value, str) and bool(_NAME.match(value)) and '..' not in value
            and not value.endswith(('/', '.lock')))


def _rel(folder, root):
    """`folder` relative to `root` with forward slashes, or None when it is not inside."""
    for a, b in ((os.path.abspath(folder), os.path.abspath(root)),
                 (os.path.realpath(folder), os.path.realpath(root))):
        try:
            rel = os.path.relpath(a, b).replace('\\', '/')
        except ValueError:          # another drive
            continue
        if rel != '..' and not rel.startswith('../'):
            return rel
    return None


def prepare(store_dir, remote=None, branch=None):
    """A Plan for syncing the store folder `store_dir`, or Refused. Runs before anything is
    written and writes nothing itself.

    `branch`: the data branch the caller expects (pm_data.branch); None accepts the branch that is
    checked out. `remote`: the remote to fetch from and push to (pm_data.remote); None takes the
    remote recorded for the data branch in the kit's repository (kit-pm-data.json), and a store in
    any other repository then has no remote: its Plan commits and never fetches or pushes."""
    folder = os.path.abspath(store_dir)
    while not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            raise Refused('no folder above the hub store %s exists' % store_dir)
        folder = parent
    root, why = repo.toplevel(folder)
    if root is None:
        raise Refused('the hub store %s is in no git work tree: %s' % (store_dir, why))
    current, why = refs.current_branch(root)
    if current is None:
        raise Refused('cannot read the branch of %s: %s' % (root, why))
    if not current:
        raise Refused('HEAD is detached in %s: the hub store is committed to a branch' % root)
    if remote is not None and not _name_ok(remote):
        raise Refused('the remote must be a remote name, not %r' % (remote,))
    try:
        recorded = hubmirror._kit_checkout_error(root, current, remote)
    except hubmirror.Refused as e:
        raise Refused('the hub store cannot be synced from %s: %s' % (root, e))
    if branch is not None and current != branch:
        raise Refused('%s is on %r, not the data branch %s' % (root, current, branch))
    if not _name_ok(current):
        raise Refused('the branch of %s, %r, is not a name the hub store sync handles'
                      % (root, current))
    rel = _rel(store_dir, root)
    if rel is None:
        raise Refused('the hub store %s is outside %s' % (store_dir, root))
    if rel in ('', '.'):
        raise Refused('the hub store %s is the root of its work tree: it must be a folder below '
                      'it, so the commit is limited to the store' % store_dir)
    if rel == '.git' or rel.startswith('.git/'):
        raise Refused('the hub store %s is inside the git directory' % store_dir)
    if rel.startswith(':') or any(c in rel for c in '*?[]'):
        raise Refused('the hub store folder %r holds a character git reads as a pattern: the '
                      'commit could not be limited to it' % rel)
    name = remote or recorded
    if name is not None and not _name_ok(name):
        raise Refused('the recorded remote must be a remote name, not %r' % (name,))
    return Plan(root, rel, current, name)


def _first_line(text):
    """What a status object carries of git's stderr, cut to 300 characters: its first `fatal:` or
    `error:` line, else its last line that is not a hint."""
    lines = [l.strip() for l in (text or '').splitlines()]
    lines = [l for l in lines if l and not l.startswith('hint:')]
    for line in lines:
        if line.startswith(('fatal:', 'error:')):
            return line[:300]
    return lines[-1][:300] if lines else ''


def _chunks(paths, limit):
    """`paths` in order, in runs whose text stays under `limit` characters (one path at least)."""
    run, size = [], 0
    for p in paths:
        if run and size + len(p) + 1 > limit:
            yield run
            run, size = [], 0
        run.append(p)
        size += len(p) + 1
    if run:
        yield run


class GitSync:
    """The store folder's sync, one per hub server.

        sync = GitSync(store_dir, commit_every_s, remote='private', branch='pmdata')
        sync.start()            # a thread; a pass at once, then one every commit_every_s
        ... sync.status() ...   # what /health shows
        sync.stop()             # joins the thread

    The constructor runs prepare() and raises Refused; it starts nothing. `push` False commits
    and never fetches or pushes. `clock` (default time.time) answers seconds and decides when a
    pass is due; `git` (default kitlib.git.cmd.call) runs every git command of a pass as
    `git(args, cwd, timeout=, env=)` and answers an object with rc (None: git did not run), out
    (bytes) and err (text); `env` is the environment those commands get (None: cmd.call's own).
    One question is asked outside `git`: refs.is_ancestor, after a fast-forward that failed.

    poll() and sync_once() may be called from any thread, with or without the thread running: one
    pass runs at a time, and status() never waits for one."""

    def __init__(self, store_dir, commit_every_s, remote=None, branch=None, push=True,
                 clock=None, git=None, env=None):
        if isinstance(commit_every_s, bool) or not isinstance(commit_every_s, (int, float)) \
                or not 0 < commit_every_s < float('inf'):
            raise ValueError('commit_every_s must be a positive number of seconds, not %r'
                             % (commit_every_s,))
        self.plan = prepare(store_dir, remote, branch)
        self.every = commit_every_s
        self.push = bool(push) and self.plan.remote is not None
        self._clock = clock or time.time
        self._run = git or cmd.call
        self._env = env
        self._pass_lock = threading.Lock()      # one pass at a time
        self._lock = threading.Lock()           # _state, _due, _thread, _stop
        self._thread = None
        self._stop = threading.Event()
        self._due = self._clock() + self.every
        self._pushed = None                     # the commit the last push of this process sent
        self._state = {
            'ok': None, 'failed': '', 'detail': '', 'pending_commit': False,
            'pending_push': False, 'diverged': False, 'failing_since': None,
            'last_pass_at': None, 'last_commit_at': None, 'last_commit': '',
            'last_push_at': None}

    # ---- the thread ---------------------------------------------------------------------------

    def start(self):
        """Start the sync thread: a pass at once, then one every commit_every_s. False when it
        is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self._due = self._clock()
            self._thread = threading.Thread(target=self._loop, args=(self._stop,),
                                            name='hub-gitsync', daemon=True)
            thread = self._thread
        thread.start()
        return True

    def stop(self, timeout=None):
        """Ask the thread to end and join it (`timeout` seconds; None waits for the pass in
        flight). True when no thread is left running. Runs no last pass: a caller that wants what
        was written since the last one committed calls sync_once() after stop()."""
        with self._lock:
            thread, stop = self._thread, self._stop
        if thread is None:
            return True
        stop.set()
        thread.join(timeout)
        if thread.is_alive():
            return False
        with self._lock:
            if self._thread is thread:
                self._thread = None
        return True

    def _loop(self, stop):
        while True:
            now = self._clock()
            with self._lock:
                wait = self._due - now
            if stop.wait(min(max(wait, 0.0), self.every)):
                return
            self.poll()

    def poll(self):
        """Run a pass when one is due on the clock and return its status; None when none is due.
        The next pass is due commit_every_s after this one ends, so a pass that waits out a dead
        network is not followed at once by another. A clock that went backwards by more than
        commit_every_s counts as due."""
        now = self._clock()
        with self._lock:
            if 0 < self._due - now <= self.every:
                return None
            self._due = now + self.every        # a second caller meanwhile finds none due
        try:
            return self.sync_once()
        finally:
            with self._lock:
                self._due = self._clock() + self.every

    # ---- the status ---------------------------------------------------------------------------

    def status(self):
        """The sync's state as a new dict, for the server's /health. Never waits for a pass.

          running          the thread is alive
          store, branch    the store folder (relative to its work tree) and the data branch
          remote           the remote's name; '' when nothing is fetched or pushed
          every            commit_every_s
          ok               the last pass ended without a failure; None before the first pass
          failed           the step the last pass failed at (one of STEPS), else ''
          detail           why, in a line; git's own words may name a remote URL or a path, so
                           show it only where the caller may see those
          pending_commit   the store folder holds changes that are not committed
          pending_push     the data branch holds commits the remote does not
          diverged         the remote's branch and the local one have diverged: a person joins them
          failing_since    the clock at the first of the failing passes still running, else None
          last_pass_at, last_commit_at, last_push_at    clock values, None before the first
          last_commit      the last commit this process made, a full hash, else ''"""
        with self._lock:
            out = dict(self._state)
            thread = self._thread
        out['running'] = thread is not None and thread.is_alive()
        out['store'] = self.plan.rel
        out['branch'] = self.plan.branch
        out['remote'] = self.plan.remote if self.push else ''
        out['every'] = self.every
        return out

    # ---- one pass -----------------------------------------------------------------------------

    def sync_once(self):
        """Run one pass now (waiting for a pass in flight first) and return status(). Never
        raises for a git failure or an error of its own: the pass is recorded as failed."""
        with self._pass_lock:
            now = self._clock()
            try:
                found = self._pass(now)
            except Exception as e:      # the thread must outlive any one pass
                found = {'failed': 'internal', 'detail': '%s: %s' % (type(e).__name__, e)}
            with self._lock:
                state = self._state
                state.update(found)
                state['failed'] = found.get('failed', '')
                state['detail'] = found.get('detail', '') if state['failed'] else ''
                state['ok'] = not state['failed']
                state['last_pass_at'] = now
                if state['ok']:
                    state['failing_since'] = None
                elif state['failing_since'] is None:
                    state['failing_since'] = now
        return self.status()

    def _git(self, args, timeout=None, env=None):
        kw = {'env': self._env if env is None else env}
        if timeout is not None:
            kw['timeout'] = timeout
        return self._run(args, self.plan.root, **kw)

    def _tip(self, ref):
        """The commit `ref` names, or None."""
        r = self._git(['rev-parse', '--verify', '%s^{commit}' % ref])
        sha = cmd.text(r.out).strip() if r.rc == 0 else ''
        return sha if refs.is_sha(sha) else None

    def _push_env(self):
        env = dict(cmd.scrubbed_env() if self._env is None else self._env)
        env.update((k, cmd.NETWORK_ENV[k]) for k in _PROMPTS_OFF)
        return env

    def _pass(self, now):
        """One pass; the status keys it settled. `failed` and `detail` are set only on failure."""
        plan = self.plan
        head_ref = 'refs/heads/%s' % plan.branch
        track_ref = 'refs/remotes/%s/%s' % (plan.remote, plan.branch)

        r = self._git(['branch', '--show-current'])
        current = cmd.text(r.out).strip() if r.rc == 0 else None
        if current != plan.branch:
            why = ('the work tree is on %r, not the data branch %s' % (current, plan.branch)
                   if current is not None else 'cannot read the branch: %s' % _first_line(r.err))
            return {'failed': 'branch', 'detail': why}

        r = self._git(['status', '--porcelain', '-z', '--untracked-files=all', '--', plan.rel])
        if r.rc != 0:
            return {'failed': 'status', 'detail': _first_line(r.err) or 'git status exited %s' % r.rc}
        entries = [(xy, p) for xy, p in refs.parse_porcelain_z(cmd.text(r.out)) if not skipped(p)]
        out = {'pending_commit': bool(entries)}

        local = self._tip(head_ref)
        if local is None:
            out.update(failed='read', detail='cannot read the commit of %s' % head_ref)
            return out
        if not self.push:
            if entries:
                out.update(self._commit(entries, now))
            out['pending_push'] = False
            return out

        tracking = self._tip(track_ref)
        if not entries and local in (tracking, self._pushed):
            out.update(pending_push=False, diverged=False)
            return out

        r = self._git(['fetch', '-q', plan.remote, '%s:%s' % (head_ref, track_ref)],
                      timeout=NET_TIMEOUT)
        absent = r.rc is not None and r.rc != 0 and _NO_REMOTE_REF in (r.err or '')
        fetched = r.rc == 0 or absent
        fetch_why = '' if fetched else (_first_line(r.err) or 'git fetch exited %s' % r.rc)

        if fetched and not absent:
            tracking = self._tip(track_ref)
            if tracking is None:
                out.update(failed='fetch', detail='fetched, but cannot read %s' % track_ref,
                           pending_push=True)
                return out
            if tracking != local:
                r = self._git(['merge', '-q', '--ff-only', track_ref])
                if r.rc != 0:
                    contains, _why = refs.is_ancestor(local, tracking, plan.root)
                    out.update(failed='fast-forward', diverged=contains is False,
                               pending_push=contains is not True,
                               detail=_first_line(r.err) or 'git merge --ff-only exited %s' % r.rc)
                    return out
                local = self._tip(head_ref)
                if local is None:
                    out.update(failed='read', detail='cannot read the commit of %s' % head_ref)
                    return out
        if fetched:
            out['diverged'] = False     # fast-forwarded, level, ahead, or no branch there yet

        if entries:
            done = self._commit(entries, now)
            out.update(done)
            if not done.get('failed'):
                local = done['last_commit']

        if not fetched:
            out['pending_push'] = True      # unknown without the remote: the next pass finds out
            if not out.get('failed'):
                out.update(failed='fetch', detail=fetch_why)
            return out

        if absent or local != tracking:
            r = self._git(['push', '-q', plan.remote, '%s:%s' % (head_ref, head_ref)],
                          timeout=NET_TIMEOUT, env=self._push_env())
            if r.rc != 0:
                out['pending_push'] = True
                if not out.get('failed'):
                    out.update(failed='push',
                               detail=_first_line(r.err) or 'git push exited %s' % r.rc)
                return out
            self._pushed = local
            out['last_push_at'] = now
        out['pending_push'] = False
        return out

    def _commit(self, entries, now):
        """Commit the store folder; the status keys that settles. Untracked store files are added
        by name, tracked ones (changed or deleted) are taken by the pathspec."""
        plan = self.plan
        why = None
        for run in _chunks(sorted(p for xy, p in entries if xy == '??'), ADD_CHARS):
            r = self._git(['add', '--'] + run)
            if r.rc != 0:
                why = _first_line(r.err) or 'git add exited %s' % r.rc
                break
        if why is None:
            r = self._git(['commit', '-q', '-m', SUBJECT, '--', plan.rel])
            if r.rc != 0:
                why = _first_line(r.err) or 'git commit exited %s' % r.rc
        sha = self._tip('refs/heads/%s' % plan.branch) if why is None else None
        if why is None and sha is None:
            why = 'committed, but cannot read the new commit'
        if why is not None:
            self._git(['reset', '-q', '--', plan.rel])      # the index only: nothing stays staged
            return {'failed': 'commit', 'detail': why, 'pending_commit': True}
        return {'pending_commit': False, 'last_commit': sha, 'last_commit_at': now}
