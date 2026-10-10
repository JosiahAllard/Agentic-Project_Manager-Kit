"""Branch cleanup: a landed branch goes - its remote branch, its worktree and its local branch
(docs/gate.md "Branch cleanup").

`after_landing()` runs in a landing (kitlib.gate.land.Lander) once the push is verified and the
landing lock released; `sweep()` (`land.py --sweep-landed [--apply]`) applies the same rule once to
every branch main already contains. Nothing here prints. For each branch, three parts, each judged
on its own:

  remote branch   deleted when main contains its tip: `git push --force-with-lease=<ref>:<tip>
                  <remote> :<ref>`. The lease is a compare-and-swap, not a force: the remote
                  deletes the branch only while it still points at the tip read a moment before,
                  so a branch someone pushed to since is refused and kept. Then ls-remote must
                  show it gone.
  worktree        removed by kitlib.worktree.remove (the `worktree_remove.py` checks, in order:
                  workstream folder, landed in <remote>/<main>, nothing holding it, no
                  uncommitted, untracked or unlisted ignored file, no link inside; then `git
                  worktree remove`, never forced), which then deletes the local branch with
                  `git branch -d`
  local branch    with no worktree: `git branch -d`, never -D

Never forced. A tip main does not contain, a worktree with work in it, a worktree in use and a
refusal from git are kept, and the reason is named; the landing stands whatever is kept.

One branch's result (the result record's `cleanup`, the landing's facts['cleanup'][branch]):

  remote_branch   deleted | absent | kept | skipped (--keep-branch) | would-delete (a dry run)
  worktree        removed | absent | kept | skipped | would-remove
  worktree_path   the worktree's folder, or null
  local_branch    deleted | absent | kept | skipped | would-delete
  kept            why each kept part was kept, one line each
"""
import os

from kitlib import gate as G
from kitlib.gate import steps as S
from kitlib.git import cmd, refs
from kitlib.worktree import remove
from kitlib.worktree import sweep as wsweep
from kitlib.worktree.holders import canon

PUSH_TIMEOUT = 300      # seconds
DELETED, REMOVED, ABSENT, KEPT, SKIPPED = 'deleted', 'removed', 'absent', 'kept', 'skipped'
WOULD_DELETE, WOULD_REMOVE = 'would-delete', 'would-remove'
HEAD = 'cleanup   '
KEPT_LINE = '  kept     '


def sweep_command(root, apply=True):
    """The command that runs the sweep for the repository at `root`."""
    return 'python "%s" -C "%s" --sweep-landed%s' % (
        os.path.join(S.KIT_TOOLS, 'land.py'), root, ' --apply' if apply else '')


def entry(remote=ABSENT, worktree=ABSENT, path=None, local=ABSENT, kept=None):
    return {'remote_branch': remote, 'worktree': worktree, 'worktree_path': path,
            'local_branch': local, 'kept': list(kept or [])}


def skipped(branch):
    """--keep-branch: `(entry, lines)` touching nothing."""
    return (entry(SKIPPED, SKIPPED, None, SKIPPED),
            [HEAD + '%s: skipped (--keep-branch): the remote branch, the worktree and the local '
             'branch are left as they are' % branch])


def _worktrees(ctx, branch):
    """The registered worktrees (not the main checkout) on `branch`: [path as registered]."""
    main = canon(ctx.main)
    return [e['path'] for e in remove.worktree_entries(remove.worktree_listing(ctx.main))
            if e['branch'] == branch and canon(e['path']) != main]


def _first_refusal(lines):
    for line in lines:
        word = line.split(' ', 1)[0]
        if word in ('refused', 'failed', 'leftover', 'nothing'):
            return line[len(word):].strip()
    return ' '.join(l.strip() for l in lines[:1]) or 'refused'


def _delete_remote(root, remote, branch, sha, git_env):
    """None when `branch` is gone from `remote`, else why it is still there."""
    ref = 'refs/heads/%s' % branch
    r = cmd.call(['push', '--quiet', '--force-with-lease=%s:%s' % (ref, sha), remote, ':' + ref],
                 root, timeout=PUSH_TIMEOUT, env=git_env)
    now, why = refs.ls_remote(remote, ref, root)
    if now == '':
        return None
    if r.rc != 0:
        return 'the remote refused the delete (%s)' % ' '.join((r.err or 'exit %s' % r.rc).split())
    return 'the delete exited 0, and ls-remote shows %s' % (
        'it unreadable (%s)' % why if now is None else 'it at %s' % now[:10])


