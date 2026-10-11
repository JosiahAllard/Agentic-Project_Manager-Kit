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
  2. status   `--no-optional-locks status --porcelain -z --untracked-files=all -- <store>`: what
              differs from HEAD under the store folder, read without rewriting the index. The
              store's lock file and its temp files are never counted and never added.
  3. read     the branch's commit and the remote-tracking ref's commit (`rev-parse --verify`).
     Nothing to commit and nothing to push ends the pass here: an idle sync runs no fetch.
  4. fetch    `fetch -q --no-tags <remote> refs/heads/<b>:refs/remotes/<remote>/<b>`, never
              with `+`. A remote that has no such branch yet is not a failure: the push creates
              it.
  5. fast-forward
              the guard (below), then `merge -q --ff-only --no-autostash refs/remotes/<remote>/<b>`
              when the two commits differ.
  6. commit   the guard again and the identity check, then `add -- <untracked store files>` and
              `commit -q --no-gpg-sign -m SUBJECT -- <store>`.
  7. push     `push -q <remote> refs/heads/<b>:refs/heads/<b>`, never forced.

The fetch and the fast-forward run BEFORE the commit. That order is what makes a remote that
moved cost nothing: the branch is fast-forwarded with the store's changes still uncommitted in the
work tree, the commit lands on top of the remote's commit, and the push is a fast-forward. Commit
first and the same case is two diverged histories.

The guard. The fetch can take minutes, and the data worktree belongs to other tools too. So
before each of the two local writes (the fast-forward, the commit) the pass asks again which
branch is checked out, and looks in the work tree's own git directory (`rev-parse --git-path`,
read once) for MARKERS: a merge, cherry-pick, revert or rebase under way, or an `index.lock`. A
branch that changed ends the pass at `branch`; a marker ends it at `busy`, and the detail names
what was found. Either way that pass writes nothing more: no fast-forward, no add, no commit, no
reset, no push. What is left between the guard and git's own start is a few milliseconds, in
which git's index lock still refuses a second writer.

The fast-forward never stashes. `--no-autostash` overrides a `merge.autoStash` the owner has set:
uncommitted changes in the data worktree, the store's or anybody else's, are either left exactly
as they are under a fast-forward that does not touch them, or git refuses the fast-forward.

What a failure leaves, step by step:

  - fetch fails (no network, a remote that is down): the commit is still made, nothing is pushed,
    the pass fails at `fetch` with `pending_push` set.
  - the fast-forward fails: NOTHING is committed and nothing is pushed in that pass. The cause may
    pass by itself (another tool held the index for a moment), and a commit made then, on the old
    tip, would turn a remote that only moved into a real divergence. `diverged` is True when git
    says the remote's commit does not contain the branch's (refs.is_ancestor, a definite NO only):
    the two histories have to be joined by a person. gitsync never merges, rebases or forces.
  - no git identity (identity check, below): nothing is added or committed, the pass fails at
    `commit` with a detail that begins "git identity not set".
  - add or commit fails or is refused (a hook, the index held): the paths THIS pass added are
    unstaged again (`reset -q -- <those paths>`, the index only), so no other commit in the data
    worktree finds them staged. A store path somebody else had staged before the add stays
    staged. `pending_commit` stays set and the next pass tries again; commits made earlier are
    still pushed. A reset that fails too is said in the same detail.
  - a merge, add, commit or reset that ran into its time limit was stopped half way and may have
    left `index.lock` behind: the detail says so. Nothing here ever removes an `index.lock`; the
    next pass finds it and ends at `busy` until a person has removed it.
  - push fails or is refused: `pending_push` stays set and the next pass tries again.
  - `push` on and no remote to push to: every pass commits, and reports `pending_push` with
    failed `push` and the detail NO_REMOTE, from the constructor on. Passing `push` False is how
    a caller says a store is meant to stay local.

Every pass ends in the status object (GitSync.status), which the server's /health reads.

