"""Create a workstream's worktree in one call (ported from the PM kit's tools/new_worktree.py;
docs/new-worktree.md). tools/new_worktree.py is the entry point.

`create(root, branch, existing, base)` acts on the MAIN checkout of the repository `root` is in
(the main checkout or any linked worktree): `git fetch <remote>`; refuses unless local `<base>`
equals `<remote>/<base>` (two exceptions, a landing in progress and a local base strictly behind the
remote, below; a base passed as `<remote>/<name>` names the branch `<name>`, branch_name); then
`git worktree add <worktrees_dir>/<slug> -b <area>/<slug> <base's hash>` for a new branch, or
`git worktree add <worktrees_dir>/<slug> <area>/<slug>` with `existing`. It never switches a
branch in the main checkout and never touches another worktree. The start point is a hash, so
`from <base> @ <hash>` stays true if the base moves in between, and the new branch tracks nothing.
The repo values are `.kit.json` "repo".main_branch, .remote, .worktrees_dir and .tools_dir.

The mode is what the caller said, never what the repository holds: the default refuses a branch
that exists, `existing` refuses one that does not.

The landing exception: while the kit landing lock (kitlib.records.landing, the file
in the git common dir that `gate.py land` holds from its merge to its push or undo) is held, the
base may hold unpushed merges, so a new branch starts at the pushed `<remote>/<base>` (by hash)
and the output names the base's unpushed commits the branch does NOT contain. A lock file that is
there but is not a lock record counts as held. The lock is only read. The one refusal under a held
lock: `<remote>/<base>` does not resolve. With the lock free the base must equal the remote; the
refs are read once more before refusing (`choose_base`), for a lander that pushed and released
between the ref read and the lock read. A non-main `--base` that exists locally with no pushed
branch is refused with the lock held or free; that refusal ends with a `route` line (route_line)
naming tools/redfirst.py, so a wait is not mistaken for the way out.

The behind exception: with the lock free and local `<base>` an ancestor of `<remote>/<base>` but not
equal to it (a pure fast-forward: no commit of its own), the new branch starts at `<remote>/<base>`
(by hash) and one `base` line says local `<base>` is N commit(s) behind. Local `<base>` is not
updated. A local base with a commit the remote lacks (ahead or diverged) is still refused.

The wait (only when `.kit.json` "landing".in_flight is set; kitlib.records.inflight): before the
fetch, a landing in flight is waited for, up to its wait_s, so a landing that pushes meanwhile is
in the new branch; at the bound, or with `wait` False (--no-wait), a line says so and the work
goes on. wait_s counts from just before the first look, and each look by command gets only what
is left of it. With in_flight.command set and local `<base>` != `<remote>/<base>` after the fetch,
a command that says "in flight" (or cannot tell) also takes the landing exception above, as a held
kit lock does; equal refs never ask it. The local branch checks (exists, --existing) refuse before
any wait.

Names: `<area>/<slug>`, ASCII letters, digits, `_`, `.` and `-`, each part starting with a letter
or digit; the area is lowercase-first, and so is a NEW slug (`--existing` also takes a slug
starting with a capital). A slug that differs only in case from a folder already in the worktrees
folder, or a branch that differs only in case from a local branch, is refused. Branch existence
is an exact-spelling match.

Exit codes: OK 0; REFUSED 1 (nothing made, the lines say why); CONFIG 2 (the kit configuration
was refused).
"""
import collections
import os
import re
import time

from kitlib.config import ConfigError, load
from kitlib.git import cmd
from kitlib.git import refs as gitrefs
from kitlib.records import inflight, landing
from kitlib.host.registry import RegistryError
from kitlib.worktree import hostdirs

OK, REFUSED, CONFIG = 0, 1, 2

NEW_BRANCH = re.compile(r'^[a-z0-9][\w.-]*/[a-z0-9][\w.-]*$', re.ASCII)
EXISTING_BRANCH = re.compile(r'^[a-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$', re.ASCII)
HELD_NO_REMOTE = 'did not resolve'   # choose_base's one refusal under a held lock
REFUSAL = 'Stop and tell the coordinator; do not pull or reset yourself.'
FETCH_TIMEOUT = 300                  # seconds

Outcome = collections.namedtuple('Outcome', 'code lines')


class _Unread(Exception):
    """A base ref git gave no answer for (create's refs())."""


def branch_ok(branch, existing):
    """True when `branch` is a name this mode takes."""
    return bool((EXISTING_BRANCH if existing else NEW_BRANCH).match(branch or ''))


