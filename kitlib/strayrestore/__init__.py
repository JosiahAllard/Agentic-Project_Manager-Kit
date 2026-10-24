"""Discard a main-checkout stray a branch tip already holds, byte for byte (docs/stray-restore.md).

A stray is a change `git status` shows in a repository's MAIN checkout (kitlib.git.stray). This
package proves, path by path, that discarding one loses nothing: the work file's exact bytes are
the blob at the same path in the tip of a local branch (refs/heads/*) or a remote-tracking branch
(refs/remotes/*/*). Everything it cannot prove is LEAVE and is never touched; there is no override.

  plan(path)            -> Run: the verdict for every stray, writing nothing.
  apply(path, environ)  -> Run: plan(), then each restorable path is moved into a quarantine
                           folder, matched again from the moved bytes, and only then discarded
                           (restored to HEAD, or deleted when untracked). One log line per restore.
  allowed(mark)         -> may a session with this mark run it (unmarked, pm, coordinator, lander).

Nothing here prints, parses arguments or exits.
"""
import collections
import hashlib
import json
import os
import shutil
import stat
import time

from kitlib.git import cmd, refs, repo

DELETE = 'DELETE'           # untracked (??): the file is removed
RESTORE = 'RESTORE'         # modified in the work tree only ( M): the file becomes HEAD's bytes
UNDELETE = 'UNDELETE'       # deleted in the work tree only ( D): HEAD's file is written back
LEAVE = 'LEAVE'             # not proven lossless: never touched

ACTIONS = {DELETE: 'deleted', RESTORE: 'restored', UNDELETE: 'undeleted'}
ROLES = ('pm', 'coordinator', 'lander')
REF_PREFIXES = ('refs/heads/', 'refs/remotes/')
FILE_MODES = ('100644', '100755')
UNMERGED = ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU')
IN_PROGRESS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply',
               'BISECT_LOG')
LOG_NAME = 'kit-stray-restore.jsonl'
QUARANTINE = 'kit-stray-restore'
NOT_REACHED = 'not reached: an earlier error stopped the run'

Match = collections.namedtuple('Match', 'ref commit blob')
Item = collections.namedtuple('Item', 'path xy verdict match reason')
Run = collections.namedtuple('Run', 'top items refused left error')
"""`top` the main checkout; `items` [Item]; `refused` why the whole run was refused (nothing
touched) or None; `left` [(xy, path)] git status shows afterwards (apply only, else None);
`error` a git or file failure during apply, or None."""


def _after_check(full):
    """A test seam: called with the work file's path between its check and its move."""


def allowed(mark):
    """True for an unmarked session (the owner's) and the pm, coordinator and lander marks."""
    return mark is None or mark.strip().lower() in ROLES


def blob_id(data, fmt):
    """The git blob id of `data` in object format `fmt` (sha1 or sha256), computed here: no git
    filter can change the bytes hashed."""
    h = hashlib.sha256() if fmt == 'sha256' else hashlib.sha1()
    h.update(b'blob %d\0' % len(data))
    h.update(data)
    return h.hexdigest()