The commit is a plain data commit: the machine's own git identity (no --author, no -c), the fixed
subject SUBJECT, no body, no agent trailer, limited by pathspec to the store folder, hooks left
on, never signed (`--no-gpg-sign`: a signing program may ask for a passphrase, and nobody watches
this thread). It does not go through kitlib.commit: that is the path for an agent's change to
tracked code, with a model trailer and a staged machine-path scan, and neither fits data the owner
wrote in a browser. Other paths staged in the data worktree stay staged and out of the commit.

The identity check. Before the add, `config --get-regexp` reads user.name, user.email and the
author.* and committer.* forms. A name and an email must each come from GIT_AUTHOR_* and
GIT_COMMITTER_* in the child's environment or from that config (the email also from EMAIL), the
sources git itself reads first. When one is missing git would make it up from the account and
host name; no commit is made instead.

Nothing may wait for a person. A fetch and a push run with git's terminal prompt and the
credential manager's window off (kitlib.git.cmd.NETWORK_ENV's two switches), and with GIT_ASKPASS
and SSH_ASKPASS set to the empty string: git reads an empty GIT_ASKPASS as "no helper program",
ahead of core.askPass and SSH_ASKPASS, so no program is started and a missing credential is a
failed fetch or push. GIT_SSH_COMMAND is set to SSH_BATCH (`ssh -o BatchMode=yes`: ssh fails
where it would ask for a passphrase or a password) only when nothing else chooses the ssh
command: not when the environment sets GIT_SSH_COMMAND or GIT_SSH, and not when `core.sshCommand`
is set or cannot be read. Those are the owner's own choice of key and program, and GIT_SSH_COMMAND
would replace them; with one of them set, keeping ssh from asking is up to that choice.

Machine paths. A hub document is the owner's own text and may hold a path of this machine. Nothing
here scans or changes it. The machine-path check (kitlib.machinepaths) keeps running for
everything else in the data worktree; the data worktree's own `.kit.json` leaves the store out
with an allow-list entry, for a store folder `hub`:

    "machine_paths": {"allow": [{"glob": "hub/*", "line": ".",
                                 "why": "hub documents are the owner's own text"}]}

(machinepaths matches the glob with fnmatch, whose `*` crosses `/`, so `hub/*` covers every
collection.) That entry is an allow-list, never a scrub of the owner's text. gitsync reads and
writes no `.kit.json`.

No git config is written, ever: `config` is run only as a read (`--get`, `--get-regexp`), and
there is no `remote` write and no upstream. Every fetch and push names its refspec on the command
line, so `.git/config` is byte-identical after any number of passes
(tests/hubsite/test_gitsync.py asserts it on real git).

prepare() refuses, before anything runs: a store folder in no git work tree, a detached HEAD, a
branch other than the one the caller named, a store folder that is the work tree's root (the
pathspec would then cover the whole data worktree) or whose path git would read as a pattern, a
store folder git tracks under another spelling than the folder has on disk, a remote that is not
a plain name or that the repository does not have, and a work tree of the kit itself, through
kitlib.hubmirror's own check (_kit_checkout_error): a checkout holding the kit's code, a work tree
of the kit's repository that is not on its recorded data branch, or a remote other than the
recorded one. The hub holds the owner's private data and the kit's history is public.

Limits, stated so nobody has to find them:

  - An idle sync does not fetch. What another machine pushed arrives with the next pass that has
    something to commit or push.
  - The Store never reloads from disk. A fast-forward that brings store documents written on
    another machine changes files under a running Store, which will not see them until it is
    opened again. Two machines writing one store is outside what this module makes safe.
  - The store's lock is not taken. kitlib.hubsite.store keeps its folder lock for as long as a
    Store is open and its write mutex to itself, so a commit can fall between the two files of
    one write (`_meta.json`, then the document) and hold the first without the second. The next
    pass commits the rest; one commit by itself is not promised to be a consistent store.
  - Two machines that both commit before either pushes have diverged; that is reported, not
    repaired. The same holds for a remote that moves between one pass's fetch and its push when
    that pass committed: the push is refused (pass failed at `push`) and the next pass reports
    `diverged`.
  - A fast-forward that would overwrite a file changed and not committed in the data worktree
    (the other machine's commit and this store both rewrote `_meta.json`, say) is refused by git
    and stays refused: every pass fails at `fast-forward` with `diverged` False, until a person
    settles it. Commits from elsewhere that touch no uncommitted file (backlog records, most of
    the data branch) fast-forward under the store's changes.
  - A pass can take as long as git takes: stop() waits for the pass in flight (NET_TIMEOUT at
    most for a fetch or a push, kitlib.git.cmd.GIT_TIMEOUT for anything else).
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
STEPS = ('branch', 'status', 'read', 'fetch', 'busy', 'fast-forward', 'commit', 'push', 'internal')

# What the guard looks for in the work tree's own git directory: another git operation under way.
MARKERS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply',
           'index.lock')
