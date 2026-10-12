"""kitlib.hubsite.migrate: copy the earlier Action Hub's documents into the hub store.

A source is a folder of <collection>/<id>.json files: the mirror folder (kitlib.hubmirror) or an
ArtifactData out_dir export of one account's copy. Both spellings of a document are read, the
bare fields and the wrapped one, through kitlib.hubmirror.fields_of.

  migrate(root, sources, dry_run=False)  -> Result   copy the sources' documents into the store
  validate(root)                         -> [Finding] the store's findings, read only, no lock
  check(docs)                            -> [Finding] the same over {collection: {id: fields}}
  report_lines(result)                   -> [str]     the migration report
  validate_lines(findings)               -> [str]     the findings, one line each, and a summary

It only copies in. Nothing in a source is deleted, moved, rewritten or marked: a source is opened
for reading and nothing else. In the store a document is only ever created, never replaced, so a
second run finds every document there and writes nothing.

What a source document becomes:
  - A document of one of schema.COLLECTIONS that the store does not hold is written to
    <root>/<collection>/<id>.json with its own id and its own fields, as store.normal_text. It has
    no `version`, so the store reads it at version 0, exactly as it reads a mirror file (the
    store's module docstring). It is not passed through the store's write checks: a document the
    earlier hub held is kept whole even where a new write of it would be refused, and check()
    reports what is wrong with it. An inbox document that is a lock-window notice is a document
    like any other and is copied in.
  - A document the store already holds with the same fields is counted in `same`.
  - A document the store already holds with other fields is not written: the store's is kept and
    the difference is in `kept`. The store may have been written since the first run.
  - A document two sources hold with different fields, and the store does not hold, is written
    from neither: which one is right is not decided here. It is in `conflicts`, with the source
    whose time stamps are later, when they tell.
  - A file that cannot be read as a document, an id the store cannot address (schema.check_id) or
    one that differs only in case from another is in `skipped`.
  - A folder the store has no collection for (parked/, and backlog/, completed/, usage/, agents/
    of an export) is in `left`, with its document count. Nothing of it is read into the store.

The store's lock. The run opens the store the way the server does (store.Store.open) and holds it
until the last document is written: while a server holds the store the run is refused, Busy,
before anything is written. A dry run writes nothing at all: it takes the lock only when the
lock file is already there (a holder made it), and never makes the store folder.

A source may be the store folder itself (the mirror folder served in place): every document is
then already the store's and nothing is written.

Nothing here prints, parses arguments or exits.
"""
import collections
import datetime
import json
import os

from kitlib import hubmirror, hubvalidate

from . import schema
from . import store as hubstore

LOCK_WINDOW = 'lock-window'         # the `kind` of an inbox document that is a lock-window notice
# the fields whose time says how new a document is, for a conflict's `later`
STAMPS = ('updated_at', 'done_at', 'asked_at')

Left = collections.namedtuple('Left', 'source folder count')
Kept = collections.namedtuple('Kept', 'rel sources')
Conflict = collections.namedtuple('Conflict', 'rel sources later')
Skipped = collections.namedtuple('Skipped', 'source rel why')


class Result(collections.namedtuple(
        'Result', 'store dry_run sources added same kept conflicts skipped left lock_windows '
                  'findings')):
    """One run. `store` and `sources` are absolute folders. `added`, `same`: sorted
    "collection/id" lists (written, or with dry_run what would be; already in the store with the
    same fields). `kept`: [Kept], the store's differing document was kept. `conflicts`:
    [Conflict], written from no source. `skipped`: [Skipped]. `left`: [Left], folders the store
    has no collection for. `lock_windows`: the lock-window notices among added, same and kept.
    `findings`: check() over the store after the run (with dry_run, over what it would hold)."""

    @property
    def changed(self):
        """True when this run wrote a document."""
        return bool(self.added) and not self.dry_run

    @property
    def complete(self):
        """True when every document the store has a collection for is in the store (or, with
        dry_run, would be): no conflict and nothing skipped."""
        return not self.conflicts and not self.skipped


class MigrateError(Exception):
    """A run that was refused, said in one message; nothing was written."""


class Busy(MigrateError):
    """Another process (a running hub server) holds the store's lock."""


def _own(fields):
    """A document's fields without what the store or ArtifactData sets (id, version,
    updated_at): what two copies of a document are compared by."""
    return {k: v for k, v in fields.items() if k != 'id' and k not in schema.STORE_KEYS}


def _doc_files(folder):
    """{id: path} of the *.json files directly in folder."""
    out = {}
    for n in sorted(os.listdir(folder)):
        path = os.path.join(folder, n)
        if n.lower().endswith('.json') and len(n) > 5 and os.path.isfile(path):
            out[n[:-5]] = path
    return out