def clean_branch(ctx, branch, landed, apply=True, git_env=None):
    """One branch: `(entry, lines)`. `ctx` is kitlib.worktree.remove's (the main checkout and its
    settings); `landed(sha)` is `(True, None)` when main contains `sha`, else `(False, why)` or
    `(None, why)`. `apply` False changes nothing and says what would go."""
    remote = ctx.s['remote']
    e = entry()
    lines = []

    def keep(part, why):
        e['kept'].append('%s: %s' % (part, why))

    sha, why = refs.ls_remote(remote, 'refs/heads/%s' % branch, ctx.main)
    if sha is None:
        e['remote_branch'] = KEPT
        keep('remote branch', 'it could not be read (%s)' % why)
    elif sha:
        ok, why = landed(sha)
        if not ok:
            e['remote_branch'] = KEPT
            keep('remote branch', 'at %s, %s' % (sha[:10], why))
        elif not apply:
            e['remote_branch'] = WOULD_DELETE
        else:
            why = _delete_remote(ctx.main, remote, branch, sha, git_env)
            e['remote_branch'] = KEPT if why else DELETED
            if why:
                keep('remote branch', why)

    tip, why = refs.resolve('refs/heads/%s' % branch, ctx.main)
    if tip is None and not why.endswith('does not name a commit'):
        e['local_branch'] = KEPT
        keep('local branch', 'it could not be read (%s)' % why)
    trees = _worktrees(ctx, branch) if tip else []
    if tip:
        ok, why = landed(tip)
        if not ok:
            e['local_branch'] = KEPT
            keep('local branch', 'at %s, %s' % (tip[:10], why))
            for path in trees:
                e['worktree'], e['worktree_path'] = KEPT, os.path.normpath(path)
                keep('worktree %s' % e['worktree_path'], 'its branch is %s' % why)
        elif not apply:
            e['local_branch'] = WOULD_DELETE
            if trees:
                e['worktree'], e['worktree_path'] = WOULD_REMOVE, os.path.normpath(trees[0])
        elif trees and not os.path.isdir(trees[0]):
            e['worktree'], e['worktree_path'] = KEPT, os.path.normpath(trees[0])
            e['local_branch'] = KEPT
            keep('worktree %s' % e['worktree_path'], 'registered, and its folder is gone (`git '
                 'worktree prune` clears the entry)')
            keep('local branch', 'it is checked out in that worktree entry')
        elif trees:
            path = os.path.normpath(trees[0])
            e['worktree_path'] = path
            ctx.out = []
            code = remove._remove(ctx, path, False)
            said, ctx.out = [l for l in ctx.out if l.strip()], []
            gone = not os.path.isdir(path)
            e['worktree'] = REMOVED if gone else KEPT
            if code != remove.OK or not gone:
                why = _first_refusal(said)
                keep('worktree %s' % path, why)
                lines.extend('           ' + l.strip() for l in said if why not in l)
            now, _ = refs.resolve('refs/heads/%s' % branch, ctx.main)
            e['local_branch'] = DELETED if not now else KEPT
            if now and gone:
                keep('local branch', _first_refusal([l for l in said if 'branch -d' in l]
                                                    or said))
            elif now:
                keep('local branch', 'it is checked out in the kept worktree')
        else:
            ctx.out = []
            remove.delete_branch(ctx, branch, False)
            said, ctx.out = list(ctx.out), []
            now, _ = refs.resolve('refs/heads/%s' % branch, ctx.main)
            e['local_branch'] = DELETED if not now else KEPT
            if now:
                keep('local branch', _first_refusal(said))
                lines.extend('           ' + l.strip() for l in said[1:] if l.strip())

    parts = ['remote branch %s' % e['remote_branch'],
             'worktree %s%s' % (e['worktree'], ' (%s)' % e['worktree_path']
                                if e['worktree_path'] else ''),
             'local branch %s' % e['local_branch']]
    text = ', '.join(parts).replace('would-delete', 'would be deleted').replace(
        'would-remove', 'would be removed')
    head = [HEAD + '%s: %s' % (branch, text)]
    return e, head + [KEPT_LINE + k for k in e['kept']] + lines


