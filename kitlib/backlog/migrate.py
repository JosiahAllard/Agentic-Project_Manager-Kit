"""`backlog.py migrate`: the records on main become a new store's first commit (docs/backlog-store.md
"Migration", build step 4).

`migrate()` reads `<rel>/*.md` in the tree of one commit of main (the source, normally the
remote's main after a fetch: `main_tip`) and publishes them as the ROOT commit of a `ref` or
`branch` store, whose message cites the source commit. It never writes main, a working tree or
an index, and it only ever starts a store: a store that exists already, or one another writer
creates while it runs, is refused and left as it is.

On the way some one-off steps for older record shapes run (each is a no-op on records that do
not have the key):

  tombstones  a record with `closed:` or `retired:` and no `state:` is not copied (its id stays
              checkable in main's history)
  migrated    the header key is dropped
  reref       the header key is dropped
  anchor,     each line moves out of the header into the end of the body, as `- <key>: <value>`
  artefact    in header order

Every other record is copied byte for byte. Files under `<rel>/` that are not `*.md` (and
sub-folders) are not records: they are named and not copied. An unreadable record stops the
migration before anything is written; FAIL findings (`check`'s meaning, without the ledger) are
reported and do not stop it - the copy is faithful, and fixing them is ordinary store writes.
"""
from kitlib.git import batch, cmd, refs

from . import check as checkmod
from . import record as rmod
from . import store as smod
from .errors import Failure, Refusal, Usage

DROPPED_KEYS = ('migrated', 'reref')
FOLDED_KEYS = ('artefact', 'anchor')
TOMBSTONE_KEYS = ('closed', 'retired')
FOLD_INTRO = 'Moved from the header at migration:'
FETCH_TIMEOUT = 300


class Plan(object):
    """What a migration copies: `writes` {stem: text}, and what it left out or changed:
    `skipped` paths that are not records, `tombstones` {key: [stems]}, `stripped` {key: [stems]},
    `folded` [stems]."""

    def __init__(self):
        self.writes = {}
        self.skipped = []
        self.tombstones = {}
        self.stripped = {}
        self.folded = []


class Result(object):
    def __init__(self, src, label, plan, findings, message, tip):
        self.src, self.label, self.plan = src, label, plan
        self.findings, self.message, self.tip = findings, message, tip


def main_tip(root, remote, main_branch, env=None):
    """`(label, sha)` of main as the migration reads it: the remote's main (asked with ls-remote,
    its objects fetched without writing a ref), or the local main when there is no remote."""
    if not remote:
        sha, why = refs.resolve('refs/heads/%s' % main_branch, root)
        if not sha:
            raise Failure('cannot read %s: %s' % (main_branch, why))
        return main_branch, sha
    sha, why = refs.ls_remote(remote, 'refs/heads/%s' % main_branch, root, timeout=FETCH_TIMEOUT)
    if sha is None:
        raise Failure('cannot read %s/%s: %s' % (remote, main_branch, why))
    if not sha:
        raise Failure('%s has no branch %s' % (remote, main_branch))
    if not refs.resolve(sha, root)[0]:
        # The empty --refmap keeps git from moving refs/remotes/<remote>/<main> on the way.
        r = cmd.call(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--refmap=',
                      remote, 'refs/heads/%s' % main_branch], root, timeout=FETCH_TIMEOUT, env=env)
        if not refs.resolve(sha, root)[0]:
            raise Failure('cannot fetch %s/%s (%s): %s' % (remote, main_branch, sha[:10],
                                                            r.err or 'exit %s' % r.rc))
    return '%s/%s' % (remote, main_branch), sha


def fold_and_strip(text):
    """`(text, dropped keys, [(key, value)] folded)`: `text` with the DROPPED_KEYS header lines
    removed and the FOLDED_KEYS lines moved to the end of the body. Unchanged bytes when the
    header has none of them; the lines that stay keep their own line endings."""
    parts = text.split('\n')
    end = next((i for i, line in enumerate(parts) if not line.strip()), len(parts))
    kept, dropped, folded = [], [], []
    for line in parts[:end]:
        key, sep, value = line.partition(': ')
        if sep and key in DROPPED_KEYS:
            dropped.append(key)
        elif sep and key in FOLDED_KEYS:
            folded.append((key, value.rstrip('\r')))
        else:
            kept.append(line)
    if not dropped and not folded:
        return text, [], []
    if not folded:
        return '\n'.join(kept + parts[end:]), dropped, []
    nl = '\r\n' if '\r\n' in text else '\n'
    gap = parts[end] if end < len(parts) else ''
    body = '\n'.join(parts[end + 1:])
    if body.strip():
        body = body + ('' if body.endswith('\n') else nl) + nl
    else:
        body = ''
    block = nl.join([FOLD_INTRO, ''] + ['- %s: %s' % kv for kv in folded]) + nl
    return '\n'.join(kept) + '\n' + gap + '\n' + body + block, dropped, folded


def _entries(root, rev, rel):
    """`[(name, type, sha)]` directly under `rel` in the tree of `rev`."""
    out, why = cmd.run(['ls-tree', '-z', '--full-tree', rev, '--', rel + '/'], root)
    if out is None:
        raise Failure('`git ls-tree` failed: %s' % why)
    entries = []
    for item in out.split(b'\0'):
        if not item:
            continue
        meta, _, path = item.partition(b'\t')
        parts = meta.decode('ascii', 'replace').split()
        path = path.decode('utf-8', 'replace')
        if len(parts) == 3 and path.startswith(rel + '/'):
            entries.append((path[len(rel) + 1:], parts[1], parts[2]))
    return entries