NO_REMOTE = 'no remote to push to: none was named and none is recorded for the data branch'
NO_IDENTITY = 'git identity not set'
LOCK_NOTE = ('; git was stopped at its time limit and may have left index.lock behind in the git '
             'directory (nothing here removes it)')
SSH_BATCH = 'ssh -o BatchMode=yes'

_NO_REMOTE_REF = "couldn't find remote ref"     # git's stderr (C locale) for a branch the remote lacks
_NO_SUCH_REMOTE = 2                             # `git remote get-url <name>`: no remote of that name
_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\Z')
_PROMPTS_OFF = ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE')
_ASKPASS_OFF = {'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}
_IDENTITY_KEYS = r'^(user|author|committer)\.(name|email)$'

# root: the work tree's top level; rel: the store folder below it, forward slashes, spelled as
# the folder is on disk; branch: the data branch; remote: the remote's name, or None when there
# is none to push to.
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


def _disk_case(root, rel):
    """`rel` with every part that exists below `root` spelled as its folder lists it. A caller may
    name the store in any case on a file system that folds case; git matches a pathspec by its
    exact spelling, and a store named `HUB` for the folder `hub` would match nothing. A part that
    is not there yet keeps the caller's spelling: the store makes the folder under that name."""
    parts, here = [], root
    for part in rel.split('/'):
        try:
            names = os.listdir(here)
        except OSError:
            names = []
        if part not in names:
            same = [n for n in names if os.path.normcase(n) == os.path.normcase(part)]
            if len(same) == 1:
                part = same[0]
        parts.append(part)
        here = os.path.join(here, part)
    return '/'.join(parts)


def _tracked_as(root, rel):
    """The other spellings under which git's index holds the folder `rel` (same letters, another
    case), sorted; Refused when the index cannot be read."""
    r = cmd.call(['ls-files', '-z', '--full-name'], root)
    if r.rc != 0:
        raise Refused('cannot read what git tracks in %s: %s'
                      % (root, _first_line(r.err) or 'git ls-files exited %s' % r.rc))
    depth, want, other = rel.count('/') + 1, os.path.normcase(rel), set()
    for path in cmd.text(r.out).split('\0'):
        parts = path.split('/')
        if len(parts) > depth:
            head = '/'.join(parts[:depth])
            if head != rel and os.path.normcase(head) == want:
                other.add(head)
    return sorted(other)


def prepare(store_dir, remote=None, branch=None):
    """A Plan for syncing the store folder `store_dir`, or Refused. Runs before anything is
    written and writes nothing itself.

    `branch`: the data branch the caller expects (pm_data.branch); a work tree on any other
    branch is Refused. None means the caller does not know its name, and the branch checked out
    when prepare() runs IS then the data branch for the life of the Plan: in the kit's own
    repository that can only be the recorded data branch (the hubmirror check), in any other
    repository nothing else vouches for it, so a caller that has the name passes it. Either way
    the branch has to be readable and attached: a detached HEAD, or a work tree whose branch git
    cannot name, is Refused.

    `remote`: the remote to fetch from and push to (pm_data.remote); None takes the remote
    recorded for the data branch in the kit's repository (kit-pm-data.json), and a store in any
    other repository then has no remote: its Plan commits and never fetches or pushes. A remote
    the repository does not have is Refused.

    The Plan's `rel` is the store folder spelled as it is on disk, whatever case the caller
    wrote; a store git tracks under one spelling while the folder has another is Refused."""
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
        raise Refused('the data branch cannot be determined: cannot read the branch of %s: %s'
                      % (root, why))
    if not current:
        raise Refused('the data branch cannot be determined: HEAD is detached in %s, and the hub '
                      'store is committed to a branch' % root)
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
    if rel.lower() == '.git' or rel.lower().startswith('.git/'):
        raise Refused('the hub store %s is inside the git directory' % store_dir)
    if rel.startswith(':') or any(c in rel for c in '*?[]'):
        raise Refused('the hub store folder %r holds a character git reads as a pattern: the '
                      'commit could not be limited to it' % rel)
    rel = _disk_case(root, rel)
    other = _tracked_as(root, rel)
    if other:
        raise Refused('git tracks the hub store folder as %s and the folder on disk is %r: git '
                      'matches a path by its exact spelling, so nothing would be committed. '
                      'Rename one to match the other' % (', '.join(repr(o) for o in other), rel))
    name = remote or recorded
    if name is not None:
        if not _name_ok(name):
            raise Refused('the recorded remote must be a remote name, not %r' % (name,))
        r = cmd.call(['remote', 'get-url', name], root)
        if r.rc == _NO_SUCH_REMOTE:
            raise Refused('%s has no remote named %r' % (root, name))
        if r.rc != 0:
            raise Refused('cannot read whether %s has the remote %r: %s'
                          % (root, name, _first_line(r.err) or 'git remote get-url exited %s' % r.rc))
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


def _why(r, what):
    """Why the git command `what` failed, from its result `r`; with LOCK_NOTE when it wrote to
    the index and was stopped at its time limit (kitlib.git.cmd.call answers rc None and
    'git ... timed out after N seconds')."""
    why = _first_line(r.err) or 'git %s exited %s' % (what, r.rc)
    if r.rc is None and 'timed out' in (r.err or ''):
        why += LOCK_NOTE
    return why


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
    and never fetches or pushes, and says nothing about it; `push` True with no remote to push to
    commits too, and status() reports that from the start (NO_REMOTE). `clock` (default
    time.time) answers seconds and decides when a pass is due; `git` (default
    kitlib.git.cmd.call) runs every git command of a pass as `git(args, cwd, timeout=, env=)` and
    answers an object with rc (None: git did not run), out (bytes) and err (text); `env` is the
    environment those commands get (None: cmd.call's own). One question is asked outside `git`:
    refs.is_ancestor, after a fast-forward that failed or was not tried.

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
        self._no_remote = bool(push) and self.plan.remote is None
        self._clock = clock or time.time
        self._run = git or cmd.call
        self._env = env
        self._pass_lock = threading.Lock()      # one pass at a time
        self._lock = threading.Lock()           # _state, _due, _thread, _stop
        self._thread = None
        self._stop = threading.Event()
        self._due = self._clock() + self.every
        self._pushed = None                     # the commit the last push of this process sent
        self._marker_paths = None               # [(name, path)] of MARKERS, read by the first guard
        self._state = {
            'ok': None, 'failed': 'push' if self._no_remote else '',
            'detail': NO_REMOTE if self._no_remote else '', 'pending_commit': False,
            'pending_push': self._no_remote, 'diverged': False, 'failing_since': None,
            'last_pass_at': None, 'last_commit_at': None, 'last_commit': '',
            'last_push_at': None}

    # ---- the thread ---------------------------------------------------------------------------

    def start(self):
        """Start the sync thread: a pass at once, then one every commit_every_s. False, and
        nothing started, when it is already running. The thread is made AND started under the
        lock, so of two callers at the same moment one starts it and the other finds it alive."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            stop = threading.Event()
            thread = threading.Thread(target=self._loop, args=(stop,), name='hub-gitsync',
                                      daemon=True)
            self._due = self._clock()
            thread.start()
            self._stop, self._thread = stop, thread
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
          failed           the step the last pass failed at (one of STEPS), else ''; `push` from
                           the constructor on when `push` is on and there is no remote
          detail           why, in a line; git's own words may name a remote URL or a path, so
                           show it only where the caller may see those
          pending_commit   the store folder holds changes that are not committed
          pending_push     the data branch holds commits the remote does not (always, when
                           `push` is on and there is no remote)
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

    def _net_env(self):
        """The environment of this pass's fetch and push: nothing in it may wait for a person
        (the module docstring, "Nothing may wait for a person")."""
        env = dict(cmd.scrubbed_env() if self._env is None else self._env)
        forced = dict(_ASKPASS_OFF)
        forced.update((k, cmd.NETWORK_ENV[k]) for k in _PROMPTS_OFF)
        for key in [k for k in env if k.upper() in forced]:     # Windows reads names in any case
            del env[key]
        env.update(forced)
        names = {k.upper() for k in env}
        if 'GIT_SSH_COMMAND' not in names and 'GIT_SSH' not in names:
            r = self._git(['config', '--get', 'core.sshCommand'])
            if r.rc == 1:               # git's answer for a key that is not set
                env['GIT_SSH_COMMAND'] = SSH_BATCH
        return env

    def _on_branch(self):
        """None while the work tree is on the data branch, else the keys of a pass that ends at
        `branch`."""
        r = self._git(['branch', '--show-current'])
        current = cmd.text(r.out).strip() if r.rc == 0 else None
        if current == self.plan.branch:
            return None
        why = ('the work tree is on %r, not the data branch %s' % (current, self.plan.branch)
               if current is not None else 'cannot read the branch: %s' % _first_line(r.err))
        return {'failed': 'branch', 'detail': why}

    def _guard(self):
        """Asked immediately before a write to the work tree or its index: None when the work
        tree is still on the data branch and no other git operation holds it, else the keys of a
        pass that ends here (`branch`, or `busy` with the MARKERS found)."""
        stop = self._on_branch()
        if stop is not None:
            return stop
        if self._marker_paths is None:
            args = ['rev-parse', '--path-format=absolute']
            for name in MARKERS:
                args += ['--git-path', name]
            r = self._git(args)
            lines = cmd.text(r.out).splitlines() if r.rc == 0 else []
            if len(lines) != len(MARKERS) or not all(os.path.isabs(l) for l in lines):
                return {'failed': 'busy', 'detail': 'cannot tell whether another git operation '
                        'holds the work tree: %s' % (_first_line(r.err) or 'git rev-parse '
                                                     '--git-path answered %r' % (lines,))}
            self._marker_paths = list(zip(MARKERS, lines))
        found = [name for name, path in self._marker_paths if os.path.lexists(path)]
        if found:
            return {'failed': 'busy', 'detail': 'another git operation holds the work tree (%s): '
                    'nothing was written' % ', '.join(found)}
        return None

    def _identity_missing(self):
        """'' when git has a name and an email for the commit's author and committer without
        making one up, else the detail of the refusal (it begins with NO_IDENTITY)."""
        r = self._git(['config', '--get-regexp', _IDENTITY_KEYS])
        if r.rc not in (0, 1):          # 1: none of the keys is set
            return '%s: cannot read the git config (%s)' % (
                NO_IDENTITY, _first_line(r.err) or 'git config exited %s' % r.rc)
        config = {}
        for line in cmd.text(r.out).splitlines():
            key, _, value = line.partition(' ')
            config[key.lower()] = value.strip()
        source = os.environ if self._env is None else self._env
        env = {k.upper(): v for k, v in source.items()}
        missing = []
        for who in ('author', 'committer'):
            if not (env.get('GIT_%s_NAME' % who.upper()) or config.get('%s.name' % who)
                    or config.get('user.name')):
                missing.append('user.name')
            if not (env.get('GIT_%s_EMAIL' % who.upper()) or config.get('%s.email' % who)
                    or config.get('user.email') or env.get('EMAIL')):
                missing.append('user.email')
        if not missing:
            return ''
        return ('%s: no %s for the commit, and one made up from the account and the host name '
                'is never used' % (NO_IDENTITY, ' and no '.join(sorted(set(missing)))))

    def _pass(self, now):
        """One pass; the status keys it settled. `failed` and `detail` are set only on failure."""
        plan = self.plan
        head_ref = 'refs/heads/%s' % plan.branch
        track_ref = 'refs/remotes/%s/%s' % (plan.remote, plan.branch)

        stop = self._on_branch()
        if stop is not None:
            return stop

        r = self._git(['--no-optional-locks', 'status', '--porcelain', '-z',
                       '--untracked-files=all', '--', plan.rel])
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
                out.update(self._guard() or self._commit(entries, now))
            out['pending_push'] = self._no_remote
            if self._no_remote and not out.get('failed'):
                out.update(failed='push', detail=NO_REMOTE)
            return out

        tracking = self._tip(track_ref)
        if not entries and local in (tracking, self._pushed):
            out.update(pending_push=False, diverged=False)
            return out

        net = self._net_env()
        r = self._git(['fetch', '-q', '--no-tags', plan.remote, '%s:%s' % (head_ref, track_ref)],
                      timeout=NET_TIMEOUT, env=net)
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
                stop = self._guard()
                if stop is None:
                    r = self._git(['merge', '-q', '--ff-only', '--no-autostash', track_ref])
                    if r.rc != 0:
                        stop = {'failed': 'fast-forward', 'detail': _why(r, 'merge --ff-only')}
                if stop is not None:
                    contains, _unread = refs.is_ancestor(local, tracking, plan.root)
                    out.update(stop, diverged=contains is False, pending_push=contains is not True)
                    return out
                local = self._tip(head_ref)
                if local is None:
                    out.update(failed='read', detail='cannot read the commit of %s' % head_ref)
                    return out
        if fetched:
            out['diverged'] = False     # fast-forwarded, level, ahead, or no branch there yet

        if entries:
            stop = self._guard()
            if stop is not None:        # nothing more is written in this pass, the push included
                out.update(stop, pending_push=not fetched or absent or local != tracking)
                return out
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
                          timeout=NET_TIMEOUT, env=net)
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
        """Commit the store folder; the status keys that settles. The caller has just asked the
        guard. Untracked store files are added by name, tracked ones (changed or deleted) are
        taken by the pathspec. On a failure only what this pass added is unstaged again."""
        plan = self.plan
        why = self._identity_missing()
        if why:
            return {'failed': 'commit', 'detail': why, 'pending_commit': True}
        new = sorted(p for xy, p in entries if xy == '??')
        staged = set()
        if new:     # what somebody else staged under the store since the status: never unstaged here
            r = self._git(['diff', '--cached', '--name-only', '-z', '--', plan.rel])
            if r.rc != 0:
                return {'failed': 'commit', 'pending_commit': True,
                        'detail': 'cannot read what is staged under the store: %s'
                                  % (_first_line(r.err) or 'git diff --cached exited %s' % r.rc)}
            staged = set(cmd.text(r.out).split('\0'))
        mine, why = [], None
        for run in _chunks(new, ADD_CHARS):
            mine += [p for p in run if p not in staged]
            r = self._git(['add', '--'] + run)
            if r.rc != 0:
                why = _why(r, 'add')
                break
        if why is None:
            r = self._git(['commit', '-q', '--no-gpg-sign', '-m', SUBJECT, '--', plan.rel])
            if r.rc != 0:
                why = _why(r, 'commit')
        sha = self._tip('refs/heads/%s' % plan.branch) if why is None else None
        if why is None and sha is None:
            why = 'committed, but cannot read the new commit'
        if why is None:
            return {'pending_commit': False, 'last_commit': sha, 'last_commit_at': now}
        for run in _chunks(mine, ADD_CHARS):        # the index only, and only this pass's own adds
            r = self._git(['reset', '-q', '--'] + run)
            if r.rc != 0:
                why += ('; and what this pass staged under the store could not be unstaged '
                        'again: %s' % _why(r, 'reset'))
                break
        return {'failed': 'commit', 'detail': why, 'pending_commit': True}
