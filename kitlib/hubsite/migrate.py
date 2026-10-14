"""kitlib.hubsite.migrate: copy the earlier Action Hub's documents into the hub store.

A source is a folder of <collection>/<id>.json files: the old hub's data folder (hub/ in the PM data
worktree) or an ArtifactData out_dir export of one account's copy. Both spellings of a document
are read, the bare fields and the wrapped one, through fields_of.

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
  - One field is not copied as it is: a question's `recommended` that is text is written as the
    number it stands for, and is in `turned`. Text that is exactly the `label` of one of the
    question's options (the same characters, case and spaces included; an option that is an
    object) stands for that option's position, counted from 0 as hubvalidate's options check
    and the page count it. Text that is a whole number (one to nine of the digits 0-9 and
    nothing else) stands for that number, in range of the options or not. These stand for
    nothing, and the document is copied as it is: text that is not a whole number and is no
    option's label or the label of two; a whole number that is also an option's label, unless
    it is the label of one option only and that option is at the position the number says;
    more than nine digits; and the empty text. The
    store refuses every update of a question whose `recommended` is text (schema.check), so such
    a question could not be answered. No other field and no other text is changed. Two copies
    are compared with this text read as the number on both sides: a store document that holds
    the number, or the text, is the same document as a source that holds the text. The store's
    file is never rewritten, so one that already holds the text keeps it and check() reports it.
  - A document the store already holds with the same fields is counted in `same`.
  - A document the store already holds with other fields is never written over. When the store's
    has a `version` (a server wrote it since), the store's is the newer truth: the difference is
    in `kept` and the run is complete. When it has none (no server ever wrote it: it is a copy
    from a source, or a mirror file where the mirror folder is the store folder), it is one
    source against another: it is in `conflicts` (Conflict.store True) and the run is not
    complete.
  - A document two sources hold with different fields, and the store does not hold, is written
    from neither: which one is right is not decided here. It is in `conflicts`. A conflict names
    the copy whose time stamps are later, when they tell.
  - A file that cannot be read as a document or written as UTF-8, an id the store cannot address
    (schema.check_id), one that differs only in case from another, or a file whose own `id`,
    `doc_id` or `collection` names another place than its path is in `skipped`.
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
import re

from kitlib import hubvalidate

from . import schema
from . import store as hubstore

# A wrapped out_dir document: a `data` object plus only these keys (fields_of).
WRAPPER_KEYS = frozenset(('data', 'id', 'version', 'doc_id', 'collection'))
LOCK_WINDOW = 'lock-window'         # the `kind` of an inbox document that is a lock-window notice
# the fields whose time says how new a document is, for a conflict's `later`
STAMPS = ('updated_at', 'done_at', 'asked_at')

Left = collections.namedtuple('Left', 'source folder count')
Kept = collections.namedtuple('Kept', 'rel sources')
# store False: `sources` hold different fields and the store has no such document. store True:
# the store holds one no server has written, and `sources` are those that differ from it.
# later: the source whose time stamps are the latest, the store folder for the store's copy, or
# None when the stamps do not tell.
Conflict = collections.namedtuple('Conflict', 'rel sources later store')
Skipped = collections.namedtuple('Skipped', 'source rel why')
# a question written with its `recommended`, the text `old`, as the number `new`; label: `old`
# is the label of the option at position `new`, not a number written as text
Turned = collections.namedtuple('Turned', 'rel old new label', defaults=(False,))
_WHOLE = re.compile(r'[0-9]{1,9}')  # a whole number small enough to be a position; more digits
_DIGITS = re.compile(r'[0-9]+')     # than that are kept as text


class Result(collections.namedtuple(
        'Result', 'store dry_run sources added same kept conflicts skipped left lock_windows '
                  'findings empty unchecked turned')):
    """One run. `store` and `sources` are absolute folders. `added`, `same`: sorted
    "collection/id" lists (written, or with dry_run what would be; already in the store with the
    same fields). `kept`: [Kept], the document a server wrote was kept. `conflicts`:
    [Conflict], written from no source. `skipped`: [Skipped]. `left`: [Left], folders the store
    has no collection for. `lock_windows`: the lock-window notices among added, same, kept and
    the store's side of a conflict. `findings`: check() over the store after the run (with
    dry_run, over what it would hold). `empty`: the sources that gave no document of a
    collection the store has. `unchecked`: why `findings` could not be made ('' when it was).
    `turned`: [Turned], the documents of `added` whose `recommended` was text."""

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


def _whole(coll, fields):
    """(fields, old, label): a question whose `recommended` is text that stands for a number
    (the module docstring) is given back as a copy with the number; old is that text and label
    is True when it was an option's label. Any other document is given back as it is, with old
    None."""
    old = fields.get('recommended') if coll == 'questions' else None
    if not isinstance(old, str) or not old:
        return fields, None, False              # '' names no option, whatever an option's label is
    options = fields.get('options')
    at = [i for i, o in enumerate(options if isinstance(options, list) else [])
          if isinstance(o, dict) and o.get('label') == old]
    if _WHOLE.fullmatch(old):
        if at and at != [int(old)]:
            return fields, None, False          # the number, or one of those options: it does not say
        return dict(fields, recommended=int(old)), old, bool(at)
    if len(at) != 1 or _DIGITS.fullmatch(old):  # too long to be a number here: left as it is
        return fields, None, False
    return dict(fields, recommended=at[0]), old, True


def _doc_files(folder):
    """{id: path} of the *.json files directly in folder."""
    out = {}
    for n in sorted(os.listdir(folder)):
        path = os.path.join(folder, n)
        if n.lower().endswith('.json') and len(n) > 5 and os.path.isfile(path):
            out[n[:-5]] = path
    return out


class NotADocument(ValueError):
    """A source file that cannot be read as one document; the message says why."""


def fields_of(collection, obj):
    """The document's own fields from one ArtifactData out_dir file or one old-hub data file. An
    object whose `data` is an object and whose other keys are all in WRAPPER_KEYS (none at all
    included) is a wrapped document and gives its `data`. An object with a `data` object beside
    any other key is refused (NotADocument): it is either a wrapper with keys this module does not
    know or a document with a `data` field, and reading the wrong one would store nested garbage
    (no hub collection has a `data` field, docs/action-hub.md). Otherwise the object is the
    fields. `id` and `version` are never kept: a document's id is its file name."""
    if not isinstance(obj, dict):
        raise NotADocument('a %s document is not a JSON object' % collection)
    if isinstance(obj.get('data'), dict):
        extra = sorted(set(obj) - WRAPPER_KEYS)
        if extra:
            raise NotADocument(
                'a %s document has a `data` object beside %s: cannot tell a wrapped ArtifactData '
                'document from one with a `data` field, so it was not read. If these are '
                "ArtifactData's own keys, add them to kitlib.hubsite.migrate.WRAPPER_KEYS"
                % (collection, ', '.join(extra)))
        out = dict(obj['data'])
    else:
        out = dict(obj)
    out.pop('version', None)
    out.pop('id', None)
    return out