class _Repo:
    """What one run asks git about the main checkout at `top`, each answer asked once."""

    def __init__(self, top):
        self.top = top
        self._trees = {}

    def setup(self):
        """None, or why git could not answer."""
        fmt, why = cmd.run_text(['rev-parse', '--show-object-format'], self.top)
        if not fmt:
            return 'git rev-parse --show-object-format failed: %s' % why
        self.fmt = fmt
        head, why = cmd.run_text(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], self.top)
        if not head:
            return 'the main checkout has no HEAD commit: %s' % (why or 'none')
        self.head = head
        out, why = cmd.run(['for-each-ref', '--format=%(objectname) %(objecttype) %(symref) '
                            '%(refname)'] + [p.rstrip('/') for p in REF_PREFIXES], self.top)
        if out is None:
            return 'git for-each-ref failed: %s' % why
        tips = []
        for line in cmd.text(out).splitlines():
            parts = line.split(' ', 3)
            if len(parts) != 4:
                continue
            sha, kind, symref, name = parts
            # A symbolic ref (refs/remotes/origin/HEAD) names another tip; a ref to a tag object
            # or a tree is not a branch tip.
            if symref or kind != 'commit' or not name.startswith(REF_PREFIXES):
                continue
            tips.append((name, sha))
        tips.sort(key=lambda t: (not t[0].startswith('refs/heads/'), t[0]))
        self.tips = tips
        return None

    def entry(self, commit, path):
        """(mode, blob id) of `path` in `commit`, or None when it is not there. Read from the
        parent tree's listing, so the path is matched exactly, never as a pattern."""
        parent, _, name = path.rpartition('/')
        key = (commit, parent)
        if key not in self._trees:
            spec = '%s:%s' % (commit, parent) if parent else '%s^{tree}' % commit
            out, _why = cmd.run(['ls-tree', '-z', spec], self.top)
            listing = {}
            for rec in cmd.text(out or b'').split('\0'):
                meta, tab, nm = rec.partition('\t')
                fields = meta.split()
                if tab and len(fields) == 3:
                    listing[nm] = (fields[0], fields[2])
            self._trees[key] = listing
        return self._trees[key].get(name)

    def filtered(self, paths):
        """{path: True} for each path whose `filter` attribute is set (a driver, LFS or other)."""
        if not paths:
            return {}
        out, why = cmd.run(['check-attr', '-z', '--stdin', 'filter'], self.top,
                           stdin=b''.join(p.encode('utf-8') + b'\0' for p in paths))
        if out is None:
            raise _GitFailed('git check-attr failed: %s' % why)
        f = cmd.text(out).split('\0')
        result = {}
        for i in range(0, len(f) - 2, 3):
            result[f[i]] = f[i + 2] not in ('unspecified', 'unset')
        return result

    def match(self, data, path):
        """The first Match of `data` at `path` in a branch tip, or None. Only the raw form counts:
        the blob id of `data` is the tip's blob there. Bytes that differ from every blob (a
        checkout's eol or encoding form, say) are in no git object, so they are never matched."""
        bid = blob_id(data, self.fmt)
        for name, sha in self.tips:
            e = self.entry(sha, path)
            if e is not None and e[0] in FILE_MODES and e[1] == bid:
                return Match(name, sha, bid)
        return None


class _GitFailed(Exception):
    pass


def _reparse(full):
    try:
        st = os.lstat(full)
    except OSError:
        return False
    attrs = getattr(st, 'st_file_attributes', 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT',
                                                            0x400))


def _unsafe_place(top, path):
    """Why `path` under `top` cannot be touched safely (a link on the way, outside the top), or
    None."""
    if not path or path.endswith('/') or '\n' in path or '\r' in path or '\0' in path:
        return 'a directory or an unusual name'
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return 'an unusual name'
    cur = top
    for p in parts:
        cur = os.path.join(cur, p)
        if _reparse(cur):
            return 'a symlink, junction or reparse point at %s' % os.path.relpath(cur, top)
    full = os.path.join(top, *parts)
    real_top = os.path.normcase(os.path.realpath(top))
    real = os.path.normcase(os.path.realpath(full))
    if not real.startswith(real_top.rstrip('\\/') + os.sep):
        return 'it resolves outside the checkout'
    return None


def _read(full):
    with open(full, 'rb') as fh:
        return fh.read()


