"""kitlib.hubsite.store: the hub server's store, the one record of hub data (the hub site design,
section 1 "Hub store format").

Layout under the store folder `root` (the caller resolves it; nothing here reads config):

    <root>/<collection>/<id>.json   one document: its fields, sorted keys, 2-space indent, UTF-8,
                                    one final newline (the text the old hub's data folder
                                    holds, so that folder opens as a store with no conversion),
                                    plus the store's own `version` (int) and `updated_at`; a
                                    document with no `version` (an old-hub file) is at version 0
    <root>/_meta.json               {revision, counters: {Q, M, T, P, S, A, E}}
    <root>/_store.lock              the single-writer lock (below); never a document

Only schema.COLLECTIONS are read and written; any other folder (parked/ from the old hub) is left
alone. A question whose `recommended` is text that stands for a number (the rule is in
schema.whole_recommended's docstring) is read as that number, so its next write stores the
number; its file is not rewritten until then.

One writer. Store.open takes kitlib.fs.treelock.TreeLock on `_store.lock` for the life of
the Store: an msvcrt byte-range lock the system drops when the holder exits, however it exits. A
second open, in this process or another, raises StoreBusy naming the holder. A dead holder's lock
is already gone, so the next open succeeds; `took_over` is the record it left. Inside the process
one mutex serialises every write and read.

A write, in this order (add, put, update, delete):
  1. take the mutex;
  2. check: schema.check, then kitlib.hubvalidate.validate(after, only={rel}) on the documents
     with the change applied, the way kitlib.backlog.store.check_change does. A new `error`, or a
     new finding whose code is in REFUSED, refuses the write (HubError invalid); new `warn`
     findings come back as Result.warnings. Findings the document already had never block. A
     delete is not validated;
  3. write `_meta.json` with the revision bumped (and the id counter, for add);
  4. write the document (a temp file, then os.replace, retried while a reader holds the file);
  5. append the event to the in-memory ring and wake the waiters.
_meta.json is written BEFORE the document: a crash between the two leaves a revision or an id
skipped, never a document newer than the revision. Store.open also raises each id counter to the
highest id on disk, treating an unreadable or empty _meta.json as missing, and removes the temp
files a crash left. An id is never given twice, unless _meta.json is lost or rolled back after the
document holding the highest id was deleted: the counter is then rebuilt from the ids still on
disk. A put of a server-style id (Q-005) raises its counter too, committed with the meta. A
counter never starts below ID_FLOOR: the first question add gives is Q-1000, whatever the
store holds below it.

Ids are file names, and NTFS ignores case: a new id that differs only in case from an existing
one (Alpha beside alpha) is refused, HubError invalid.

Revision and versions. `revision` counts writes over the store's life; it can skip a number.
Each document's `version` counts its own writes. `if_version` pins a write: None writes
unconditionally, an int must equal the document's version, and 0 matches a document that does
not exist yet or that the store has never written (an old-hub file); a mismatch is HubError
conflict. put creates or replaces; update merges top-level
keys and delete removes, both HubError not_found on a missing document.

Events. The ring holds the last RING (512) events, (rev, collection, id, op, status), oldest
evicted first. It lives in memory only: after open it is empty. changes_since(rev) gives the
events after rev, with `reset` True when some of them are no longer held (evicted, or written
before this open) or when rev is past the store's revision (a _meta.json lost or rolled back
restarts the revision from what it holds, 0 when it is missing), so the caller reads a snapshot
instead.

pending(): the owner input nobody has handled yet (design section 4): answered questions,
submitted stories, sent to_pm items, to-dos that are done or added by the owner without
pm_ack, and approvals the owner decided that are not consumed (approval_decided), read at the
store's clock.

Nothing here prints, parses arguments or exits.
"""
import collections
import copy
import datetime
import json
import os
import re
import threading

from kitlib import hubvalidate
from kitlib.fs import replace as fsreplace
from kitlib.fs.treelock import TreeBusy, TreeLock, TreeLockError, describe

from . import schema

