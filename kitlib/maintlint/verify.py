"""kitlib.maintlint.verify: records that name repository files which no longer are what the
record says (docs/maint-lint.md "Path checks").

  paths(snap, root, cmd, sweep)        kitlib.backlog.stale.sweep's body paths gone and `files`
                                       changed or moved since `files_at` (its age list is not
                                       read: it is given no store history, so it has none)
  scopes(snap, root, rev, cmd, git)    a scope path missing from the checkout that `rev`'s
                                       history once held: it existed and is gone. A scope path
                                       no commit ever held is a file still to be made, not a
                                       finding

Each returns `(findings, notes)`. Nothing here writes.
"""
import os

from kitlib.backlog import scope as scmod
from kitlib.backlog import stale
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
