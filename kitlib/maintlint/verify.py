"""kitlib.maintlint.verify: records that name repository files which no longer are what the
record says (docs/maint-lint.md "Path checks").

  paths(snap, root, cmd, sweep)        kitlib.backlog.stale.sweep's body paths gone and `files`
                                       changed or moved since `files_at` (its age list is not
                                       read: it is given no store history, so it has none)
  scopes(snap, root, rev, cmd, git)    a scope path missing from the checkout that `rev`'s
                                       history once held: it existed and is gone. A scope path
                                       no commit ever held is a file still to be made, not a
                                       finding

And whether the store and the code agree on what landed (docs/maint-lint.md "Landing checks"),
against the code base code_base reads: the pushed main (refs/remotes/<remote>/<main>), else the
local one. A branch has LANDED when its tip is in the base and off the base's first-parent chain
(kitlib.worktree.remove.merged_worktrees' rule), so a fresh branch, whose tip is a base tip, never
has.

  code_base(root, remote, main)        `((ref, chain, ancestors), None)` or `(None, why)`
  landed_pulls(snap, root, remote, base, cmd, tips)
                                       a pulled record whose pulled_by branch has landed
                                       (landed-still-pulled: the check the worktree list gives
                                       for an A tree, for every branch)
  closed_unlanded(changes, snap, root, base, cmd, commits)
                                       each record the store no longer holds whose newest close,
                                       retire or delete is `closes ID: landed <sha>`, whose <sha>
                                       the code checkout has and the base does not hold

Each returns `(findings, notes)`. Nothing here writes.
"""
import os
import re

from kitlib.backlog import scope as scmod
from kitlib.backlog import stale
from kitlib.git import batch
from kitlib.git import cmd as gitcmd

from . import Finding

GIT_TIMEOUT = 60


def paths(snap, root, cmd, today, sweep=stale.sweep):
    rep = sweep(snap, {}, today, stale.DEFAULT_DAYS, root)
    out = [Finding('body-path-gone', '%s %s' % (rid, ref),
                   '%s names `%s` in its body, and no file is at that path' % (rid, ref),
                   '%s set %s --body-file <file>   (the body re-pointed, or the record closed or'
                   ' retired if the work went with the file)' % (cmd, rid))
           for rid, ref in rep.bad_paths]
    out += [Finding('file-moved', '%s %s' % (rid, path),
                    '%s lists `%s` in files: %s since its files_at' % (rid, path, verdict),
                    '%s show %s   (re-check the record against the file; then set --files-at)'
                    % (cmd, rid))
            for rid, path, verdict in rep.moved]
    notes = []
    if rep.unchecked:
        notes.append('path checks: %d record(s) NOT CHECKED' % len(rep.unchecked))
    if rep.files_skipped:
        notes.append('path checks: %d `files` entr%s skipped' % (
            rep.files_skipped, 'y' if rep.files_skipped == 1 else 'ies'))
    return out, notes


def scope_path(member):
    """The repo path a scope member names (`a/b/**` names `a/b`), or None for `none` and a member
    with any other wildcard."""
    if member == scmod.NONE:
        return None
    path = member[:-len(scmod.GLOB_SUFFIX)] if member.endswith(scmod.GLOB_SUFFIX) else member
    if not path or any(ch in path for ch in scmod.WILDCARD_CHARS):
        return None
    return path


def last_commit(root, rev, path):
    """`(short sha or '', None)`: the newest commit on `rev` that touched `path`; `(None, why)`."""
    return gitcmd.run_text(['log', '-1', '--format=%h', rev, '--', path], root,
                           timeout=GIT_TIMEOUT)


def scopes(snap, root, rev, cmd, git=last_commit):
    out, unchecked, asked = [], 0, {}
    for rec in snap.readable:
        for member in rec.all('scope'):
            path = scope_path(member)
            if path is None or os.path.exists(os.path.join(root, *path.split('/'))):
                continue
            if path not in asked:
                asked[path] = git(root, rev, path)
            sha, why = asked[path]
            if sha is None:
                unchecked += 1
            elif sha:
                out.append(Finding(
                    'scope-path-gone', '%s %s' % (rec.stem, member),
                    '%s has scope `%s`, which %s held and the checkout no longer has'
                    % (rec.stem, member, rev),
                    '%s set %s --scope <path>   (or close or retire it if the work went with the'
                    ' path)' % (cmd, rec.stem)))
    notes = ['scope checks: %d path(s) NOT CHECKED (git failed)' % unchecked] if unchecked else []
    return out, notes