def _verdict(r, top, xy, path, filtered):
    """Item for one status entry."""
    if xy not in ('??', ' M', ' D'):
        return Item(path, xy, LEAVE, None, 'staged or another change (git status %r) this tool '
                    'cannot prove lossless' % xy)
    why = _unsafe_place(top, path)
    if why:
        return Item(path, xy, LEAVE, None, why)
    if filtered.get(path):
        return Item(path, xy, LEAVE, None, 'a filter attribute is set (a filter driver runs '
                    'other code)')
    full = os.path.join(top, *path.split('/'))
    if xy != '??':
        e = r.entry(r.head, path)
        if e is None or e[0] not in FILE_MODES:
            return Item(path, xy, LEAVE, None, 'HEAD holds no plain file there')
    if xy == ' D':
        if os.path.lexists(full):
            return Item(path, xy, LEAVE, None, 'something is at the path again')
        return Item(path, xy, UNDELETE, None, 'HEAD holds it')
    if not os.path.isfile(full) or os.path.isdir(full):
        return Item(path, xy, LEAVE, None, 'not a regular file')
    try:
        data = _read(full)
    except OSError as exc:
        return Item(path, xy, LEAVE, None, 'could not read it: %s' % exc)
    m = r.match(data, path)
    if m is None:
        return Item(path, xy, LEAVE, None, 'no branch tip holds these bytes at this path')
    return Item(path, xy, DELETE if xy == '??' else RESTORE, m, None)


def _locate(path):
    """(top, None) of the main checkout `path` belongs to, or (None, why)."""
    place = repo.classify(path)
    top = repo.main_checkout_toplevel(path, place)
    if top is None:
        return None, 'could not find the main checkout of the repository at %s (%s%s)' % (
            os.path.abspath(path), place.kind, ': %s' % place.detail if place.detail else '')
    return os.path.normpath(top), None


def _refusal(top, entries):
    """Why the whole run must stop before touching anything, or None."""
    for xy, p in entries:
        if xy in UNMERGED:
            return 'an unmerged path (%s, git status %r): a merge is in progress' % (p, xy)
    for name in IN_PROGRESS:
        where, why = repo.git_path(top, name)
        if where is None:
            return 'git rev-parse --git-path %s failed: %s' % (name, why)
        if os.path.lexists(where):
            return 'an operation is in progress in the main checkout (%s exists)' % name
    return None


def plan(path):
    """Run for the main checkout of the repository at `path`, writing nothing."""
    return _plan(path)[0]


def _plan(path):
    """(Run, _Repo or None): the _Repo only when every stray has a verdict."""
    top, why = _locate(path)
    if top is None:
        return Run(None, [], None, None, why), None
    entries, why = refs.status(top, untracked='all')
    if entries is None:
        return Run(top, [], None, None, 'git status failed in the main checkout: %s' % why), None
    refused = _refusal(top, entries)
    if refused:
        return Run(top, [], refused, None, None), None
    r = _Repo(top)
    why = r.setup()
    if why:
        return Run(top, [], None, None, why), None
    try:
        filtered = r.filtered([p for _xy, p in entries])
    except _GitFailed as exc:
        return Run(top, [], None, None, str(exc)), None
    return Run(top, [_verdict(r, top, xy, p, filtered) for xy, p in entries], None, None,
               None), r


def _stamp():
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())


def _log(common, record):
    with open(os.path.join(common, LOG_NAME), 'a', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps(record, sort_keys=True) + '\n')


def _restore_head(top, path, commit):
    """Write `commit`'s file at `path`: the commit the log names, not HEAD as it is now."""
    out, why = cmd.run(['restore', '--source=%s' % commit, '--worktree', '--',
                        ':(literal)%s' % path], top)
    return None if out is not None else (why or 'git restore failed')