def _read_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def read_source(src):
    """(docs, left, skipped) of one source folder: docs is {(collection, id): fields} for the
    collections the store holds, left a [Left] for every other folder, skipped a [Skipped] for
    each file that is no document. MigrateError when src is not a folder or cannot be listed.
    The source is only read."""
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        raise MigrateError('--from %s is not a folder' % src)
    docs, left, skipped = {}, [], []
    try:
        for name in sorted(os.listdir(src)):
            folder = os.path.join(src, name)
            if not os.path.isdir(folder):
                continue
            files = _doc_files(folder)
            if name not in schema.COLLECTIONS:
                left.append(Left(src, name, len(files)))
                continue
            for doc_id, path in sorted(files.items()):
                rel = '%s/%s' % (name, doc_id)
                why = schema.check_id(doc_id)
                if why:
                    skipped.append(Skipped(src, rel, why))
                    continue
                try:
                    docs[(name, doc_id)] = hubmirror.fields_of(name, _read_json(path))
                except (OSError, ValueError, hubmirror.Refused) as e:
                    skipped.append(Skipped(src, rel, 'cannot be read as a document: %s' % e))
    except OSError as e:
        raise MigrateError('cannot list %s: %s' % (src, e))
    return docs, left, skipped


def store_docs(root):
    """{collection: {id: document}} of the store folder `root`, every schema.COLLECTIONS key
    present, read from its files with no lock ({} each for a missing folder). MigrateError for a
    document that cannot be read or is not an object."""
    out = {}
    for coll in schema.COLLECTIONS:
        folder = os.path.join(root, coll)
        docs = {}
        try:
            files = _doc_files(folder) if os.path.isdir(folder) else {}
            for doc_id, path in files.items():
                docs[doc_id] = _read_json(path)
                if not isinstance(docs[doc_id], dict):
                    raise ValueError('not a JSON object')
        except (OSError, ValueError) as e:
            raise MigrateError('cannot read the hub store %s: %s/: %s' % (root, coll, e))
        out[coll] = docs
    return out


def check(docs):
    """The findings for docs ({collection: {id: document}}): schema.check's errors for each
    document, then kitlib.hubvalidate.validate's, sorted by document, code and field."""
    out = []
    for coll in schema.COLLECTIONS:
        for doc_id, d in sorted(docs.get(coll, {}).items()):
            out.extend(schema.check(coll, d, doc_id))
    out.extend(hubvalidate.validate(docs))
    return sorted(out, key=lambda f: (f.rel, f.code, f.field, f.message))