LOCK_NAME = '_store.lock'
PAGES_LOCK = threading.Lock()   # serialises a hub page's write or delete: its card and body docs
META_NAME = '_meta.json'
RING = 512                      # events held for changes_since and wait
REPLACE_S = 2.0                 # how long os.replace is retried while a reader holds the file
REFUSED = ('handstep-commands',)    # warn-level hubvalidate codes the store refuses (section 9)
TMP_SUFFIX = '.tmp'
# The lowest id add gives, by prefix (1 when absent). The rulings ledger cites questions up to
# Q-978 from before the store, whose counter had fallen below them; new questions start at
# Q-1000 so no id the ledger cites is given again (an owner ruling).
ID_FLOOR = {'Q': 1000}

Result = collections.namedtuple('Result', 'id version revision warnings')
Event = collections.namedtuple('Event', 'rev collection id op status')
Changes = collections.namedtuple('Changes', 'revision events reset')
# sender: the document's own `from` as stored ('' when it has none); the api routes on it.
# note: a to_pm note's own `kind` as stored, 'filed' for a question filed into its record ('' for
# the rest); the api cuts an act note's title and routes a filed question on it, and no caller is
# sent it
Pending = collections.namedtuple('Pending', 'kind id title repo at sender note',
                                 defaults=('', ''))


class HubError(Exception):
    """A refused or failed store call: code is invalid, not_found, conflict or store (the
    design's section 2 codes); field names the field at fault, or ''."""

    def __init__(self, code, message, field=''):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.field = field


class StoreBusy(HubError):
    """Another Store holds the store's lock; record is its holder's record ({} if unread)."""

    def __init__(self, root, record):
        HubError.__init__(self, 'store', 'the hub store %s is %s' % (root, describe(record)))
        self.record = record