def code_base(root, remote, main):
    """`((ref, first-parent chain, ancestors), None)` of the code base in `root`: the pushed
    `refs/remotes/<remote>/<main>` when this checkout has it, else `refs/heads/<main>`; or
    `(None, why)`."""
    cands = ['refs/remotes/%s/%s' % (remote, main), 'refs/heads/%s' % main]
    found, why = batch.commits(cands, root, timeout=GIT_TIMEOUT)
    if found is None:
        return None, why
    ref = next((c for c in cands if found.get(c)), None)
    if ref is None:
        return None, 'neither %s nor %s names a commit' % tuple(cands)
    got, why = batch.ancestry(ref, root, timeout=GIT_TIMEOUT)
    if got is None:
        return None, why
    return (ref, got[0], got[1]), None


def branch_tips(root, remote):
    """`({branch: full sha}, None)`: every local branch and `<remote>/<branch>`, the local tip
    when both exist; or `(None, why)`."""
    out, why = gitcmd.run_text(['for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads',
                                'refs/remotes/%s' % remote], root, timeout=GIT_TIMEOUT)
    if out is None:
        return None, why
    local, tips = {}, {}
    for line in out.split('\n'):
        sha, _, ref = line.strip().partition(' ')
        if ref.startswith('refs/heads/'):
            local[ref[len('refs/heads/'):]] = sha
        elif ref.startswith('refs/remotes/%s/' % remote):
            tips[ref[len('refs/remotes/%s/' % remote):]] = sha
    tips.update(local)
    return tips, None


def landed_pulls(snap, root, remote, base, cmd, tips=branch_tips):
    pulled = [r for r in snap.readable if r.get('state') == 'pulled' and r.get('pulled_by')]
    if not pulled:
        return [], []
    found, why = tips(root, remote)
    if found is None:
        return [], ['landed pulls NOT CHECKED: %s' % why]
    ref, chain, ancestors = base
    out = []
    for r in pulled:
        branch = r.get('pulled_by')
        tip = found.get(branch)
        if not tip or tip in chain or tip not in ancestors:
            continue
        out.append(Finding(
            'landed-still-pulled', r.stem,
            '%s is still pulled, and its branch %s is in %s (landed)' % (r.stem, branch, ref),
            '%s close %s --reason "landed <merge>"   (or, when that landing was one slice of it:'
            ' %s --as %s release %s --reason "<why>")' % (cmd, r.stem, cmd, branch, r.stem)))
    return out, []


LANDED_CLOSE = re.compile(r'^closes (\S+): landed ([0-9a-f]{40})$')
# Every subject that ends a record (kitlib.backlog.plan's close, retire and delete).
ENDS = re.compile(r'^(?:closes|retires|deletes) (\S+): ')


def closed_unlanded(changes, snap, root, base, cmd, commits=batch.commits):
    """`changes`: kitlib.backlog.audit_check.history's Changes, newest first. Only a record's
    newest close, retire or delete is judged, and only when it is a landed close."""
    ref, _chain, ancestors = base
    newest = {}
    for change in changes:
        subject = change.subject.strip()
        m = ENDS.match(subject)
        if m and m.group(1) not in newest:
            landed = LANDED_CLOSE.match(subject)
            newest[m.group(1)] = landed.group(2) if landed else None
    # A record back in the store was reopened; a sha the base holds landed.
    asked = {rid: sha for rid, sha in newest.items()
             if sha and snap.get(rid) is None and sha not in ancestors}
    if not asked:
        return [], []
    have, why = commits(sorted(set(asked.values())), root, timeout=GIT_TIMEOUT)
    if have is None:
        return [], ['closed-unlanded: %d close(s) NOT CHECKED: %s' % (len(asked), why)]
    out, other = [], 0
    for rid, sha in sorted(asked.items()):
        if not have.get(sha):
            other += 1
            continue
        out.append(Finding(
            'closed-unlanded', rid,
            '%s was closed as landed by %s, which %s does not hold' % (rid, sha[:12], ref),
            '%s add %s --reopen --summary "<summary>" --found-on none --why-not-folded "<why>"'
            '   (once it is clear the work did not land by another commit)' % (cmd, rid)))
    notes = ['closed-unlanded: %d close(s) name a commit this checkout does not have (another'
             ' repository\'s, or not fetched): NOT CHECKED' % other] if other else []
    return out, notes