def git(cwd, *args, timeout=cmd.GIT_TIMEOUT):
    """`(rc, stdout text stripped, stderr)`; rc None when git did not run."""
    r = cmd.call(list(args), cwd, timeout=timeout)
    return r.rc, cmd.text(r.out or b'').strip(), (r.err or '').strip()


def local_branches(cwd):
    """Every refs/heads/ name as git enumerates it (loose and packed, in the case it was made in)."""
    out = git(cwd, 'for-each-ref', '--format=%(refname)', 'refs/heads/')[1]
    return [l[len('refs/heads/'):] for l in out.splitlines() if l.startswith('refs/heads/')]


def branch_exists(cwd, branch):
    """True when refs/heads/<branch> is a local branch spelled exactly so. Not `rev-parse --verify`:
    a loose ref is a file, so on Windows that resolves `codex/abc` to `codex/ABC`'s tip."""
    return branch in local_branches(cwd)


def case_twin(names, name):
    """A name in `names` that equals `name` but for case, or None."""
    for other in names:
        if other != name and other.casefold() == name.casefold():
            return other
    return None


def lock_line(lock):
    """`(True, line)` when the landing lock is held (a file that is no lock record is held, by
    nobody we can name); `(False, why free)` otherwise. Reads only."""
    try:
        record = lock.read()
    except landing.LockError as e:
        return True, 'the landing lock %s cannot be read (%s), treated as held' % (lock.path, e)
    if record is None:
        return False, 'nobody holds the landing lock (%s)' % lock.path
    return True, 'the landing lock %s is held: %s' % (lock.path, landing.describe(record))


def unpushed_commits(cwd, m, om):
    """The base's first-parent commits not on the pushed commit, as short hashes; '(none)'; or
    'unknown (<reason>)' when they cannot be read - never '(none)' for a read that failed."""
    if not m:
        return 'unknown (the base did not resolve)'
    rc, out, err = git(cwd, 'rev-list', '--first-parent', '%s..%s' % (om, m))
    if rc != 0:
        return 'unknown (git rev-list %s..%s failed: %s)' % (om[:7], m[:7], err or 'exit %s' % rc)
    return ' '.join(h[:7] for h in out.split()) or '(none)'


def choose_base(m, om, held, reread, free_why=''):
    """`(start, m, om, None)`, or `(None, m, om, why)` to refuse. Held: start at pushed `om`
    (refuse HELD_NO_REMOTE when it did not resolve). Free: start at `m` when it equals `om`;
    else `reread()` -> (m, om) ONCE and decide again; still different refuses with `free_why`."""
    if held:
        if not om:
            return None, m, om, HELD_NO_REMOTE
        return om, m, om, None
    if m and om and m == om:
        return m, m, om, None
    m, om = reread()
    if m and om and m == om:
        return m, m, om, None
    if not m or not om:
        return None, m, om, 'the base or its remote branch did not resolve'
    return None, m, om, (free_why or 'nobody holds the landing lock') + \
        ', so the difference is no landing'


def route_line(base, pushed, tools, remote, main_branch):
    """The `route` line ending a refusal of a non-main base that exists locally but has no pushed
    branch: that refusal holds with the landing lock free or held, so waiting does not change it."""
    return ('route     %s has no pushed %s, so this is refused with or without a landing; waiting '
            'will not change it. For red-first evidence make no worktree: `python %s/redfirst.py '
            '-C <your worktree> --tests <test file>` runs the committed tests at the pre-change '
            'commit in a tree of its own (`--base <commit>` when that is not the merge-base with '
            '%s/%s); it needs no pushed branch and waits for no landing. Otherwise push %s or tell '
            'the coordinator.' % (base, pushed, tools, remote, main_branch, base))


def behind_count(cwd, m, om):
    """N when `m` is a strict ancestor of `om` (a pure fast-forward away: N commits behind), else
    None (equal, ahead, diverged, or git failed - kitlib.git.refs.is_ancestor gave no answer)."""
    if not m or not om or m == om:
        return None
    if gitrefs.is_ancestor(m, om, cwd)[0] is not True:
        return None
    rc, out, _ = git(cwd, 'rev-list', '--count', '%s..%s' % (m, om))
    return int(out) if rc == 0 and out.isdigit() else None


