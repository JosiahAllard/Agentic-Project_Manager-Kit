"""kitlib.hubjobs.unblock_conditions: the hub server's check of the backlog's hold end conditions
(docs/action-hub.md "Timed jobs", docs/backlog.md "block --until").

  make(sources) -> jobs.Job | None   the job; None when launcher.repos names no repository
  UnblockJob(roots, readers, ...)    its run(ctx): one pass
  Readers(hub_store, plugin_root, record_closed)
                                     what a condition is read against (conditions.evaluate)

`block ID --on M --until "<condition>"` writes `until: M <condition>` on a record. Every EVERY_S
the job reads each repository's store (launcher.repos, the store coord_restart.store_root
names; no fetch: the hub's own backlog fetch keeps the local copy fresh) and, on each blocked
record, each `until` line whose member the record is still blocked on. A condition that is one of
the forms kitlib/backlog/conditions.py reads, and is met, clears that member: one store commit
under Session: hub-condition with the reason `condition met: <condition>`
(kitlib.hubsite.recordwrite.condition_met, which re-reads the record and writes nothing when the
line is gone), then one `unblocked` job push to the PM, detail `<repo>:<ID>`. The commit drops the
`until` line, so the next pass finds nothing to do: a rerun is idempotent.

Never acted on: free text (not one of the forms), a member the record is not blocked on, an
`owner` member with any condition but a to-do or a question (the owner's own act; the job never
clears the owner's hold on its own reading), and a condition that cannot be told (a hub document,
a store or the plugin export root that cannot be read: conditions.evaluate's None). A coordinator
learns of the opened record from coord_restart's backlog wake (a stopped coordinator whose offer
gained it is restarted); a live one gets no push of its own.

Where each condition is read: a to-do or question, the hub store's document on disk (read only,
`<store>/todos/<T-n>.json`, `<store>/questions/<Q-n>.json`: the store writes each with
os.replace, so a read sees a whole document); a record, the named repository's store (closed: gone
from it, with a commit that removed it); a path, the file system; a plugin folder, the plugin
export root (kitlib.pluginsource settings, `export_dir`).

Outcomes (kitlib.hubsite.jobs): nothing met is a skip; an unblock is a success whose note names
the records. A store that cannot be read, or a write the store refused, is a failure once the
pass has done what it could (health shows it; the runner pushes `failing` at the third in a row).
A repository launcher.repos names whose store cannot be found is a note, not a failure.
"""
import json
import os

from kitlib.hubsite import jobs

NAME = 'unblock-conditions'
EVERY_S = 300
EVENT = 'unblocked'
NOTE_MAX = 200


class ConditionsFailed(Exception):
    """A store that could not be read, or a write that did not go through."""


class Readers(object):
    """conditions.evaluate's readers. `hub_store` is the hub store folder (None: to-dos and
    questions cannot be told), `plugin_root` the plugin export root (None: plugin folders cannot
    be told), `record_closed(repo, rid)` True, False or None."""

    def __init__(self, hub_store, plugin_root, record_closed):
        self.hub_store = hub_store
        self.plugin_root = plugin_root
        self.record_closed = record_closed

    def _status(self, coll, doc_id):
        if not self.hub_store:
            return None
        try:
            with open(os.path.join(self.hub_store, coll, doc_id + '.json'), encoding='utf-8-sig') as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        status = doc.get('status') if isinstance(doc, dict) else None
        return status if isinstance(status, str) else None

    def todo_status(self, tid):
        return self._status('todos', tid)

    def question_status(self, qid):
        return self._status('questions', qid)

    def path_exists(self, path):
        return os.path.exists(path)

    def plugin_present(self, folder):
        if not self.plugin_root:
            return None
        return os.path.isdir(os.path.join(self.plugin_root, folder))


def open_snapshot(root):
    """(snapshot, store) of the store at `root`, read with no fetch, or (None, None) for the
    implicit store (the hub writes none: kitlib.hubsite.recordwrite). A store that cannot be read
    raises."""
    from kitlib.backlog import opener
    from kitlib.hubsite import recordwrite
    ctx = opener.open_store(root, fetch=False, session=recordwrite.CONDITION_SESSION)
    if getattr(ctx.store.backend, 'implicit', False):
        return None, None
    return ctx.store.load(), ctx.store


def watched(snap):
    """[(rid, member, condition)] of each blocked record's `until` lines whose member it is still
    blocked on and that this job may act on: a readable condition, and on `owner` an owner event
    only."""
    from kitlib.backlog import conditions as condmod
    from kitlib.backlog import record as rmod
    out = []
    for rec in snap.readable:
        if rec.get('state') != 'blocked':
            continue
        members = rec.all('blocked_on')
        for member, cond in rmod.until_lines(rec):
            if member not in members or condmod.parse(cond) is None:
                continue
            if condmod.owner_error(member, cond):
                continue
            out.append((rec.id, member, cond))
    return out