def normal_text(fields):
    """A document's file text: sorted keys, 2-space indent, UTF-8 kept, one final newline."""
    return json.dumps(fields, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _replace(path, text):
    """Write text to a temp file beside path, then os.replace path with it, retrying for
    REPLACE_S (kitlib.fs.replace) while a reader (an indexer, an antivirus scan) holds path open."""
    folder, name = os.path.split(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, '.%s.%d%s' % (name, os.getpid(), TMP_SUFFIX))
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    try:
        fsreplace.replace(tmp, path, wait=REPLACE_S)
    except PermissionError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _remove(path):
    fsreplace.remove(path, wait=REPLACE_S)


def _read_json(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            obj = json.load(f)
    except (OSError, ValueError) as e:
        raise HubError('store', 'cannot read %s: %s' % (path, e))
    if not isinstance(obj, dict):
        raise HubError('store', '%s is not a JSON object' % path)
    return obj


def _sweep(folder):
    """Remove the temp files a crashed write left in folder."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if n.startswith('.') and n.endswith(TMP_SUFFIX):
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def _version(doc):
    v = (doc or {}).get('version', 0)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _id_number(prefix, doc_id):
    m = re.match(r'^%s-(\d+)$' % prefix, doc_id, re.I)
    return int(m.group(1)) if m else 0


def _finding_key(f):
    return (f.level, f.rel, f.field, f.code, f.message)


class Store(object):
    """The hub store at root, held for writing by this object until close(). See the module
    docstring. Use Store.open."""

    def __init__(self, root, clock=None, known_repos=None):
        self.root = os.path.abspath(root)
        # callable() -> the PM's repo names; asked only on a write whose document has a repo tag
        self.known_repos = known_repos or (lambda: ())
        self.clock = clock or _now
        self.took_over = {}
        self._lock = TreeLock(os.path.join(self.root, LOCK_NAME), what='hub store')
        self._mutex = threading.Lock()
        self._cond = threading.Condition(self._mutex)
        self._docs = {}
        self._revision = 0
        self._counters = {}
        self._ring = collections.deque(maxlen=RING)
        self._known = 0             # every event after this revision is in the ring
        self._open = False

    @classmethod
    def open(cls, root, clock=None, known_repos=None):
        """The Store at root, its lock taken and its documents loaded; StoreBusy when another
        Store holds it, HubError store when it cannot be opened or a document cannot be read."""
        s = cls(root, clock, known_repos)
        try:
            os.makedirs(s.root, exist_ok=True)
            s._lock.acquire()
        except TreeBusy as e:
            raise StoreBusy(s.root, e.record)
        except (TreeLockError, OSError) as e:
            raise HubError('store', 'cannot lock the hub store %s: %s' % (s.root, e))
        try:
            s.took_over = s._lock.took_over
            s._load()
        except BaseException:
            s._lock.release()
            raise
        s._open = True
        return s

    def _load(self):
        _sweep(self.root)
        for coll in schema.COLLECTIONS:
            folder = os.path.join(self.root, coll)
            _sweep(folder)
            docs = {}
            try:
                names = sorted(os.listdir(folder))
            except FileNotFoundError:
                names = []
            except OSError as e:
                raise HubError('store', 'cannot list %s: %s' % (folder, e))
            for n in names:
                path = os.path.join(folder, n)
                if n.lower().endswith('.json') and len(n) > 5 and os.path.isfile(path):
                    # a legacy question's text `recommended` is read as the number it stands for
                    # (migrate's rule), so schema.check does not refuse every later update of it
                    docs[n[:-5]] = schema.whole_recommended(coll, _read_json(path))[0]
            self._docs[coll] = docs
        meta_path = os.path.join(self.root, META_NAME)
        try:
            meta = _read_json(meta_path) if os.path.exists(meta_path) else {}
        except HubError:
            meta = {}       # unreadable or empty: rebuilt like a missing one (counters from ids)
        rev = meta.get('revision', 0)
        self._revision = rev if isinstance(rev, int) and not isinstance(rev, bool) else 0
        saved = meta.get('counters') if isinstance(meta.get('counters'), dict) else {}
        for coll, prefix in schema.ID_PREFIX.items():
            n = saved.get(prefix, 0)
            n = n if isinstance(n, int) and not isinstance(n, bool) else 0
            self._counters[prefix] = max([n, ID_FLOOR.get(prefix, 1) - 1]
                                         + [_id_number(prefix, d) for d in self._docs[coll]])
        self._known = self._revision

    def close(self):
        """Drop the lock and wake every waiter; later calls raise HubError store."""
        with self._mutex:
            self._open = False
            self._cond.notify_all()
        self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _check_open(self):
        if not self._open:
            raise HubError('store', 'the hub store %s is closed' % self.root)

    def check_open(self):
        """HubError store once the store is closed: for a caller that waits on wait() for
        something else (the machine lock), which a close ends with no error."""
        with self._mutex:
            self._check_open()

    # ---------------------------------------------------------------- reads
    @property
    def revision(self):
        with self._mutex:
            return self._revision

    def get(self, coll, doc_id):
        """A copy of the document, or None."""
        with self._mutex:
            self._check_open()
            d = self._docs.get(coll, {}).get(doc_id)
            return copy.deepcopy(d)

    def docs(self, coll):
        """A copy of {id: document} for coll."""
        with self._mutex:
            self._check_open()
            return copy.deepcopy(self._docs.get(coll, {}))

    def snapshot(self):
        """(revision, {collection: {id: document}}), copied under one hold of the mutex."""
        with self._mutex:
            self._check_open()
            return self._revision, copy.deepcopy(self._docs)

    def pending(self):
        with self._mutex:
            self._check_open()
            # the clock is read only when an approval can need it (a test clock counts calls)
            now = _aware(self.clock()) if self._docs.get('approvals') else None
            return pending_items(self._docs, now)

    def changes_since(self, since):
        with self._mutex:
            return self._changes(since)

    def _changes(self, since):
        if since > self._revision:
            return Changes(self._revision, [], True)     # a store behind the caller: re-read
        if since == self._revision:
            return Changes(self._revision, [], False)
        events = [e for e in self._ring if e.rev > since]
        return Changes(self._revision, events, since < self._known)

    def wait(self, since, timeout):
        """changes_since(since) once the revision differs from since (at once, with reset, when
        since is past it), or after timeout seconds (then with no events), or when the store
        closes."""
        with self._cond:
            self._cond.wait_for(lambda: self._revision != since or not self._open, timeout)
            return self._changes(since)

    # ---------------------------------------------------------------- writes
    def add(self, coll, fields):
        """A new document with the next id of coll's prefix (Q-001, S-002, ...)."""
        if coll not in schema.ID_PREFIX:
            raise HubError('invalid', 'the store does not assign %s/ ids: give the id (put)' % coll,
                           'collection')
        return self._write('add', coll, None, fields, 0)

    def put(self, coll, doc_id, fields, if_version=None):
        """Create the document or replace all its fields."""
        return self._write('put', coll, doc_id, fields, if_version)

    def update(self, coll, doc_id, changes, if_version=None):
        """Merge changes into the document's top-level fields."""
        return self._write('update', coll, doc_id, changes, if_version)

    def delete(self, coll, doc_id, if_version=None):
        return self._write('delete', coll, doc_id, None, if_version)

    def _write(self, op, coll, doc_id, fields, if_version):
        if coll not in schema.COLLECTIONS:
            raise HubError('invalid', 'there is no %s/ collection in the hub store' % coll,
                           'collection')
        if op != 'delete' and not isinstance(fields, dict):
            raise HubError('invalid', 'a document is a JSON object', 'fields')
        if if_version is not None and (isinstance(if_version, bool)
                                       or not isinstance(if_version, int) or if_version < 0):
            raise HubError('invalid', 'if_version must be a whole number, not %r' % (if_version,),
                           'if_version')
        with self._mutex:
            self._check_open()
            counters = dict(self._counters)
            if op == 'add':
                prefix = schema.ID_PREFIX[coll]
                counters[prefix] += 1
                doc_id = '%s-%03d' % (prefix, counters[prefix])
            why = schema.check_id(doc_id)
            if why:
                raise HubError('invalid', why, 'id')
            rel = '%s/%s' % (coll, doc_id)
            cur = self._docs[coll].get(doc_id)
            if cur is None:
                # NTFS names ignore case: Alpha would write over alpha.json
                same = [d for d in self._docs[coll] if d.lower() == doc_id.lower()]
                if same:
                    raise HubError('invalid', '%s differs only in case from %s/%s, and file '
                                   'names ignore case' % (rel, coll, same[0]), 'id')
            if op == 'put' and coll in schema.ID_PREFIX:
                prefix = schema.ID_PREFIX[coll]
                counters[prefix] = max(counters[prefix], _id_number(prefix, doc_id))
            if cur is None and op in ('update', 'delete'):
                raise HubError('not_found', 'there is no %s' % rel, 'id')
            if if_version is not None and _version(cur) != if_version:
                raise HubError('conflict', '%s is at version %d, not %d' % (
                    rel, _version(cur), if_version), 'if_version')
            new, warnings = None, []
            if op != 'delete':
                new = dict(cur) if op == 'update' else {}
                new.update(copy.deepcopy(fields))
                for k in ('id',) + schema.STORE_KEYS:
                    new.pop(k, None)
                new['version'] = _version(cur) + 1
                new['updated_at'] = self.clock()
                warnings = self._validate(coll, doc_id, new)
            rev = self._revision + 1
            try:
                _replace(os.path.join(self.root, META_NAME),
                         normal_text({'revision': rev, 'counters': counters}))
            except OSError as e:
                raise HubError('store', 'cannot write %s: %s' % (META_NAME, e))
            self._revision, self._counters = rev, counters
            path = os.path.join(self.root, coll, doc_id + '.json')
            try:
                if new is None:
                    _remove(path)
                else:
                    _replace(path, normal_text(new))
            except OSError as e:
                raise HubError('store', 'cannot write %s: %s (revision %d is skipped)'
                               % (rel, e, rev))
            if new is None:
                del self._docs[coll][doc_id]
            else:
                self._docs[coll][doc_id] = new
            if len(self._ring) == self._ring.maxlen:
                self._known = self._ring[0].rev
            self._ring.append(Event(rev, coll, doc_id, op, (new or {}).get('status')))
            self._cond.notify_all()
            return Result(doc_id, _version(new), rev, warnings)

    def _validate(self, coll, doc_id, new):
        """The new warn findings for writing new as coll/doc_id; HubError invalid on a schema
        error, a new hubvalidate error or a new finding whose code is in REFUSED."""
        errors = schema.check(coll, new, doc_id)
        if errors:
            raise HubError('invalid', '; '.join(f.message for f in errors), errors[0].field)
        rel = '%s/%s' % (coll, doc_id)
        names = []

        def known():
            # asked by hubvalidate only when this document's repo tag is checked, and once a
            # write: a write with no repo tag never runs known_repos (its git) under the mutex
            if not names:
                names.append(self.known_repos())
            return names[0]
        before = {_finding_key(f)
                  for f in hubvalidate.validate(self._docs, only={rel}, known_repos=known)}
        after = dict(self._docs)
        after[coll] = dict(self._docs[coll])
        after[coll][doc_id] = new
        found = [f for f in hubvalidate.validate(after, only={rel}, known_repos=known)
                 if _finding_key(f) not in before]
        refused = [f for f in found if f.level == hubvalidate.ERROR or f.code in REFUSED]
        if refused:
            raise HubError('invalid', '; '.join('%s [%s]' % (f.message, f.code) for f in refused),
                           refused[0].field)
        return [f for f in found if f.level == hubvalidate.WARN]


def _s(v):
    return v if isinstance(v, str) else ''


def _aware(text):
    """The store clock's ISO time as an aware datetime; the current time when it is not one."""
    try:
        t = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        t = None
    if t is None or t.tzinfo is None:
        return datetime.datetime.now(datetime.timezone.utc)
    return t


def approval_decided(doc, now):
    """The status ('approved' or 'denied') of an approval document the owner decided and nobody
    consumed, while it still counts at `now` (an aware datetime); else None. An approval counts
    until approvals.status_at expires it (APPROVED_TTL_S after decided_at); a denial, which never
    expires there, for the same APPROVED_TTL_S after decided_at (the PM's ruling). A consumed,
    pending or expired one, a bad time and a naive `now` give None."""
    from . import approvals as ap   # approvals imports api, which imports this module
    status = ap.status_at(doc, now)
    if status == ap.APPROVED:
        return status
    if status != ap.DENIED:
        return None
    try:
        at = datetime.datetime.fromisoformat(doc.get('decided_at'))
        if at.tzinfo is not None and at <= now < at + datetime.timedelta(
                seconds=ap.APPROVED_TTL_S):
            return status
    except (TypeError, ValueError):
        pass
    return None


def pending_items(docs, now=None):
    """[Pending] of owner input not yet handled in docs ({collection: {id: document}}), oldest
    first: questions answered or filed and not yet logged or done (a filed one's `note` is
    'filed': its asker still reads the answer), stories submitted, to_pm items sent, to-dos done
    or added by the owner (`from: you`) with no pm_ack, approvals decided and not consumed
    (approval_decided at `now`, an aware datetime; the current time when None), and skill edits
    submitted. Inbox reads are not pending. A question's and a to-do's `sender` is its stored
    `from`; a story's, a note's, an approval's and a skill edit's is '' (the api routes an
    approval by its repo and a skill edit to the kit's coordinator)."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for doc_id, d in docs.get('questions', {}).items():
        if d.get('status') in ('answered', 'filed'):
            ans = d.get('answer') if isinstance(d.get('answer'), dict) else {}
            out.append(Pending('question', doc_id, _s(d.get('title')), _s(d.get('repo')),
                               _s(ans.get('at')), _s(d.get('from')),
                               'filed' if d.get('status') == 'filed' else ''))
    for doc_id, d in docs.get('stories', {}).items():
        if d.get('status') == 'submitted':
            out.append(Pending('story', doc_id, _s(d.get('title')), _s(d.get('repo')),
                               _s(d.get('submitted_at'))))
    for doc_id, d in docs.get('to_pm', {}).items():
        if d.get('status') == 'sent':
            text = _s(d.get('text')).strip()
            out.append(Pending('to_pm', doc_id, text.splitlines()[0] if text else '',
                               _s(d.get('repo')), _s(d.get('sent_at')),
                               note=_s(d.get('kind'))))
    for doc_id, d in docs.get('todos', {}).items():
        if not d.get('pm_ack') and (d.get('status') == 'done' or d.get('from') == 'you'):
            out.append(Pending('todo', doc_id, _s(d.get('title')), _s(d.get('repo')),
                               _s(d.get('done_at')) or _s(d.get('added_at')),
                               _s(d.get('from'))))
    for doc_id, d in docs.get('approvals', {}).items():
        if approval_decided(d, now):
            out.append(Pending('approval', doc_id, _s(d.get('title')), _s(d.get('repo')),
                               _s(d.get('decided_at'))))
    for doc_id, d in docs.get('skill_edits', {}).items():
        if d.get('status') == 'submitted':
            out.append(Pending('skill_edit', doc_id, "Skill %s: the owner's edit"
                               % _s(d.get('skill')), '', _s(d.get('submitted_at'))))
    return sorted(out, key=lambda p: (p.at, p.kind, p.id))