def branch_name(base, remote):
    """`base` as a local branch name: a `<remote>/<name>` or `refs/remotes/<remote>/<name>` the
    caller passed names the branch `<name>` (the remote's copy is compared anyway), and
    `refs/heads/<name>` is `<name>`. Anything else is returned as given."""
    for prefix in ('refs/remotes/%s/' % remote, '%s/' % remote, 'refs/heads/'):
        if base.startswith(prefix) and len(base) > len(prefix):
            return base[len(prefix):]
    return base


def main_checkout_of(common):
    """`<main>/.git` (git's `--git-common-dir`) -> `<main>` in its one real spelling: abspath for
    one kind of separator, realpath so a checkout reached through a junction registers every new
    worktree under the real path."""
    return os.path.realpath(os.path.abspath(os.path.dirname(common)))


def create(root, branch, existing=False, base=None, wait=True, say=None, sleep=time.sleep,
           clock=time.monotonic, host=None):
    """Outcome(code, lines) of making the worktree for `branch` in the repository `root` is in.
    `wait` False is --no-wait; `say(line)` gets the lines said while waiting for a landing in
    flight (None: they lead the Outcome's lines); `sleep` and `clock` are time's, for tests.
    `host` is a hosts.json name whose folder the worktree goes in (kitlib.worktree.hostdirs); None
    is "repo".worktrees_dir, as before hosts."""
    rc, common, err = git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if rc != 0 or not common:
        return Outcome(REFUSED, ['refused: %s is not a git checkout (no common dir: %s).'
                                 % (root, err or 'git printed nothing')])
    main = main_checkout_of(common)
    try:
        cfg = load(main)
        lanes = tuple(cfg.section('landing').get('lanes') or ())
        lock = landing.LandingLock.for_config(main, cfg, lanes=lanes)
        reader = inflight.reader(main, cfg, lock)
    except (ConfigError, landing.LockError) as e:
        return Outcome(CONFIG, ['new_worktree: refused: kit configuration: %s' % e])
    remote = cfg.remote
    base = branch_name(base, remote) if base else cfg.main_branch
    tools = cfg.tools_dir.replace('\\', '/').rstrip('/')
    slug = branch.split('/', 1)[1]
    try:
        folder = cfg.repo['worktrees_dir'] if host is None else hostdirs.folder(cfg, host)
    except RegistryError as error:
        return Outcome(CONFIG, ['new_worktree: refused: %s' % error])
    parent = os.path.normpath(os.path.join(main, folder))
    if folder != cfg.repo['worktrees_dir']:
        # A host's own folder never grants a destination outside this main checkout; the
        # repository's worktrees_dir keeps the rules it had before hosts.
        real_main, real_parent = os.path.realpath(main), os.path.realpath(parent)
        try:
            inside = os.path.commonpath((real_main, real_parent)) == real_main and real_parent != real_main
        except ValueError:
            inside = False
        if not inside or os.path.normcase(real_parent) != os.path.normcase(os.path.abspath(parent)):
            return Outcome(CONFIG, ['new_worktree: refused: host worktree folder must stay inside '
                                    'the main checkout without a redirect'])
    path = os.path.join(parent, slug)
    out = []

    def refuse(*lines):
        return Outcome(REFUSED, out + list(lines))

    twin = case_twin(os.listdir(parent) if os.path.isdir(parent) else [], slug)
    if twin is not None:
        return refuse('refused   %s differs only in case from the existing folder %s; pick another '
                      'slug (`python %s/worktree_remove.py --check` says what that one is)'
                      % (path, os.path.join(parent, twin), tools))
    twin = case_twin(local_branches(main), branch)
    if twin is not None:
        return refuse('refused   branch %s differs only in case from the existing branch %s; on '
                      'Windows git can read the one as the other. Name the branch exactly as it is '
                      'spelled, or pick another name.' % (branch, twin))
    if os.path.exists(path):
        if existing:
            return refuse('exists already: %s (the commonest cause: a leftover directory from a '
                          '`git worktree remove` that failed - `python %s/worktree_remove.py --check` '
                          'says what it is and what holds it; never reuse it live)' % (path, tools))
        return refuse('exists already: %s (pick another slug, or see what it is: `python '
                      '%s/worktree_remove.py --check`; never reuse a name that failed to delete)'
                      % (path, tools))
    there = branch_exists(main, branch)       # local only: refuse before any wait or fetch
    if existing and not there:
        return refuse('no such branch: %s. --existing resumes a branch that is already there; '
                      'drop it to create one.' % branch)
    if there and not existing:
        return refuse('branch exists already: %s @ %s. To resume it: --existing. To start a new '
                      'branch, pick a name that is free.'
                      % (branch, git(main, 'rev-parse', 'refs/heads/' + branch)[1][:7]))
    began = clock()                           # wait_s counts from here, the looks included
    if reader is not None:                    # before the fetch: a landing that pushes is fetched
        out.extend(inflight.settle(reader, wait, say, sleep, clock, start=began)[1])
    rc, _, err = git(main, 'fetch', remote, timeout=FETCH_TIMEOUT)
    if rc != 0:
        return refuse('fetch failed: %s' % (err or 'exit %s' % rc))
    pushed = '%s/%s' % (remote, base)

    def refs():
        """(m, om): each a commit, or '' when the ref names none; _Unread when git gives no answer.

        GOTCHA: KIT-GIT-EXIT1-ANSWERS-2 - this asked `rev-parse --verify --quiet`, whose "no such
        ref" is exit 1 with no output, a killed git's shape (gitrefs.KILLED_SHAPE): a kill read as
        "did not resolve". resolve(known=True) tells the two apart; a str or a raise, never None,
        so `m[:7]` below always has a string."""
        got = []
        for rev in (base, 'refs/remotes/%s' % pushed):
            sha, why = gitrefs.resolve(rev, main, known=True)
            if sha is None:
                raise _Unread('%s could not be read (%s)' % (rev, why))
            got.append(sha)
        return got[0], got[1]
    try:
        m, om = refs()
        held, line = lock_line(lock)          # after the refs: see choose_base
        seen = 'the landing lock is held'
        if not held and reader is not None and reader.kind == 'command' and m != om:
            # the repo's own lock: the landing exception too. Equal refs need no exception.
            held, said = reader.look(inflight.time_left(reader, began, clock))
            if held:
                line, seen = 'landing.in_flight.command: %s' % said, 'a landing is in flight'
            else:
                line = '%s; landing.in_flight.command: %s' % (line, said)
        start, m, om, why = choose_base(m, om, held, refs, free_why=line)
    except _Unread as e:
        return refuse('refused   %s; nothing was created. %s' % (e, REFUSAL))
    if held:
        out.append('landing   in progress: %s' % line)
    behind = None if held or start is not None else behind_count(main, m, om)
    if behind is not None:
        start = om
        out.append('base      local %s is %d commit(s) behind %s; this branch starts at %s %s and '
                   'local %s is not updated' % (base, behind, pushed, pushed, om[:7], base))
    route = [route_line(base, pushed, tools, remote, cfg.main_branch)] \
        if start is None and m and not om and base != cfg.main_branch else []
    if start is None and held:
        return refuse('refused   %s, but %s %s: no pushed commit to base on. %s'
                      % (seen, pushed, why, REFUSAL), *route)
    if start is None:
        return refuse('%s %s != %s %s. %s' % (base, m[:7] or '?', pushed, om[:7] or '?', REFUSAL),
                      '  not a landing in progress: %s' % why, *route)
    if held:
        shas = unpushed_commits(main, m, om)
        if not existing:
            out.append('base      this branch is based on pushed %s %s and does NOT contain '
                       "%s's unpushed commit(s) %s" % (pushed, om[:7], base, shas))
        elif shas != '(none)':
            out.append("unpushed  %s's commit(s) %s, not counted below" % (base, shas))

    add = (path, branch) if existing else (path, '-b', branch, start)
    rc, said, err = git(main, 'worktree', 'add', *add, timeout=FETCH_TIMEOUT)
    if rc != 0:
        return refuse('worktree add failed: %s' % (err or said or 'exit %s' % rc))
    out.append('worktree  %s' % path)
    if existing:
        tip = git(main, 'rev-parse', 'refs/heads/' + branch)[1]
        missing = git(main, 'rev-list', '--count', '%s..%s' % (tip, start))[1]
        out.append('branch    %s @ %s, resumed (%s commit(s) of %s not in it)'
                   % (branch, tip[:7], missing,
                      ('pushed ' + pushed) if held or behind is not None else base))
    elif held or behind is not None:
        out.append('branch    %s from pushed %s @ %s (not %s: %s)'
                   % (branch, pushed, om[:7], base,
                      'a landing is in progress' if held else 'it is behind'))
    else:
        out.append('branch    %s from %s @ %s' % (branch, base, m[:7]))
    out.append('rules     absolute paths and `git -C "%s"`; do not cd into it in Bash; commit by '
               'explicit path; you do not land' % path)
    out.append('next      python "%s" (run from the new worktree)'
               % os.path.normpath(os.path.join(path, tools, 'preflight.py')))
    return Outcome(OK, out)