def _apply_one(r, top, common, qdir, item, environ):
    """(Item as done, error or None). An Item whose verdict is LEAVE was not discarded."""
    full = os.path.join(top, *item.path.split('/'))
    record = {'ts': _stamp(), 'top': top, 'path': item.path, 'status': item.xy,
              'action': ACTIONS[item.verdict], 'head': r.head,
              'head_blob': (r.entry(r.head, item.path) or (None, None))[1],
              'mark': _mark(environ), 'session': (environ.get('PM_KIT_SESSION_NAME') or '-'),
              'bytes_blob': None, 'match_ref': None, 'match_commit': None}
    if item.verdict == UNDELETE:
        if os.path.lexists(full):
            return item._replace(verdict=LEAVE, reason='something is at the path again'), None
        why = _restore_head(top, item.path, r.head)
        if why:
            return item._replace(verdict=LEAVE, reason=why), why
        why = _log_safe(common, record)
        if why:
            # HEAD's file is back (nothing was lost) but no line records it: stop the run.
            return item, why
        return item, None
    _after_check(full)
    held = os.path.join(qdir, *item.path.split('/'))
    try:
        os.makedirs(os.path.dirname(held), exist_ok=True)
        os.replace(full, held)
    except OSError as exc:
        return item._replace(verdict=LEAVE, match=None, reason='could not move it aside: %s'
                             % exc), None
    # The verdict comes from the moved bytes: a writer that changed the file after the check
    # cannot slip new bytes past it.
    try:
        m = r.match(_read(held), item.path)
    except OSError as exc:
        m, failed = None, exc
    else:
        failed = None
    if m is None:
        back = _put_back(held, full)
        reason = 'the bytes changed after the check' if failed is None else (
            'could not read the moved copy: %s' % failed)
        return item._replace(verdict=LEAVE, match=None, reason=reason + back), None
    if item.verdict == RESTORE:
        if os.path.lexists(full):
            return item._replace(verdict=LEAVE, match=None, reason='something was written at the '
                                 'path; the stray is kept at %s' % held), None
        why = _restore_head(top, item.path, r.head)
        if why:
            back = _put_back(held, full)
            return item._replace(verdict=LEAVE, match=None, reason=why + back), why
    record.update(bytes_blob=m.blob, match_ref=m.ref, match_commit=m.commit)
    why = _log_safe(common, record)
    if why:
        # The path was acted on (deleted, or HEAD's file written), so the item keeps its verdict.
        # No line records the discard, so the moved copy stays where the error says.
        why = '%s; %s acted on, the stray is kept at %s' % (why, item.path, held)
        return item._replace(match=m, reason=why), why
    try:
        os.remove(held)
    except OSError:
        pass
    return item._replace(match=m), None


def _log_safe(common, record):
    """None when the log line is written, else why not."""
    try:
        _log(common, record)
        return None
    except OSError as exc:
        return 'could not write the log: %s' % exc


def _put_back(held, full):
    """'' when the moved file is back at `full`, else a note naming where it is kept."""
    try:
        os.rename(held, full)            # never over a file written there meanwhile
        return ''
    except OSError:
        return '; the file is kept at %s' % held


def _mark(environ):
    from kitlib import sessionmark
    return sessionmark.mark(environ)


def apply(path, environ=None):
    """plan(), then discard each restorable path (module docstring). Returns Run with `left`.

    An error before anything is touched returns no items. An error part way stops the run: the
    paths not reached are LEAVE (NOT_REACHED). A failing `git status` at the end sets `error`
    with `left` None, whatever was done before it."""
    environ = os.environ if environ is None else environ
    run, r = _plan(path)
    if r is None:
        return run
    top = run.top
    common, why = repo.common_dir(top)
    if common is None:
        # Nothing has been touched: report no items, so none reads as done.
        return run._replace(items=[], error='git rev-parse --git-common-dir failed: %s' % why)
    qdir = os.path.join(common, QUARANTINE, 'q-%s-%d' % (_stamp(), os.getpid()))
    done, error = [], None
    for item in run.items:
        if item.verdict == LEAVE:
            done.append(item)
            continue
        if error:
            done.append(item._replace(verdict=LEAVE, match=None, reason=NOT_REACHED))
            continue
        item, error = _apply_one(r, top, common, qdir, item, environ)
        done.append(item)
    _drop_empty(qdir)
    left, why = refs.status(top, untracked='all')
    if left is None:
        failed = 'git status failed afterwards: %s' % why
        error = '%s; %s' % (error, failed) if error else failed
    return Run(top, done, None, left, error)


def _drop_empty(qdir):
    """Remove the quarantine folder when it holds no file (a kept stray keeps it)."""
    if not os.path.isdir(qdir):
        return
    for _root, _dirs, files in os.walk(qdir):
        if files:
            return
    shutil.rmtree(qdir, ignore_errors=True)