class UnblockJob(object):
    """One pass per run. `roots()` -> ([(repo, root)], notes): the stores to read; `readers()`
    -> a fresh Readers for the pass; `write(root, rid, member, cond)` -> {"lines", "unblocked"}
    (recordwrite.condition_met); `snapshot(root)` -> (snapshot, store) (open_snapshot)."""

    def __init__(self, roots, readers, write=None, snapshot=None):
        self.roots = roots
        self.readers = readers
        self.write = write
        self.snapshot = snapshot or open_snapshot

    def __call__(self, ctx):
        from kitlib.backlog import conditions as condmod
        from kitlib.hubsite.store import HubError
        write = self.write
        if write is None:
            from kitlib.hubsite import recordwrite
            write = recordwrite.condition_met
        roots, notes = self.roots()
        readers = self.readers()
        done, failed, count = [], [], 0
        for repo, root in roots:
            if ctx.stop.is_set():
                break
            try:
                snap, _store = self.snapshot(root)
            except Exception as e:      # noqa: BLE001 - one store's fault leaves the others
                failed.append('%s: the store could not be read (%s)' % (repo, type(e).__name__))
                continue
            if snap is None:
                continue
            for rid, member, cond in watched(snap):
                count += 1
                if condmod.evaluate(condmod.parse(cond), readers) is not True:
                    continue
                try:
                    out = write(root, rid, member, cond)
                except HubError as e:
                    failed.append('%s:%s: %s' % (repo, rid, e.message))
                    continue
                if out.get('unblocked'):
                    done.append('%s:%s' % (repo, rid))
                    ctx.event(EVENT, '%s:%s' % (repo, rid))
        note = '; '.join(
            (['unblocked %s' % ', '.join(done)] if done else []) + notes)[:NOTE_MAX]
        if failed:
            raise ConditionsFailed('; '.join(failed + ([note] if note else []))[:NOTE_MAX * 2])
        if not done:
            return jobs.Skip(('no condition met (%d watched)' % count
                              + ('; ' + note if note else ''))[:NOTE_MAX])
        return note


def repo_roots(settings):
    """([(repo, store root)], notes): launcher.repos' repositories, each once per store root (two
    repositories sharing a store read it once, under the first name)."""
    from kitlib.hubjobs import coord_restart
    out, seen, notes = [], set(), []
    for repo in sorted(settings.repos):
        try:
            root = coord_restart.store_root(settings, repo)
        except coord_restart.BacklogUnread as e:
            notes.append('%s skipped: %s' % (repo, e))
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            out.append((repo, root))
    return out, notes


class RecordReader(object):
    """record_closed for one pass: each repository's store read once."""

    def __init__(self, settings, snapshot=None):
        self.settings = settings
        self.snapshot = snapshot or open_snapshot
        self._read = {}

    def __call__(self, repo, rid):
        from kitlib.hubjobs import coord_restart
        if repo not in self._read:
            try:
                root = coord_restart.store_root(self.settings, repo)
                self._read[repo] = self.snapshot(root)
            except Exception:           # noqa: BLE001 - not told: the condition waits
                self._read[repo] = (None, None)
        snap, store = self._read[repo]
        if snap is None:
            return None
        if snap.get(rid) is not None:
            return False
        try:
            gone = store.uncommitted_removal(rid) or store.removal(rid, snap)
        except Exception:               # noqa: BLE001
            return None
        return True if gone else None


def plugin_root(home):
    """The plugin export root, or None when it cannot be told."""
    from kitlib import pluginsource
    try:
        return pluginsource.load(home).export_dir
    except Exception:                   # noqa: BLE001 - a bad config: plugin conditions wait
        return None


def hub_store(home):
    """The hub store folder, or None when it cannot be told."""
    from kitlib.hubsite import settings as hsettings
    try:
        return hsettings.load(home=home).store
    except Exception:                   # noqa: BLE001
        return None


def make(sources):
    from kitlib.launcher import settings as lsettings
    home = getattr(sources, 'home', None)
    s = lsettings.load(home)
    if not s.repos:
        return None                     # no repository to read on this machine
    job = UnblockJob(lambda: repo_roots(s),
                     lambda: Readers(hub_store(home), plugin_root(home), RecordReader(s)))
    return jobs.Job(NAME, EVERY_S, job)