def _read_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def _misplaced(coll, doc_id, obj):
    """Why the file's own keys name another document than its path coll/doc_id, or ''. A wrapped
    document's `id`, `doc_id` and `collection`, the `id` inside its `data`, and a bare document's
    `id` are not stored (fields_of): one that disagrees with the path would be dropped
    unseen. A whole number is read as its text: the `id` 7 names the file 7.json."""
    if not isinstance(obj, dict):
        return ''
    named = [('id', obj, doc_id)]
    if isinstance(obj.get('data'), dict):
        named += [('doc_id', obj, doc_id), ('collection', obj, coll),
                  ('data.id', obj['data'], doc_id)]
    for name, holder, want in named:
        key = name.split('.')[-1]
        if key not in holder:
            continue
        got = holder[key]
        if isinstance(got, int) and not isinstance(got, bool):
            got = str(got)
        if got != want:
            return ('its `%s` is %r, not %r as its path says: which document it is cannot be '
                    'told' % (name, holder[key], want))
    return ''


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
                    obj = _read_json(path)
                    fields = fields_of(name, obj)
                except (OSError, ValueError) as e:
                    skipped.append(Skipped(src, rel, 'cannot be read as a document: %s' % e))
                    continue
                why = _misplaced(name, doc_id, obj)
                if why:
                    skipped.append(Skipped(src, rel, why))
                    continue
                docs[(name, doc_id)] = fields
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
    document, then kitlib.hubvalidate.validate's, sorted by document, code and field. With no
    repos/ document there is nothing to judge a `repo` tag against: `repos` is then left out, so
    validate gives its one note that the tags were not checked, not a warning per tagged
    document."""
    out = []
    for coll in schema.COLLECTIONS:
        for doc_id, d in sorted(docs.get(coll, {}).items()):
            out.extend(schema.check(coll, d, doc_id))
    if not docs.get('repos'):
        docs = {k: v for k, v in docs.items() if k != 'repos'}
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
    Windows when path exists. OSError, or ValueError for a text that is not UTF-8 (a lone
    surrogate), with the temp file removed."""
    folder, name = os.path.split(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, '.%s.%d%s' % (name, os.getpid(), hubstore.TMP_SUFFIX))
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.rename(tmp, path)
    except (OSError, ValueError):
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
        added, same, kept, conflicts, turned = [], [], [], [], []
        for (coll, doc_id), found in sorted(versions.items()):
            rel = '%s/%s' % (coll, doc_id)
            cur = have[coll].get(doc_id)
            was = {s: _whole(coll, f)[1:] for s, f in found}
            found = [(s, _whole(coll, f)[0]) for s, f in found]
            if cur is not None:
                mine = _own(_whole(coll, cur)[0])       # compared as a source is: text as number
                differs = [(s, f) for s, f in found if _own(f) != mine]
                if not differs:
                    same.append(rel)
                elif 'version' in cur:          # a server wrote it since: the store's is newer
                    kept.append(Kept(rel, [s for s, _ in differs]))
                else:                           # never written by a server: source against source
                    conflicts.append(Conflict(rel, [s for s, _ in differs],
                                              _later([(root, cur)] + differs), True))
            else:
                distinct = []
                for s, f in found:
                    if not any(_own(f) == _own(g) for _, g in distinct):
                        distinct.append((s, f))
                if len(distinct) > 1:
                    conflicts.append(Conflict(rel, [s for s, _ in distinct], _later(distinct),
                                              False))
                    continue
                src, fields = distinct[0]
                try:
                    text = hubstore.normal_text(fields)
                    text.encode('utf-8')        # a dry run names what a run could not write
                    if not dry_run:
                        _create(os.path.join(root, coll, doc_id + '.json'), text)
                except (OSError, ValueError) as e:
                    skipped.append(Skipped(src, rel, 'cannot be written to the store: %s' % e))
                    continue
                have[coll][doc_id] = fields
                added.append(rel)
                if was[src][0] is not None:
                    turned.append(Turned(rel, was[src][0], fields['recommended'], was[src][1]))
            if coll == 'inbox' and LOCK_WINDOW in (
                    [f.get('kind') for _, f in found] + [(cur or {}).get('kind')]):
                notices.add(rel)
    finally:
        if held is not None:
            held.close()
    findings, unchecked = [], ''
    try:        # the documents are written by now: a store that cannot be read back is said so
        findings = check(have if dry_run else store_docs(root))
    except MigrateError as e:
        unchecked = str(e)
    empty = [src for src, docs, _, src_skipped in read if not docs and not src_skipped]
    return Result(root, bool(dry_run), sources, added, same, kept, conflicts,
                  sorted(skipped), sorted(left), sorted(notices), findings, empty, unchecked,
                  turned)