def plan_migration(root, rev, rel):
    """The Plan for `<rel>/*.md` at `rev`; Failure when there is no record to copy or a record
    cannot be read."""
    entries = _entries(root, rev, rel)
    if not entries:
        raise Failure('no %s/ folder at %s: nothing to migrate' % (rel, rev[:10]))
    plan = Plan()
    md = []
    for name, kind, sha in entries:
        if kind == 'blob' and name.endswith('.md') and len(name) > 3:
            md.append((name[:-3], sha))
        else:
            plan.skipped.append('%s/%s%s' % (rel, name, '/' if kind == 'tree' else ''))
    plan.skipped.sort()
    blobs, why = batch.cat_file([sha for _, sha in md], root)
    if blobs is None:
        raise Failure('cannot read the records at %s: %s' % (rev[:10], why))
    unreadable = []
    for stem, sha in md:
        got = blobs.get(sha)
        path = '%s/%s.md' % (rel, stem)
        if got is None:
            unreadable.append('%s (no blob %s)' % (path, sha[:10]))
            continue
        rec = smod.parse_blob(stem, got[1], path)
        if not rec.readable:
            unreadable.append('%s (%s)' % (path, rec.structural[0].message))
            continue
        if any(rec.has(k) for k in TOMBSTONE_KEYS) and not rec.has('state'):
            key = next(k for k in TOMBSTONE_KEYS if rec.has(k))
            plan.tombstones.setdefault(key, []).append(stem)
            continue
        raw = got[1].decode('utf-8')
        text, dropped, folded = fold_and_strip(rec.text)
        if text == rec.text:
            text = raw                      # byte for byte, a byte-order mark included
        for key in sorted(set(dropped)):
            plan.stripped.setdefault(key, []).append(stem)
        if folded:
            plan.folded.append(stem)
        plan.writes[stem] = text
    if unreadable:
        raise Failure('%d record(s) under %s/ at %s cannot be read, so nothing was written: %s'
                      % (len(unreadable), rel, rev[:10], '; '.join(unreadable)))
    if not plan.writes:
        raise Failure('no live record under %s/ at %s: nothing to migrate' % (rel, rev[:10]))
    for stems in list(plan.tombstones.values()) + list(plan.stripped.values()):
        stems.sort()
    plan.folded.sort()
    return plan


def _count(stems_by_key):
    return ', '.join('%s %d' % (k, len(v)) for k, v in sorted(stems_by_key.items()))


def summary_lines(plan):
    """The lines that say what the migration copied, dropped and changed."""
    tombs = sum(len(v) for v in plan.tombstones.values())
    lines = ['Copied: %d record(s)' % len(plan.writes)]
    lines.append('Dropped: %d tombstone(s)%s; their ids stay in main\'s history'
                 % (tombs, ' (%s)' % _count(plan.tombstones) if tombs else ''))
    lines.append('Stripped: %s' % (_count(plan.stripped) + ' record(s)' if plan.stripped
                                   else 'no `migrated` or `reref` key'))
    lines.append('Folded: anchor/artefact into the body of %d record(s)'
                 % len(plan.folded))
    if plan.skipped:
        lines.append('Not records, not copied: %s' % ', '.join(plan.skipped))
    return lines


def message(plan, rel, label, src, session=None):
    lines = ['migrate: %s/*.md at %s %s' % (rel, label, src), '',
             'Source: %s/ at %s %s; the old records\' history stays in main\'s log there'
             % (rel, label, src)]
    lines.extend(summary_lines(plan))
    lines.extend(['', 'Session: %s' % (session or '-')])
    return '\n'.join(lines) + '\n'


def _refuse_existing(backend):
    """Refusal when the target store exists already, on the remote or as the local ref."""
    if backend.remote:
        tip, why = refs.ls_remote(backend.remote, backend.remote_ref, backend.root,
                                  timeout=FETCH_TIMEOUT)
        if tip is None:
            raise Failure('cannot read %s on %s: %s' % (backend.remote_ref, backend.remote, why))
        if tip:
            raise Refusal('the backlog store already exists (%s on %s at %s): migrate only starts '
                          'a new store; nothing was written'
                          % (backend.remote_ref, backend.remote, tip[:10]))
    local, _ = refs.resolve(backend.local_ref, backend.root)
    if local:
        raise Refusal('the backlog store already exists locally (%s at %s): migrate only starts '
                      'a new store; nothing was written' % (backend.local_ref, local[:10]))


def migrate(backend, rules, root, rev, rel, dry_run=False, label=None, session=None):
    """Copy `<rel>/*.md` at `rev` into the new store `backend` (a RefBackend) as its root commit.
    A Result; `tip` None on a dry run, which writes nothing."""
    if not isinstance(backend, smod.RefBackend):
        raise Usage('migrate copies into a `ref` or `branch` store, and the target is %s; name '
                    'one with `--to ref` or `--to branch`' % backend.describe())
    rel = rel.strip('/')
    src, why = refs.resolve(rev, root)
    if not src:
        raise Failure('cannot read the source %s: %s' % (rev, why))
    label = label or rev
    _refuse_existing(backend)
    plan = plan_migration(root, src, rel)
    records = [rmod.parse(backend.path_of(stem), text.lstrip('\ufeff'), stem)
               for stem, text in plan.writes.items()]
    findings = checkmod.fail_findings(smod.Snapshot(records, None, backend.describe(), rules))
    msg = message(plan, rel, label, src, session)
    if dry_run:
        return Result(src, label, plan, findings, msg, None)
    empty = smod.Snapshot([], None, backend.describe(), rules)
    try:
        tip = backend.publish(empty, plan.writes, msg)
    except smod.Conflict as exc:
        raise Refusal('the backlog store already exists: another writer created %s while this '
                      'ran (%s); nothing of this migration was written'
                      % (backend.describe(), ' '.join(str(exc).split())))
    return Result(src, label, plan, findings, msg, tip)