def validate(root):
    """check() over the store folder `root` as it is on disk. Read only: no lock is taken and
    nothing is made, so it may run beside a server. MigrateError for a folder that does not
    exist or a document that cannot be read."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise MigrateError('the hub store %s is not a folder' % root)
    return check(store_docs(root))


def _when(fields):
    """The latest time among a document's STAMPS and its answer's `at`, or None."""
    answer = fields.get('answer') if isinstance(fields.get('answer'), dict) else {}
    best = None
    for text in [fields.get(k) for k in STAMPS] + [answer.get('at')]:
        if not isinstance(text, str):
            continue
        try:
            t = datetime.datetime.fromisoformat(text.strip())
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        if best is None or t > best:
            best = t
    return best


def _later(versions):
    """The source of the one version whose time is after every other's, or None when a version
    has no time or two share the latest."""
    times = [(_when(f), s) for s, f in versions]
    if any(t is None for t, _ in times):
        return None
    top = max(t for t, _ in times)
    best = [s for t, s in times if t == top]
    return best[0] if len(best) == 1 else None


def _create(path, text):
    """Write a new file at path, never over one: the text goes to a temp file beside it (one the
    store sweeps at its next open, were this to die here), then os.rename, which fails on
    Windows when path exists."""
    folder, name = os.path.split(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, '.%s.%d%s' % (name, os.getpid(), hubstore.TMP_SUFFIX))
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    try:
        os.rename(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _hold(root):
    """The opened Store at root: the lock, held until it is closed."""
    try:
        return hubstore.Store.open(root)
    except hubstore.StoreBusy as e:
        raise Busy('%s: a hub server has this store open; stop it first, then migrate (%s)'
                   % (root, e.message))
    except hubstore.HubError as e:
        raise MigrateError(e.message)


def migrate(root, sources, dry_run=False):
    """Copy the documents of `sources` (folders, read in the order given) into the store folder
    `root`; see the module docstring. With dry_run nothing is written and the Result says what
    would be. MigrateError, before anything is written, for no source, a source that is not a
    folder or a store that cannot be read; Busy when a server holds the store."""
    root = os.path.abspath(root)
    sources = [os.path.abspath(s) for s in sources]
    if not sources:
        raise MigrateError('give at least one source folder (--from)')
    read = [(s,) + read_source(s) for s in sources]
    held = None
    if not dry_run or os.path.exists(os.path.join(root, hubstore.LOCK_NAME)):
        held = _hold(root)
    try:
        have = store_docs(root)
        spelling = {(c, i.lower()): i for c in have for i in have[c]}
        versions, left, skipped, notices = {}, [], [], set()
        for src, docs, src_left, src_skipped in read:
            left.extend(src_left)
            skipped.extend(src_skipped)
            for (coll, doc_id), fields in sorted(docs.items()):
                rel = '%s/%s' % (coll, doc_id)
                first = spelling.setdefault((coll, doc_id.lower()), doc_id)
                if first != doc_id:
                    skipped.append(Skipped(src, rel, 'differs only in case from %s/%s, and file '
                                                     'names ignore case' % (coll, first)))
                    continue
                versions.setdefault((coll, doc_id), []).append((src, fields))
        added, same, kept, conflicts = [], [], [], []
        for (coll, doc_id), found in sorted(versions.items()):
            rel = '%s/%s' % (coll, doc_id)
            cur = have[coll].get(doc_id)
            if cur is not None:
                differs = [s for s, f in found if _own(f) != _own(cur)]
                if differs:
                    kept.append(Kept(rel, differs))
                else:
                    same.append(rel)
            else:
                distinct = []
                for s, f in found:
                    if not any(_own(f) == _own(g) for _, g in distinct):
                        distinct.append((s, f))
                if len(distinct) > 1:
                    conflicts.append(Conflict(rel, [s for s, _ in distinct], _later(distinct)))
                    continue
                src, fields = distinct[0]
                if not dry_run:
                    try:
                        _create(os.path.join(root, coll, doc_id + '.json'),
                                hubstore.normal_text(fields))
                    except OSError as e:
                        skipped.append(Skipped(src, rel, 'cannot be written to the store: %s' % e))
                        continue
                have[coll][doc_id] = fields
                added.append(rel)
            if coll == 'inbox' and any(f.get('kind') == LOCK_WINDOW for _, f in found):
                notices.add(rel)
    finally:
        if held is not None:
            held.close()
    return Result(root, bool(dry_run), sources, added, same, kept, conflicts,
                  sorted(skipped), sorted(left), sorted(notices),
                  check(have if dry_run else store_docs(root)))


def validate_lines(findings):
    """The findings as lines, kitlib.hubvalidate.format_line each, then one summary line; one
    line saying the store is clean when there is none."""
    if not findings:
        return ['VALIDATE clean: no finding']
    return (['VALIDATE %s' % hubvalidate.format_line(f) for f in findings]
            + ['VALIDATE %s' % hubvalidate.summary(findings)])


def _count(n, word='document'):
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def report_lines(result):
    """The migration report: the store and sources, a count line per collection, one line per
    document or folder that is not in the store and why, the validation lines, and a last line
    that says what the run changed."""
    r = result
    lines = ['hub store: %s%s' % (r.store, ' (dry run: nothing is written)' if r.dry_run else '')]
    lines.extend('source: %s' % s for s in r.sources)
    wrote = 'would import' if r.dry_run else 'imported'
    for coll in schema.COLLECTIONS:
        def n(rels):
            return sum(1 for x in rels if x.split('/')[0] == coll)
        nums = (n(r.added), n(r.same), n(x.rel for x in r.kept), n(x.rel for x in r.conflicts))
        if any(nums):
            lines.append('%s/: %s %d, already in the store %d, store differs and is kept %d, '
                         'conflict %d' % ((coll, wrote) + nums))
    if r.lock_windows:
        lines.append('inbox/: %s in the store, kept as they are in the source'
                     % _count(len(r.lock_windows), 'lock-window notice'))
    for k in r.kept:
        lines.append('KEPT %s: the store holds other fields than %s; the store is unchanged'
                     % (k.rel, ', '.join(k.sources)))
    for c in r.conflicts:
        lines.append('CONFLICT %s: %s hold different fields; imported from neither (%s)' % (
            c.rel, ' and '.join(c.sources),
            'the later by its time stamps: %s' % c.later if c.later
            else 'the time stamps do not tell which is later'))
    for s in r.skipped:
        lines.append('SKIPPED %s in %s: %s' % (s.rel, s.source, s.why))
    for x in r.left:
        lines.append('LEFT ALONE %s/ in %s: %s; the store has no %s/ collection' % (
            x.folder, x.source, _count(x.count), x.folder))
    lines.extend(validate_lines(r.findings))
    if not r.complete:
        lines.append('NOT COMPLETE: %s not imported (CONFLICT and SKIPPED above); the sources '
                     'are unchanged' % _count(len(r.conflicts) + len(r.skipped)))
    if r.dry_run:
        lines.append('dry run: %s would be imported; nothing was written' % _count(len(r.added)))
    elif r.added:
        lines.append('imported %s; no source was changed' % _count(len(r.added)))
    else:
        lines.append('nothing changed: the store already holds every document that can be '
                     'imported; nothing was written')
    return lines
