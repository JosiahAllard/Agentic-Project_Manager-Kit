"""kitlib.hubjobs.store_commits: the hub server's hourly commit of the backlog store in the PM data
worktree (docs/backlog-store.md "Hub store-commits job").

  make(sources) -> jobs.Job | None   the job; None when this machine has no PM data worktree
  StoreCommits(root=None, ...)       its run(ctx): one pass

A backlog.py write in the PM data worktree commits its own record paths (MainBackend.publish).
What is left uncommitted there is what a write could not commit (errors.Uncommitted) and what a
hand changed. Each pass lists those paths (MainBackend.pending) and commits the record paths
among them through MainBackend.commit_paths, the store's own `catch-up --apply` path: it holds
git's index lock in that worktree (the store's _IndexLock) from before it reads the files to its
index update, commits exactly those literal paths as one commit on HEAD by compare-and-swap, and
stages nothing else. This job runs no git write of its own.

A path is committed only when its record is whole: a file changed in the last SETTLE_S seconds is
left for a later pass (a write or a save may still be going on), and a file that does not parse
as a record (kitlib.backlog.record: empty, no header, a header line that is not `key: value`) or
whose `id:` is not its file name is left out, and the pass fails naming it once the rest are
committed. A path that is not a record (`not a record`) is never committed.

It never pushes: the hub's git sync (kitlib.hubsite.gitsync) pushes the PM data branch whenever it
differs from the remote's, these commits included, and a second pusher of that ref would race it.

Outcomes (kitlib.hubsite.jobs): nothing to commit, a store that is not a folder store in the PM
data worktree, or the index lock held by another git command (errors.Busy) are skips. A commit
returns its short sha and pushes the job event `committed`. Any other store error (Overtaken,
Uncommitted, IndexStale, Failure) is a failure, and the job then stops: every later pass raises
the same error and commits nothing while one of that failure's paths is still uncommitted
(MainBackend.pending lists it), so a person clears it first (the error says how).
"""
import os
import time

from kitlib import config as kconfig
from kitlib import pmdata
from kitlib.backlog import errors
from kitlib.backlog import store as bstore
from kitlib.hubsite import jobs

NAME = 'store-commits'
EVERY_S = 3600
SETTLE_S = 60                       # a record file changed more recently is left for a later pass
SUBJECT = 'backlog: hub commits %d uncommitted record path%s'


class StoreStuck(Exception):
    """The job is stopped: an earlier pass failed on paths that are still uncommitted."""


class UnreadableRecord(Exception):
    """Uncommitted record files that are not whole records, left out of the commit."""


def make(sources):
    try:
        settings = pmdata.machine_settings(getattr(sources, 'home', None))
    except (kconfig.ConfigError, pmdata.PmDataError):
        settings = None             # the hub's own start-up reports an unreadable machine config
    if not settings or not settings.get('path'):
        return None
    return jobs.Job(NAME, EVERY_S, StoreCommits(root=settings['path']))


def check_record(path, data):
    """'' when `data` (the bytes of the record file `path`, as git spells it) is a whole record:
    it parses with no structural finding and its `id:` is its file name; else why not."""
    stem = path.rsplit('/', 1)[-1][:-3]
    rec = bstore.parse_blob(stem, data, path)
    if not rec.readable:
        return '; '.join(f.message for f in rec.structural)
    if rec.get('id') != stem:
        return 'its id: is %r, not its file name %r' % (rec.get('id'), stem)
    return ''


class StoreCommits(object):
    """run(ctx) is the job. `root` is the PM data worktree. The tests' seams: `backend(root)` the
    store's backend (default the one the worktree's .kit.json names, bstore.open_backend) and
    `clock` epoch seconds."""

    def __init__(self, root=None, backend=None, clock=None):
        self.root = root
        self.backend = backend or (lambda root: bstore.open_backend(root, kconfig.load(root),
                                                                    fetch=False))
        self.clock = clock or time.time
        self._stuck = None          # (paths, text) of the failure that stopped the job

    def __call__(self, ctx):
        be = self.backend(self.root)
        if not isinstance(be, bstore.MainBackend) or not be.commit_branch():
            return jobs.Skip('%s is not a folder store in the PM data worktree: its writes are '
                             'not committed there' % be.describe())
        pending = be.pending()
        records = [p for kind, p in pending if kind != 'not a record']
        if self._stuck is not None:
            paths, text = self._stuck
            if set(paths) & set(records):
                raise StoreStuck('stopped until cleared: %s' % text)
            self._stuck = None
        others = len(pending) - len(records)
        if not records:
            return jobs.Skip('nothing to commit' + _others(others))
        todo, young, bad = self._whole(be, records)
        if ctx.stop.is_set():
            return jobs.Skip('stopping')
        note = ''
        if todo:
            try:
                sha = be.commit_paths(todo, SUBJECT % (len(todo), '' if len(todo) == 1 else 's')
                                      + '\n\nSession: hub\n')
            except errors.Busy as e:
                return jobs.Skip('the index lock is held; tried again next pass: %s' % e)
            except errors.Refusal as e:
                text = '%s: %s' % (type(e).__name__, e)
                self._stuck = (list(getattr(e, 'paths', None) or todo), text)
                raise
            if sha:
                ctx.event('committed', sha[:12])
                note = 'committed %d record path%s at %s' % (
                    len(todo), '' if len(todo) == 1 else 's', sha[:12])
        if bad:
            raise UnreadableRecord('%s; not committed: %s' % (
                note or 'nothing committed',
                '; '.join('%s (%s)' % (p, why) for p, why in bad)))
        if young:
            note = '%s%s left for a later pass (changed in the last %d s): %s' % (
                note + '; ' if note else '', 'one path' if len(young) == 1 else
                '%d paths' % len(young), SETTLE_S, ', '.join(young))
        if not note:
            return jobs.Skip('HEAD already holds them')
        return note + _others(others)

    def _whole(self, be, records):
        """(paths to commit, paths left as too new, [(path, why)] not whole records). A deleted
        record (no file) is committed as a deletion."""
        todo, young, bad = [], [], []
        now = self.clock()
        for p in records:
            full = os.path.join(be.root, *p.split('/'))
            try:
                st = os.stat(full)
            except FileNotFoundError:
                todo.append(p)
                continue
            if now - st.st_mtime < SETTLE_S:
                young.append(p)
                continue
            with open(full, 'rb') as f:
                why = check_record(p, f.read())
            (bad.append((p, why)) if why else todo.append(p))
        return todo, young, bad


def _others(n):
    return '' if not n else '; %d path%s there not a record, never committed' % (
        n, '' if n == 1 else 's')