def validate_lines(findings):
    """The findings as lines, kitlib.hubvalidate.format_line each, then one summary line; one
    line saying the store is clean when there is none. After the findings, one line per document
    that has a schema.check error: the store refuses a write that leaves such an error in the
    document (store.Store, "check"), so the api cannot change it until its file is fixed."""
    if not findings:
        return ['VALIDATE clean: no finding']
    lines = ['VALIDATE %s' % hubvalidate.format_line(f) for f in findings]
    for rel in sorted({f.rel for f in findings if f.code == schema.CODE}):
        lines.append('VALIDATE %s: the api will refuse every update of this document until it is '
                     'fixed; fix its file by hand with the server stopped (no api route repairs '
                     'it)' % rel)
    return lines + ['VALIDATE %s' % hubvalidate.summary(findings)]


def _count(n, word='document'):
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def report_lines(result):
    """The migration report: the store and sources, a count line per collection, one line per
    document or folder that is not in the store and why, one per question whose `recommended`
    was written as the number, the validation lines, and a last line that says what the run
    changed."""
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
        if c.later is None:
            later = 'the time stamps do not tell which is later'
        elif c.store and c.later == r.store:
            later = "the later by its time stamps: the store's copy"
        else:
            later = 'the later by its time stamps: %s' % c.later
        if c.store:
            lines.append(
                "CONFLICT %s: the store's copy, which no server has written, and %s hold "
                "different fields; the store's copy is kept and nothing was imported (%s). To "
                "keep the store's copy there is nothing to do; a run with that source reports "
                "this again. To take a source's copy: with the server stopped, move the file "
                "%s.json out of the store folder (where the store folder is the mirror folder, "
                "that file is the mirror's own file), then run again with the chosen source alone"
                % (c.rel, ' and '.join(c.sources), later, c.rel))
        else:
            lines.append(
                'CONFLICT %s: %s hold different fields; imported from neither (%s). To resolve '
                'without editing a source: run again with the chosen source alone'
                % (c.rel, ' and '.join(c.sources), later))
    for t in r.turned:
        wrote = 'would be imported' if r.dry_run else 'imported'
        if t.label:
            lines.append('RECOMMENDED %s: its `recommended` is the text %r, the label of the '
                         'option at position %d (the first is 0); %s as the number %d; nothing '
                         'else in the document is changed' % (t.rel, t.old, t.new, wrote, t.new))
        else:
            lines.append('RECOMMENDED %s: its `recommended` is the text %r; %s as the number %d; '
                         'nothing else in the document is changed' % (t.rel, t.old, wrote, t.new))
    for s in r.skipped:
        lines.append('SKIPPED %s in %s: %s' % (s.rel, s.source, s.why))
    for x in r.left:
        lines.append('LEFT ALONE %s/ in %s: %s; the store has no %s/ collection' % (
            x.folder, x.source, _count(x.count), x.folder))
    for src in r.empty:
        lines.append('NO DOCUMENT in %s: it holds no document of a collection the store has'
                     % src)
    if r.unchecked:
        lines.append('VALIDATE skipped: %s' % r.unchecked)
    else:
        lines.extend(validate_lines(r.findings))
    if not r.complete:
        lines.append('NOT COMPLETE: %s not imported (CONFLICT and SKIPPED above); the sources '
                     'are unchanged' % _count(len(r.conflicts) + len(r.skipped)))
    if r.dry_run:
        lines.append('dry run: %s would be imported; nothing was written' % _count(len(r.added)))
    elif r.added:
        lines.append('imported %s; no source was changed' % _count(len(r.added)))
    elif not r.complete:
        lines.append('nothing was imported: see NOT COMPLETE above; nothing was written')
    elif not (r.same or r.kept):
        lines.append('nothing to import: no source holds a document of a collection the store '
                     'has; nothing was written')
    else:
        lines.append('nothing changed: the store already holds every document that can be '
                     'imported; nothing was written')
    return lines