def after_landing(root, branches, merge, keep_branch=False, git_env=None):
    """After a verified landing of `branches` at the merge `merge` (on the main checkout `root`):
    `(facts, lines)`, facts {branch: entry, or None when its cleanup raised}. An Exception for one
    branch is a WARNING line and the next branch is cleaned; KeyboardInterrupt is raised."""
    facts, lines = {}, []
    if keep_branch:
        for b in branches:
            facts[b], said = skipped(b)
            lines.extend(said)
        return facts, lines
    ctx, refused = remove.open_ctx(root, cwd=root)
    if refused:
        return ({b: None for b in branches},
                ['WARNING: the branch cleanup did not run: %s; run it later with: %s' % (
                    ' '.join(' '.join(refused.lines).split()), sweep_command(root))])

    def landed(sha):
        ok, why = refs.is_ancestor(sha, merge, root)
        if ok is None:
            return None, 'whether main contains it is unknown (%s)' % why
        return ok, None if ok else 'not in %s' % ctx.s['base']

    for b in branches:
        try:
            facts[b], said = clean_branch(ctx, b, landed, git_env=git_env)
            lines.extend(said)
        except Exception as e:      # noqa: BLE001 - the landing stands; the sweep finishes it
            facts[b] = None
            lines.append('WARNING: %s landed, and its branch cleanup stopped (%s: %s); finish it '
                         'with: %s' % (b, type(e).__name__, e, sweep_command(root)))
    return facts, lines


def _heads(ctx, git_env):
    """`({branch: sha} local, {branch: sha} on the remote, None)` or `(None, None, why)`."""
    out, why = cmd.run_text(['for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads/'],
                            ctx.main, env=git_env)
    if out is None:
        return None, None, 'git for-each-ref: %s' % why
    local = {}
    for line in out.splitlines():
        sha, _, ref = line.partition(' ')
        local[ref[len('refs/heads/'):]] = sha
    out, why = cmd.run_text(['ls-remote', '--heads', ctx.s['remote']], ctx.main, env=git_env)
    if out is None:
        return None, None, 'git ls-remote --heads %s: %s' % (ctx.s['remote'], why)
    remote = {}
    for line in out.splitlines():
        sha, _, ref = line.partition('\t')
        if ref.startswith('refs/heads/'):
            remote[ref[len('refs/heads/'):]] = sha
    return local, remote, None


def sweep(top, apply=False, git_env=None):
    """`land.py --sweep-landed`: the cleanup's rule for every branch (local or on the remote) whose
    tip main already contains as work of its own: in <remote>/<main> after a fetch, and off its
    first-parent chain (a branch made from main with no commit of its own is never one). A dry run
    (default) changes nothing and names what would go. OK whatever is kept; BUSY while a landing
    holds the landing lock; REFUSED when the repository cannot be read."""
    ctx, refused = remove.open_ctx(top, cwd=top)
    if refused:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % ' '.join(refused.lines)])
    held = wsweep.landing_hold(ctx)
    if held:
        return G.Outcome(G.BUSY, ['BUSY: %s; nothing was changed' % held])
    why = wsweep.fetch_base(ctx)
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % why])
    base, why = remove.base_commit(ctx)
    got = None
    if base:
        got, why = remove.ancestry(ctx, base)
    local, remote, why2 = _heads(ctx, git_env) if got else (None, None, None)
    if not got or local is None:
        return G.Outcome(G.REFUSED, ['REFUSED: %s; nothing was changed' % (why or why2)])
    chain, ancestors = got
    main = ctx.s['base']

    def landed(sha):
        if sha in chain:
            return False, 'it has no commit of its own (it is on %s\'s first-parent line)' % main
        if sha in ancestors:
            return True, None
        return False, 'not in %s' % main

    current, _ = refs.current_branch(ctx.main)
    names = sorted(b for b in set(local) | set(remote)
                   if b != main and b != current
                   and any(landed(t)[0] for t in (local.get(b), remote.get(b)) if t))
    lines = ['sweep     %s: %d branch(es) main contains (%s %s)' % (
        'cleaning' if apply else 'dry run', len(names), ctx.s['upstream'], base[:10])]
    for b in names:
        try:
            _, said = clean_branch(ctx, b, landed, apply=apply, git_env=git_env)
        except Exception as e:      # noqa: BLE001 - one branch never stops the others
            said = ['WARNING: %s: the cleanup stopped (%s: %s)' % (b, type(e).__name__, e)]
        lines.extend(said)
    if apply:
        lines.append('swept     %d branch(es); whatever was kept is named above' % len(names))
    else:
        lines.append('dry run   nothing was changed; to clean up the branches named above, run: %s'
                     % sweep_command(ctx.main))
    return G.Outcome(G.OK, lines)
