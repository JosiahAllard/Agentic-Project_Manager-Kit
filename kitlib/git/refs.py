"""Branch, commit and ancestry queries. Read-only: nothing here moves a ref or touches a tree.

Every answer is `(value, None)` or `(None, why)`. A rev a caller passes is refused before git
sees it when it could be read as an option (a leading `-`) or carries a line break or NUL.
"""
import os
import re

from . import cache, cmd

SHA_RE = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')


def bad_rev(rev):
    """Why `rev` is refused, or None. A leading `-` would be read as an option."""
    if not isinstance(rev, str) or not rev:
        return 'not a rev: %r' % (rev,)
    if rev.startswith('-') or any(c in rev for c in '\n\r\0'):
        return 'not a rev: %r' % rev
    return None


def is_sha(text):
    return isinstance(text, str) and bool(SHA_RE.fullmatch(text))


NO_SUCH_REV = 'fatal: Needed a single revision'      # `rev-parse --verify`'s whole stderr for one


def resolve(rev, cwd, peel='commit', known=False):
    """`(full hash, None)` of `rev` peeled to `peel` (commit, tree; None: no peel), or `(None, why)`.

    `known=False` (the default): `(None, '<rev> does not name a <peel>')` when git answers that
    `rev` names nothing, and `(None, why)` with another why when git gives no answer. `known=True`:
    `('', None)` for the first, so a caller can tell "no such rev" from "git cannot tell" without
    reading the why.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS - `rev-parse --verify --quiet` said "no such rev" as exit 1 with
    no output, a killed git's shape (KILLED_SHAPE), so a kill read as "no such rev". The question
    is asked without --quiet: "no such rev" is exit 128 with NO_SUCH_REV alone on stderr, and
    anything else that is not exit 0 with a hash is no answer.

    In a test process (kitlib.git.cache.enable) a full hash's answer is kept: an object never
    changes what it is. Only a found answer of the same length is kept (a 40-digit name in a sha256
    repository is an abbreviation), none while a test fakes git (cmd.genuine), and any other rev
    (a ref, HEAD, an abbreviation) is asked of git every time."""
    why = bad_rev(rev)
    if why:
        return None, why
    spec = '%s^{%s}' % (rev, peel) if peel else rev
    key = (('resolve', os.path.normcase(os.path.abspath(cwd or os.curdir)), spec)
           if is_sha(rev) and cmd.genuine() else None)
    kept = cache.get(key)
    if kept:
        return kept, None
    gen = cache.generation()
    r = cmd.call(['rev-parse', '--verify', spec], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    if r.rc == 0 and out:
        if len(out) == len(rev):
            cache.put(key, gen, out)
        return out, None
    if r.rc == 128 and not out and r.err == NO_SUCH_REV:
        return ('', None) if known else (None, '%s does not name a %s' % (rev, peel or 'object'))
    return None, _failed('rev-parse --verify %s' % spec, r, out)


def head(cwd):
    """`(HEAD's commit, None)` or `(None, why)`."""
    return resolve('HEAD', cwd)


def tree_of(rev, cwd):
    """`(the tree hash of rev, None)` or `(None, why)`."""
    return resolve(rev, cwd, peel='tree')


def current_branch(cwd):
    """`(branch name, None)`; `('', None)` on a detached HEAD; `(None, why)` when git fails or
    HEAD points outside refs/heads.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS - `symbolic-ref --quiet` says "detached" as exit 1 with no
    output, which is also what a git ended from outside leaves (KILLED_SHAPE), so a killed git
    read as a detached HEAD. `branch --show-current` answers with exit 0 either way (the name,
    or nothing when detached; an unborn branch still has its name), so anything but exit 0 is no
    answer."""
    r = cmd.call(['branch', '--show-current'], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    if r.rc != 0:
        return None, _failed('branch --show-current', r, out)
    return out, None


def ref_commit(ref, cwd):
    """`(commit, None)` the full ref `ref` (`refs/...`) points at, peeled to a commit; `('', None)`
    when there is no such ref; `(None, why)` when git cannot tell or the ref names no commit.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS - `rev-parse --verify --quiet <ref>` says "no such ref" as exit
    1 with no output, a killed git's shape (KILLED_SHAPE), so a kill read as "no ref". The
    question is asked as `for-each-ref <ref>`, which exits 0 whether or not the ref exists: no
    line with exactly that refname is "no ref", and anything but exit 0 is no answer.

    GOTCHA: for-each-ref skips a broken loose ref (a file holding garbage) with exit 0, no line and
    "warning: ignoring broken ref <ref>" on stderr, the shape of "no ref"; `rev-parse --verify`
    failed on one instead. So no line plus anything on stderr is no answer, never "no ref"."""
    why = bad_rev(ref)
    if why:
        return None, why
    if not ref.startswith('refs/'):
        return None, 'not a full ref name: %r' % ref
    what = 'for-each-ref %s' % ref
    r = cmd.call(['for-each-ref', '--format=%(objectname) %(objecttype) %(refname)', ref], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    if r.rc != 0:
        return None, _failed(what, r, out)
    for line in out.splitlines():
        sha, _, rest = line.partition(' ')
        kind, _, name = rest.partition(' ')
        if name != ref:
            continue                     # a pattern also matches the refs below it
        if not is_sha(sha):
            return None, 'git %s printed %r, not a ref line' % (what, line[:80])
        if kind == 'commit':
            return sha, None
        full, why = resolve(sha, cwd)
        if not full:
            return None, '%s points at a %s, not a commit: %s' % (ref, kind or 'object', why)
        return full, None
    if r.err:
        return None, 'git %s printed no line for it and said: %s' % (what, r.err[:200])
    return '', None


def branch_tip(name, cwd):
    """`(commit of refs/heads/<name>, None)`; `(None, why)` when there is no such branch."""
    return resolve('refs/heads/%s' % name, cwd)


def remote_tip(remote, name, cwd):
    """`(commit of refs/remotes/<remote>/<name>, None)` as last fetched; no network."""
    return resolve('refs/remotes/%s/%s' % (remote, name), cwd)


def default_base(cwd, remote, main):
    """The one default-base rule every kit tool measures against when the caller passes no base
    (docs/gate.md "Default base"): `(rev, note)`.

    `rev` is `refs/remotes/<remote>/<main>` (as last fetched) when it resolves, else the plain
    `<main>` (the local branch, which may not exist either: the caller's own resolve then says so).
    A local `<main>` lags whenever nobody pulled into the main checkout, and the branches under
    review are cut from the remote's, so measuring against the local one counts the remote's newer
    commits as the branch's work.

    `note` is None when the local branch is absent or at the same commit; else one line naming
    both commits, how far apart they are, and the ref used. A caller prints it."""
    tracking = 'refs/remotes/%s/%s' % (remote, main)
    remote_sha = resolve(tracking, cwd)[0] if remote and not bad_rev(remote) else None
    local_sha = resolve('refs/heads/%s' % main, cwd)[0]
    if not remote_sha:
        return main, None
    if not local_sha or local_sha == remote_sha:
        return tracking, None
    r = cmd.call(['rev-list', '--left-right', '--count', '%s...%s' % (local_sha, remote_sha)], cwd)
    counts = cmd.text(r.out).split() if r.rc == 0 else []
    if len(counts) == 2 and counts[0] == '0':
        apart = '%s behind' % counts[1]
    elif len(counts) == 2 and counts[1] == '0':
        apart = '%s ahead of' % counts[0]
    elif len(counts) == 2:
        apart = '%s ahead of and %s behind' % tuple(counts)
    else:
        apart = 'not at'
    return tracking, 'base: local %s %s is %s %s/%s %s; measuring against %s' % (
        main, local_sha[:7], apart, remote, main, remote_sha[:7], tracking)


def ls_remote(remote, ref, cwd, timeout=60):
    """`(commit the remote's `ref` points at, None)`, `('', None)` when the remote has no such ref,
    or `(None, why)`. Asks the remote (network); read-only."""
    for r in (remote, ref):
        why = bad_rev(r)
        if why:
            return None, why
    out, why = cmd.run_text(['ls-remote', remote, ref], cwd, timeout=timeout)
    if out is None:
        return None, why
    full = ref if ref.startswith('refs/') else 'refs/heads/%s' % ref
    for line in out.splitlines():
        sha, _, name = line.partition('\t')
        if name == full:
            return sha, None
    return '', None


KILLED_SHAPE = ('exit 1 with no output is also what a git ended from outside leaves (a stale '
                '`taskkill /PID <n> /T /F` that reached a reused pid, kitlib.proc.shield GOTCHA)')


def is_ancestor(ancestor, descendant, cwd):
    """`(True/False, None)`: is `ancestor` an ancestor of (or equal to) `descendant`; `(None, why)`
    when git cannot tell (an unknown rev, a non-commit, a failure, a git ended from outside).

    GOTCHA: KIT-TEST-GRANDCHILD-SHIELD - `merge-base --is-ancestor` says NO as exit 1 with no
    output, and a git ended from outside (KILLED_SHAPE) exits the same way, so a killed git read as
    a NO nobody gave. The question is asked as `rev-list -n 1 <descendant>..<ancestor>` instead:
    exit 0 with no output is YES, exit 0 with one commit is NO, and anything else - a killed git's
    exit 1 included - is no answer. Both ends are peeled with ^{commit}, so a tree or blob is
    refused as merge-base refused it (rev-list alone would answer YES for one).

    GOTCHA: rev-list's YES is exact, its NO is not. Its limited walk stops a few commits (SLOP)
    after only uninteresting ones are left, so with skewed commit dates it can print a commit that
    `descendant` does reach (measured 2026-10-10: a root dated after 8 commits built on it, no
    commit-graph). So every NO is confirmed (`_confirm_no`) by `merge-base --independent`, whose
    walk is exact and which prints at least one commit for any answer: a NO that cannot be
    confirmed is no answer, never a NO."""
    for rev in (ancestor, descendant):
        why = bad_rev(rev)
        if why:
            return None, why
    spec = '%s^{commit}..%s^{commit}' % (descendant, ancestor)
    r = cmd.call(['rev-list', '-n', '1', spec, '--'], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    if r.rc == 0 and not out:
        return True, None
    if r.rc == 0 and is_sha(out):
        return _confirm_no(ancestor, descendant, cwd)
    if r.rc == 0:
        return None, 'git rev-list -n 1 %s printed %r, not one commit or nothing' % (spec, out[:80])
    return None, _failed('rev-list -n 1 %s' % spec, r, out)


def _failed(what, r, out):
    if r.err or out:
        return 'git %s exited %s: %s' % (what, r.rc, r.err or out[:80])
    return 'git %s exited %s and printed nothing%s' % (
        what, r.rc, ': ' + KILLED_SHAPE if r.rc == 1 else '')


def _confirm_no(ancestor, descendant, cwd):
    """is_ancestor's answer once rev-list said NO: `merge-base --independent <ancestor's hash>
    <descendant>` prints the given commits no other given commit reaches, so `ancestor`'s hash is
    among them exactly when `descendant` does not reach it (NO), and absent when it does (YES)."""
    full, why = resolve(ancestor, cwd)
    if not full:
        return None, 'confirming a NO: %s' % why
    what = 'merge-base --independent %s %s^{commit}' % (full, descendant)
    r = cmd.call(['merge-base', '--independent', full, '%s^{commit}' % descendant], cwd)
    if r.rc is None:
        return None, 'confirming a NO: %s' % r.err
    out = cmd.text(r.out).strip()
    lines = out.split()
    if r.rc != 0 or not lines or len(lines) > 2 or not all(is_sha(l) for l in lines):
        why = (_failed(what, r, out) if r.rc != 0 else
               'git %s printed %r, not one or two commits' % (what, out[:200]))
        return None, 'confirming a NO: %s' % why
    return full not in lines, None


def is_fast_forward(old, new, cwd):
    """`(True/False, None)`: moving a ref from `old` to `new` is a fast-forward. An `old` of all
    zeros (a ref being created) is a fast-forward; a `new` of all zeros (a deletion) is not."""
    if old and set(old) == {'0'}:
        return True, None
    if new and set(new) == {'0'}:
        return False, None
    return is_ancestor(old, new, cwd)


def merge_base(a, b, cwd):
    """`(the best common ancestor, None)`, `('', None)` when there is none, or `(None, why)`.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS - `merge-base` says "none" as exit 1 with no output, a killed
    git's shape (KILLED_SHAPE). So that answer is confirmed (_confirm_no_base) before it is
    given; one that cannot be confirmed is no answer."""
    for rev in (a, b):
        why = bad_rev(rev)
        if why:
            return None, why
    what = 'merge-base %s %s' % (a, b)
    r = cmd.call(['merge-base', a, b], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    if r.rc == 0 and is_sha(out):
        return out, None
    if r.rc == 0:
        return None, 'git %s printed %r, not one commit' % (what, out[:80])
    if r.rc == 1 and not out and not r.err:
        return _confirm_no_base(a, b, cwd)
    return None, _failed(what, r, out)


def _roots(rev, cwd):
    """`(set of the root commits `rev` reaches, None)` or `(None, why)`."""
    what = 'rev-list --max-parents=0 %s^{commit}' % rev
    r = cmd.call(['rev-list', '--max-parents=0', '%s^{commit}' % rev, '--'], cwd)
    if r.rc is None:
        return None, r.err
    out = cmd.text(r.out).strip()
    lines = out.split()
    if r.rc != 0:
        return None, _failed(what, r, out)
    if not lines or not all(is_sha(l) for l in lines):
        return None, 'git %s printed %r, not root commits' % (what, out[:200])
    return set(lines), None


def _confirm_no_base(a, b, cwd):
    """merge_base's answer once git said "none": two commits share an ancestor exactly when they
    share a root commit (a shared ancestor's roots are reached from both; a shared root is a
    shared ancestor), so disjoint `rev-list --max-parents=0` root sets confirm there is none and
    anything else is no answer."""
    sets = []
    for rev in (a, b):
        roots, why = _roots(rev, cwd)
        if roots is None:
            return None, 'confirming no merge base: %s' % why
        sets.append(roots)
    shared = sets[0] & sets[1]
    if shared:
        return None, ('confirming no merge base: git merge-base %s %s exited 1 and printed '
                      'nothing, but both reach the root commit %s: %s'
                      % (a, b, min(shared)[:10], KILLED_SHAPE))
    return '', None


def merge_in_progress(cwd):
    """`(True/False, None)`: the work tree at `cwd` has a MERGE_HEAD (per work tree, so a merge
    in the main checkout is seen there and not in a linked worktree), or `(None, why)`."""
    out, why = cmd.run_text(['rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'], cwd)
    if not out:
        return None, why or 'git rev-parse --git-path printed nothing'
    return os.path.isfile(os.path.normpath(out)), None


def parse_porcelain_z(out):
    """[(xy, path)] from `git status --porcelain -z` text. A rename or copy entry (R/C in either
    column) is followed by one more field, its original path, which is skipped."""
    entries, fields, i = [], out.split('\0'), 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy, p = f[:2], f[3:]
        entries.append((xy, p))
        if 'R' in xy or 'C' in xy:
            i += 1
    return entries


def status(cwd, untracked='all'):
    """`([(xy, path)], None)` for the work tree at `cwd` (`??` marks untracked; ignored files are
    left out), or `(None, why)`. `untracked` is git's --untracked-files value."""
    data, why = cmd.run(['status', '--porcelain', '-z', '--untracked-files=%s' % untracked], cwd)
    if data is None:
        return None, why
    return parse_porcelain_z(data.decode('utf-8', 'replace')), None


def is_clean(cwd):
    """`(True/False, None)`: no tracked change and no untracked, non-ignored file; or `(None, why)`."""
    entries, why = status(cwd)
    if entries is None:
        return None, why
    return not entries, None
