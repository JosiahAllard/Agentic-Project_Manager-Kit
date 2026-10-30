"""Where records live and how a write reaches them (docs/backlog-store.md).

Three backends behind one interface (`load`, `publish`, `history`, `removal`, `touched`,
`landed`):

  ref     `backlog/<ID>.md` on `refs/backlog/main`, a ref with no history shared with main
  branch  the same on a visible `backlog` branch
  main    `<dir>/<ID>.md` files in a linked worktree (today's behaviour; landing carries them);
          in the PM data branch's worktree each write also commits its own record paths

A write (`Store.write`) on a ref store is a compare-and-swap loop. Each attempt loads a fresh
snapshot (fetching first), plans the change against it, validates the result, builds a commit
whose parent is exactly the snapshot's tip, and publishes it only if the store still points at
that tip: `push --force-with-lease=<ref>:<tip>` with a remote, `update-ref <ref> <new> <tip>`
without one. A lost race re-runs the whole attempt, plan included, so a write never lands on a
store it did not validate against. A write that fails validation publishes nothing. A ref store
never touches a working tree or an index other than its own temporary one. The main backend has
no compare-and-swap (check the bytes, then write), so it writes only in a linked worktree.
"""
import contextlib
import contextvars
import os
import random
import re
import shutil
import tempfile
import time

from kitlib import pmdata
from kitlib.config import user as kuser
from kitlib.fs import replace as fsreplace
from kitlib.git import batch, cmd, refs
from kitlib.git import repo as gitrepo

from . import record as rmod
from .errors import Busy, Dirty, Failure, IndexStale, Overtaken, Refusal, Uncommitted, Usage

STORE_REF = 'refs/backlog/main'
STORE_DIR = 'backlog'
BRANCH = 'backlog'
ATTEMPTS = 5
INDEX_WAIT = 2.0        # seconds a PM data worktree write waits for git's index lock (Busy after)
REPLACE_WAIT = 1.0      # seconds it retries a refused move of its index over the index (Windows)
FETCH_TIMEOUT = 60
MAX_ID_LEN = 64
WINDOWS_DEVICE_NAMES = frozenset(['CON', 'PRN', 'AUX', 'NUL'] + ['COM%d' % i for i in range(1, 10)]
                                 + ['LPT%d' % i for i in range(1, 10)])


class Conflict(Exception):
    """The store moved between load and publish: the attempt is re-run."""


# --- the snapshot ---------------------------------------------------------------------------------

class Snapshot(object):
    """Every record of the store at one tip. `tip` is the commit (None: the store does not exist
    yet, or a main store); `raw` the bytes each record was read from (stem -> bytes)."""

    def __init__(self, records, tip, where, rules, raw=None):
        self.records = sorted(records, key=lambda r: r.stem)
        self.tip = tip
        self.where = where
        self.rules = rules
        self.raw = raw or {}
        self.by_id = {r.stem: r for r in self.records}

    @property
    def readable(self):
        return [r for r in self.records if r.readable]

    @property
    def unreadable(self):
        return [r for r in self.records if not r.readable]

    @property
    def file_count(self):
        return len(self.records)

    def get(self, rid):
        return self.by_id.get(rid)

    def all_findings(self):
        out = []
        for r in self.records:
            out.extend(r.structural)
            out.extend(rmod.validate(r, self.rules))
        return out + validate_store(self.records, self.rules)

    def head_label(self):
        return (self.tip or '')[:10] or 'empty'


def require_readable(snap):
    """Every write verb refuses while any record is unreadable: an unreadable record cannot be
    checked against, so nothing the write decides would be sound."""
    bad = snap.unreadable
    if bad:
        raise Failure('%d record(s) unreadable (%s) - fix them first; `check` names why'
                      % (len(bad), ', '.join(r.stem for r in bad[:5])))


def validate_store(records, rules):
    """Findings about the store as a whole: case-only filename collisions, `item:` cycles, a pin
    held twice."""
    out = []
    folded = {}
    for r in records:
        folded.setdefault(r.stem.casefold(), []).append(r)
    for group in folded.values():
        if len(group) > 1:
            for r in group[1:]:
                out.append(rmod.Finding(rmod.ID_FILENAME, r.path, 1, '`%s` differs from `%s` only '
                                        'in case - one file on Windows' % (r.stem, group[0].stem)))
    live = [r for r in records if r.readable]
    edges = {}
    for r in live:
        edges[r.stem] = [m[len('item:'):] for m in r.all('blocked_on') if m.startswith('item:')]
    cycle = _find_cycle(edges)
    if cycle:
        first = next(r for r in live if r.stem == cycle[0])
        out.append(rmod.Finding(rmod.ITEM_TARGETS, first.path, first.line_of('blocked_on'),
                                'an `item:` cycle: %s - nothing in it can ever be pulled'
                                % ' -> '.join(cycle + [cycle[0]])))
    pins = {}
    for r in live:
        p = r.get('pin')
        if p is not None:
            pins.setdefault(p, []).append(r)
    for p, group in sorted(pins.items()):
        if len(group) > 1:
            for r in group[1:]:
                out.append(rmod.Finding(rmod.PIN_UNIQUENESS, r.path, r.line_of('pin'), '`pin: %s` '
                                        'is also held by %s - a pin is held once' % (p, group[0].stem)))
    return out


def _find_cycle(edges):
    """One `item:` cycle as a list of ids, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {k: WHITE for k in edges}
    for start in sorted(edges):
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(edges[start]))]
        path = [start]
        colour[start] = GREY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if nxt not in colour:
                continue
            if colour[nxt] == GREY:
                return path[path.index(nxt):]
            if colour[nxt] == WHITE:
                colour[nxt] = GREY
                stack.append((nxt, iter(edges[nxt])))
                path.append(nxt)
    return None


def dangling_items(by_id):
    """`item:X` blockers naming no live record: warnings (X may have closed)."""
    out = []
    for r in by_id.values():
        if not r.readable:
            continue
        for m in r.all('blocked_on'):
            if m.startswith('item:') and m[len('item:'):] not in by_id:
                out.append(rmod.Finding(rmod.ITEM_TARGETS, r.path, r.line_of('blocked_on'),
                                        '`%s` names no live record - closed? `unblock %s --on %s '
                                        '--reason "<why>"`' % (m, r.id, m)))
    return out


def id_refusal(snap, rid):
    """None if `rid` may name a new record, else why not (shape errors first)."""
    err = snap.rules.id_error(rid)
    if err is not None:
        return err
    if len(rid) > MAX_ID_LEN:
        return '`%s` is longer than %d characters' % (rid, MAX_ID_LEN)
    if rid.split('.')[0].upper() in WINDOWS_DEVICE_NAMES:
        return '`%s` is a Windows device name and cannot be a file' % rid
    for stem in snap.by_id:
        if stem.casefold() == rid.casefold():
            return '`%s` already exists%s' % (rid, '' if stem == rid else ' as `%s`' % stem)
    return None


def parse_blob(stem, data, path):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        return rmod.unreadable(path, stem, 'not UTF-8: %s' % exc)
    if text.startswith('﻿'):
        text = text[1:]
    return rmod.parse(path, text, stem)


def record_entries(root, rev, rel):
    """`([(stem, path, blob sha)], None)`, or `(None, why)`: every `*.md` directly under `rel` in
    the tree of `rev`, one `git ls-tree` and no blob read (preflight counts the default store
    with it: kitlib/preflight/defaultstore.py)."""
    out, why = cmd.run(['ls-tree', '-z', '--full-tree', rev, '--', rel + '/'], root)
    if out is None:
        return None, '`git ls-tree` failed: %s' % why
    entries = []
    for item in out.split(b'\0'):
        meta, _, path = item.partition(b'\t')
        parts = meta.decode('ascii', 'replace').split()
        path = path.decode('utf-8', 'replace')
        name = path[len(rel) + 1:]
        if len(parts) == 3 and parts[1] == 'blob' and '/' not in name and name.endswith('.md'):
            entries.append((name[:-3], path, parts[2]))
    return entries, None


def records_in_tree(root, rev, rel):
    """`(records, raw, None)`, or `(None, None, why)`: every `*.md` directly under `rel` in the
    tree of `rev` (record_entries), parsed, with `raw` mapping each stem to its bytes."""
    entries, why = record_entries(root, rev, rel)
    if entries is None:
        return None, None, why
    blobs, why = batch.cat_file([sha for _, _, sha in entries], root)
    if blobs is None:
        return None, None, why
    records, raw = [], {}
    for stem, path, sha in entries:
        got = blobs.get(sha)
        if got is None:
            return None, None, 'no blob for %s' % path
        raw[stem] = got[1]
        records.append(parse_blob(stem, got[1], path))
    return records, raw, None


# --- the ref and branch backends ------------------------------------------------------------------

def find_remote(root, remote):
    """`remote` when it is configured, None when the repository has no remote at all; Failure
    when git cannot say, or `remote` is missing but others exist (a misnamed remote must not turn
    the store into a local one)."""
    r = cmd.call(['remote', 'get-url', remote], root)
    if r.rc == 0:
        return remote
    if r.rc != 2:                                  # 2: no such remote; anything else is a failure
        raise Failure('cannot look up remote %s: %s'
                      % (remote, r.err or 'git remote get-url exited %s' % r.rc))
    names, why = cmd.run_text(['remote'], root)
    if names is None:
        raise Failure('cannot list the remotes: %s' % why)
    if names:
        raise Failure('no remote named %s, but this repository has %s - set .kit.json '
                      '"repo".remote' % (remote, ', '.join(names.split())))
    return None


class RefBackend(object):
    """Records under `backlog/` in the tree of a ref. With a remote the remote's `remote_ref` is
    the store and `local_ref` caches it; without one (the repository has no remote at all)
    `local_ref` is the store. `implicit`: the store is `.kit.json`'s default, not one it names
    (open_backend), so it reads as usual and publish refuses (default_store_refusal)."""

    def __init__(self, root, remote, remote_ref, local_ref, no_remote_ref=None, fetch=True,
                 implicit=False):
        self.root = root
        self.remote_ref = remote_ref
        self.fetch = fetch
        self.implicit = implicit
        self.remote = find_remote(root, remote)
        self.local_ref = local_ref if self.remote else (no_remote_ref or local_ref)

    def describe(self):
        if self.remote:
            return '%s on %s' % (self.remote_ref, self.remote)
        return self.local_ref

    def _git(self, args, **kw):
        out, why = cmd.run(args, self.root, **kw)
        if out is None:
            raise Failure('git %s: %s' % (args[0], why))
        return out

    def ensure_refspec(self):
        """Best effort: a plain `git fetch` brings the store too (design: the tool adds it). Called
        only once a fetch found the store on the remote: a refspec naming a ref the remote lacks
        makes every plain `git fetch` fail ("couldn't find remote ref")."""
        if not self.remote:
            return
        spec = '+%s:%s' % (self.remote_ref, self.local_ref)
        out, _ = cmd.run(['config', '--get-all', 'remote.%s.fetch' % self.remote], self.root)
        if out is not None and spec in cmd.text(out).split('\n'):
            return
        cmd.call(['config', '--add', 'remote.%s.fetch' % self.remote, spec], self.root)

    def _fetch(self):
        """Bring the remote's store into `local_ref`. Returns False when the remote has none."""
        why = ''
        for _ in range(3):
            r = cmd.call(['fetch', '--no-tags', '--no-write-fetch-head', '--quiet', self.remote,
                          '+%s:%s' % (self.remote_ref, self.local_ref)], self.root,
                         timeout=FETCH_TIMEOUT)
            if r.rc == 0:
                return True
            why = r.err or 'git fetch exited %s' % r.rc
            if "couldn't find remote ref" in why:
                return False
            if 'cannot lock ref' not in why and 'unable to update local ref' not in why:
                break
            time.sleep(random.uniform(0.05, 0.25))
        raise Failure('cannot fetch the backlog store from %s: %s' % (self.remote, why))

    def _local_tip(self):
        """`local_ref`'s commit, None when the ref does not exist; Failure when git cannot tell
        (a git ended from outside included: refs.ref_commit)."""
        tip, why = refs.ref_commit(self.local_ref, self.root)
        if tip is None:
            raise Failure('cannot read %s: %s' % (self.local_ref, why))
        return tip or None

    def load(self, rules):
        tip = None
        if self.remote and self.fetch:
            if self._fetch():
                self.ensure_refspec()
                tip = self._local_tip()
            else:
                cached = self._local_tip()
                if cached is not None:
                    # A write would start a new store on the remote without these records.
                    raise Failure('%s has no %s, but %s holds a store (%s) - push it there or '
                                  'move the local ref aside first'
                                  % (self.remote, self.remote_ref, self.local_ref, cached[:10]))
        else:
            tip = self._local_tip()
        records, raw = [], {}
        if tip:
            records, raw, why = records_in_tree(self.root, tip, STORE_DIR)
            if records is None:
                raise Failure('cannot read the backlog store at %s: %s' % (tip[:10], why))
        return Snapshot(records, tip, self.describe(), rules, raw)

    def path_of(self, stem):
        return '%s/%s.md' % (STORE_DIR, stem)

    def build(self, snap, writes, message):
        """The commit holding `writes` ({stem: text or None}) on top of snap.tip, or None when the
        tree would not change. No working tree or real index is touched."""
        tmp = kuser.mkdtemp('kit-backlog-')
        try:
            env = dict(cmd.scrubbed_env(), GIT_INDEX_FILE=os.path.join(tmp, 'index'))
            self._git(['read-tree', snap.tip] if snap.tip else ['read-tree', '--empty'], env=env)
            lines = []
            for stem, text in sorted(writes.items()):
                if text is None:
                    if snap.tip:                 # the null id is as long as the repo's hashes
                        lines.append('0 %s\t%s' % ('0' * len(snap.tip), self.path_of(stem)))
                    continue
                sha = cmd.text(self._git(['hash-object', '-w', '--no-filters', '--stdin'],
                                         stdin=text.encode('utf-8'))).strip()
                lines.append('100644 %s\t%s' % (sha, self.path_of(stem)))
            self._git(['update-index', '--index-info'], env=env,
                      stdin=('\n'.join(lines) + '\n').encode('utf-8'))
            tree = cmd.text(self._git(['write-tree'], env=env)).strip()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if snap.tip:
            old_tree = cmd.text(self._git(['rev-parse', snap.tip + '^{tree}'])).strip()
            if old_tree == tree:
                return None
        args = ['commit-tree', tree] + (['-p', snap.tip] if snap.tip else []) + ['-F', '-']
        return cmd.text(self._git(args, stdin=message.encode('utf-8'))).strip()

    def publish(self, snap, writes, message):
        """The new tip; Conflict when the store is no longer at snap.tip. Refused, nothing built,
        when the store is the implicit default (default_store_refusal)."""
        if self.implicit:
            raise Refusal(default_store_refusal(self.root))
        new = self.build(snap, writes, message)
        if new is None:
            return snap.tip
        expect = snap.tip or ''
        if not self.remote:
            old = snap.tip or '0' * len(new)
            r = cmd.call(['update-ref', '-m', message.split('\n', 1)[0], self.local_ref, new, old],
                         self.root)
            if r.rc == 0:
                return new
            try:
                now = self._local_tip()
            except Failure as exc:
                raise Failure('git update-ref %s failed (%s) and %s: the write may have landed - '
                              'read the store (`backlog.py check`) before retrying'
                              % (self.local_ref, r.err or 'exit %s' % r.rc, exc))
            if now == new:                       # rc None: timed out or killed after the move
                return new
            # A held lock (another writer mid-update) or a moved ref: the store is busy.
            if 'cannot lock ref' in r.err or now != snap.tip:
                raise Conflict(r.err)
            raise Failure('git update-ref %s: %s' % (self.local_ref, r.err))
        r = cmd.call(['push', '--porcelain', '--quiet',
                      '--force-with-lease=%s:%s' % (self.remote_ref, expect),
                      self.remote, '%s:%s' % (new, self.remote_ref)], self.root,
                     timeout=FETCH_TIMEOUT)
        if r.rc != 0:
            said = '\n'.join(x for x in (r.err.strip(), cmd.text(r.out).strip()) if x)
            now, why = refs.ls_remote(self.remote, self.remote_ref, self.root,
                                      timeout=FETCH_TIMEOUT)
            if now is None:
                raise Failure(self._maybe_landed('the remote cannot be read', said, why))
            if now != new:
                # Porcelain status lines are on stdout, so `said` holds both streams.
                if now == expect and 'cannot lock ref' not in said:
                    raise Failure('push to %s failed: %s' % (self.remote, said))
                if not self._landed_under(new, said):
                    raise Conflict(said)
        self._advance_cache(new)
        return new

    def _landed_under(self, new, said):
        """True when the push that reported failure landed `new` and another write followed it
        (`new` is an ancestor of the remote's store): the write succeeded and must not re-plan."""
        try:
            if not self._fetch():
                return False
            now = self._local_tip()
        except Failure as exc:
            raise Failure(self._maybe_landed('the store cannot be fetched', said, exc))
        if now is None:
            return False
        ok, why = refs.is_ancestor(new, now, self.root)
        if ok is None:
            raise Failure(self._maybe_landed('whether it landed cannot be told', said, why))
        return ok

    def _maybe_landed(self, what, said, why):
        """The ERROR for a failed push whose outcome cannot be read: the write may have landed."""
        return ('push to %s reported failure (%s) and %s (%s): the write may have landed - read '
                'the store (`backlog.py check` fetches it) before retrying'
                % (self.remote, said, what, why))

    def _advance_cache(self, new):
        """Move `local_ref` to `new` only as a fast-forward from the value it holds now (another
        writer in this repository may have moved it past `new`). Best effort: every read fetches."""
        try:
            cur = self._local_tip()
        except Failure:
            return
        if cur == new:
            return
        if cur is not None and refs.is_ancestor(cur, new, self.root)[0] is not True:
            return
        cmd.call(['update-ref', self.local_ref, new, cur or '0' * len(new)], self.root)

    def history(self, rid, snap):
        """`<short sha> <subject>` of the last commit that closed, retired or deleted `rid`, or
        None."""
        if not snap.tip:
            return None
        return _last_closing(self.root, rid, snap.tip)

    def removal(self, rid, snap):
        """The newest store commit that deleted `backlog/<rid>.md` (a Removal), or None."""
        if not snap.tip:
            return None
        return last_removal(self.root, snap.tip, STORE_DIR, rid)

    def uncommitted_removal(self, rid):
        """Always False: every write to a ref store is a commit."""
        return False

    def removed(self, snap):
        """`[(stem, Removal, last text)]` of every record a store commit deleted."""
        if not snap.tip:
            return []
        return removed_records(self.root, snap.tip, STORE_DIR)

    def touched(self, snap):
        """`{stem: epoch}` of the last store commit that wrote each record."""
        if not snap.tip:
            return {}
        return _touch_times(self.root, snap.tip, STORE_DIR)

    def landed(self, main_branch):
        """`(rev, folder)` holding the published records without a fetch: the local ref."""
        return self.local_ref, STORE_DIR


def _last_closing(root, rid, rev):
    pattern = '^(closes|retires|deletes) %s: ' % re.escape(rid)
    out, why = cmd.run_text(['log', '-1', '--format=%h %s', '-E', '--grep', pattern, rev, '--'],
                            root)
    if out is None:
        raise Failure('cannot read the store history: %s' % why)
    return out or None


class Removal(object):
    """The commit that deleted a record's file: short sha, strict ISO commit date, the
    `Session:` line of its message (None when it has none) and its subject."""

    def __init__(self, sha, when, session, subject):
        self.sha = sha
        self.when = when
        self.session = session
        self.subject = subject


def last_removal(root, rev, rel, rid):
    """The newest commit reachable from `rev` that deleted `<rel>/<rid>.md`, or None. Only the id
    is checked (Usage unless PLAIN_ID): the folder is configuration, a backslash in it read as
    `/`. `--full-history`: a record added and deleted on a side branch that was merged leaves the
    merge TREESAME to its first parent, and the default simplification would prune the side
    branch, deleting commit and all. `--no-renames`: a record renamed away counts as deleted."""
    if not PLAIN_ID.match(rid):
        raise Usage('`%s` is not a plain id: as a git pathspec it could match other files' % rid)
    path = '%s/%s.md' % (rel.replace('\\', '/').strip('/'), rid)
    out, why = cmd.run_text(['log', '-1', '--full-history', '--no-renames', '--diff-filter=D',
                             '--format=%h%x1f%cI%x1f%s%x1f%b', rev, '--', path], root)
    if out is None:
        raise Failure('cannot read the store history: %s' % why)
    if not out:
        return None
    sha, when, subject, body = (out.split('\x1f', 3) + ['', '', ''])[:4]
    return Removal(sha, when, session_of(body), subject)


def removed_records(root, rev, rel):
    """`[(stem, Removal, text)]`, newest removal first, one per record id: every `<rel>/<id>.md` a
    commit reachable from `rev` deleted, with the text it had in that commit's first parent. One
    `git log` names every deleted path and one `cat-file --batch` reads them all, however many
    records there are. A record added back later is listed too; the caller drops the ids the
    store holds. A path git cannot read at the parent is left out."""
    rel = rel.replace('\\', '/').strip('/')
    out, why = cmd.run_text(['-c', 'core.quotePath=false', 'log', '--full-history', '--no-renames',
                             '--diff-filter=D', '--name-only',
                             '--format=%x01%H%x1f%h%x1f%cI%x1f%s%x1f%b%x02', rev, '--', rel + '/'],
                            root, timeout=120, strip=False)
    if out is None:
        raise Failure('cannot read the store history: %s' % why)
    found, seen = [], set()
    for head, names in batch.log_records_names(out):
        full, sha, when, subject, body = (head.split('\x1f', 4) + ['', '', '', ''])[:5]
        for name in names:
            stem, ext = os.path.splitext(name[len(rel) + 1:])
            if not name.startswith(rel + '/') or ext != '.md' or '/' in stem or stem in seen:
                continue
            seen.add(stem)
            found.append((stem, Removal(sha, when, session_of(body), subject),
                          '%s^:%s' % (full, name)))
    blobs, why = batch.cat_file([spec for _s, _r, spec in found], root, timeout=120)
    if blobs is None:
        raise Failure('cannot read the store history: %s' % why)
    done = []
    for stem, removal, spec in found:
        got = blobs.get(spec)
        if got and got[0] == 'blob':
            done.append((stem, removal, got[1].decode('utf-8', 'replace')))
    return done


# An id the history lookup may put in a pathspec: no glob or pathspec-magic character, no path
# separator (`show 'F*'` must not match other records' files).
PLAIN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def session_of(body):
    """The value of the first `Session:` line of a commit body; None when there is none or it is
    empty."""
    for line in body.split('\n'):
        if line.startswith('Session:'):
            return line[len('Session:'):].strip() or None
    return None


def _touch_times(root, rev, rel):
    """`{stem: epoch}` of the newest commit reachable from `rev` that wrote `<rel>/<stem>.md`, from
    one `git log`. A record no commit wrote (uncommitted) is absent."""
    out, why = cmd.run_text(['-c', 'core.quotePath=false', 'log', '--no-renames',
                             '--format=%x01%ct%x02', '--name-only', rev, '--', rel + '/'], root,
                            timeout=120, strip=False)
    if out is None:
        raise Failure('cannot read the store history: %s' % why)
    seen = {}
    for stamp, names in batch.log_records_names(out):
        for name in names:
            stem, ext = os.path.splitext(name[len(rel) + 1:])
            if ext == '.md' and '/' not in stem and stamp.strip().isdigit():
                seen.setdefault(stem, int(stamp))
    return seen


# --- the main backend -----------------------------------------------------------------------------

CATCH_UP = 'backlog.py catch-up'
_UNSET = object()


def quoted(paths):
    """`paths` as the arguments of a printed command line: each in double quotes, so a store
    folder with a space in its name stays one argument."""
    return ' '.join('"%s"' % p for p in paths)


def _store_env():
    """The environment of the main backend's git children: cmd.scrubbed_env() (no GIT_DIR,
    GIT_WORK_TREE, GIT_COMMON_DIR, GIT_CEILING_DIRECTORIES) without GIT_INDEX_FILE too, so an index
    the caller inherited (a hook's) is never the one a store write reads or updates."""
    return {k: v for k, v in cmd.scrubbed_env().items() if k.upper() != 'GIT_INDEX_FILE'}


class _IndexLock(object):
    """git's own lock on a worktree's index, taken as git takes it: `<index>.lock` created
    exclusively. While it is held no git command writes that index (`git commit`, `git add`,
    `git reset` and the like stop with "Unable to create ... index.lock: File exists"), so none
    commits the index between a write's commit and its index update. `install` puts the new index
    in place by moving the lock file over the index, as git does, which also gives the lock up;
    `release` removes the file only while this holds it."""

    def __init__(self, index):
        self.index = index
        self.path = index + '.lock'
        self.held = False

    def take(self, wait):
        """True once the lock file is made, False when it still exists after `wait` seconds.
        OSError for any other refusal to make it."""
        deadline = time.monotonic() + wait
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                 | getattr(os, 'O_BINARY', 0)))
                self.held = True
                return True
            except FileExistsError:
                pass
            except PermissionError:          # Windows: a lock file another git is deleting
                if not os.path.exists(self.path):
                    raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def install(self):
        """Move the lock file over the index ('' when done, else why not). A move Windows
        refuses (a reader has the index open) is retried for REPLACE_WAIT seconds
        (kitlib.fs.replace)."""
        try:
            fsreplace.replace(self.path, self.index, wait=REPLACE_WAIT)
        except OSError as exc:
            return 'cannot move %s over the index: %s' % (self.path, exc)
        self.held = False
        return ''

    def release(self):
        """Remove the lock file if this still holds it ('' when it is gone, else why not). A
        removal Windows refuses is retried for REPLACE_WAIT seconds (kitlib.fs.replace)."""
        if not self.held:
            return ''
        try:
            fsreplace.remove(self.path, wait=REPLACE_WAIT)
        except OSError as exc:
            return ('git\'s index lock %s could not be removed (%s): no git command can '
                    'write that index until it is; remove it by hand' % (self.path, exc))
        self.held = False
        return ''


def _replace_file(full, data):
    """Give `full` the bytes `data` in one step: written to a file beside it (same folder, so the
    move stays on one volume; named `<name>.<random>.put-back`, which no reader of the store takes
    for a record), then moved over it. A move Windows refuses (another process has `full` open) is
    retried for REPLACE_WAIT seconds. An OSError leaves `full` as it was, whole, and removes the
    other file; one it cannot remove is named in the error raised. Any other exception (Ctrl+C
    included) removes it too, and is raised as it was."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(full) + '.', suffix='.put-back',
                               dir=os.path.dirname(full))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        fsreplace.replace(tmp, full, wait=REPLACE_WAIT)
    except BaseException as exc:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError as gone:
            if isinstance(exc, OSError):
                raise OSError(exc.errno, '%s; and its temp file %s was left (%s)'
                              % (exc.strerror or exc, tmp, gone)) from exc
        raise


class MainBackend(object):
    """`<dir>/<ID>.md` in a worktree. A write writes the files; the caller's branch carries them
    to main through a landing. There is no compare-and-swap on the files: a write checks that
    each record it rewrites still holds the bytes it read, then writes, so two writers in one
    worktree can still lose an update between that check and the write. It refuses anywhere but
    a linked worktree (a main checkout is the landers'). A branch worktree has one writer, its
    agent; the PM data worktree has several (every session's backlog.py, and a landing's close).

    In the PM data branch's worktree (commit_branch) a write holds git's own index lock there from
    before it reads HEAD's commit or checks the files until its index update (_lock_index; Busy after INDEX_WAIT
    seconds, nothing written), so two store writes there run one after the other, and no git
    command writes that index meanwhile: no `git commit` there can commit an index that does not
    yet hold the write, which would put back what it committed. The write also commits exactly the
    record paths it wrote, as one commit on HEAD by compare-and-swap (`update-ref HEAD <new> <old>`,
    rebuilt on a HEAD that moved unless the move changed one of those paths since publish read
    HEAD, before it checked the files: Overtaken, and the files the write wrote are put back,
    _put_back: a changed path to HEAD's content, any other to what it held before the write;
    only a writer that moves HEAD without the index lock, `git update-ref` say, gets there now),
    and sets those paths in the real index (_sync_index, through the lock file); nothing
    else in that worktree is staged or committed. Any other commit that fails is Uncommitted (the
    files stay written); an index left stale after the commit is IndexStale. Its git calls drop an
    inherited GIT_INDEX_FILE, GIT_DIR and GIT_WORK_TREE
    (_store_env). Elsewhere it commits nothing. With `clean` (a landing's close,
    at_landing.pm_data_store) a write there first requires every path it writes to match HEAD in
    the worktree and the index (_require_clean): else Dirty, and nothing is written, so it never
    deletes or commits a change another session made there and did not commit."""

    def __init__(self, root, rel, clean=False):
        self.root = root
        self.rel = rel.strip('/')
        self.dir = os.path.join(root, *self.rel.split('/'))
        self.clean = clean
        self._branch = _UNSET

    def commit_branch(self, place=None):
        """The PM data branch (kitlib.pmdata) when `root` is a worktree that has it checked out:
        a write there commits. None elsewhere, a detached HEAD included. Failure when git or a
        present PM data record cannot be read: never a silent uncommitted write there. `place`:
        gitrepo.classify(root) when the caller has it."""
        if self._branch is _UNSET:
            self._branch = self._find_branch(place or gitrepo.classify(self.root))
        return self._branch

    def _find_branch(self, place):
        if place.kind not in (gitrepo.MAIN, gitrepo.LINKED):
            return None
        try:
            record = pmdata.read(place.common)
        except pmdata.PmDataError as exc:
            raise Failure('cannot tell whether %s is the PM data worktree: %s' % (self.root, exc))
        if record is None:
            return None
        r = cmd.call(['symbolic-ref', '-q', 'HEAD'], self.root, env=_store_env())
        if r.rc == 1:
            return None                              # detached
        if r.rc != 0:
            raise Failure('cannot tell whether %s is the PM data worktree: git symbolic-ref: %s'
                          % (self.root, r.err or 'exit %s' % r.rc))
        return record.branch if cmd.text(r.out).strip() == 'refs/heads/' + record.branch else None

    def path_of(self, stem):
        return '%s/%s.md' % (self.rel.replace('\\', '/'), stem)

    def describe(self):
        return '%s in %s' % (self.rel, self.root)

    def _read(self):
        raw = {}
        if not os.path.isdir(self.dir):
            return raw
        for name in sorted(os.listdir(self.dir)):
            full = os.path.join(self.dir, name)
            if name.endswith('.md') and os.path.isfile(full):
                with open(full, 'rb') as f:
                    raw[name[:-3]] = f.read()
        return raw

    def load(self, rules):
        raw = self._read()
        records = [parse_blob(stem, data, '%s/%s.md' % (self.rel, stem))
                   for stem, data in raw.items()]
        return Snapshot(records, None, self.describe(), rules, raw)

    def publish(self, snap, writes, message):
        place = gitrepo.classify(self.root)
        if place.kind != gitrepo.LINKED:
            raise Failure('the main backend writes only in a linked worktree (it has no '
                          'compare-and-swap); %s is %s - write from your branch worktree'
                          % (self.root, 'the main checkout' if place.kind == gitrepo.MAIN
                             else 'not one (%s)' % (place.detail or place.kind)))
        branch = self.commit_branch(place)
        if not branch:
            return self._publish(snap, writes, message, None, None)
        paths = [self.path_of(stem) for stem in sorted(writes)]
        return self._locked(paths, lambda held: self._publish(snap, writes, message, branch, held))

    def _locked(self, paths, act):
        """`act(lock)` while this holds git's index lock in the PM data worktree (_lock_index),
        given up afterwards whatever happened. A lock file that cannot be removed is named on the
        Refusal in flight, or raised as a Failure when there is none."""
        held = self._lock_index(paths)
        try:
            done = act(held)
        except Refusal as exc:
            why = held.release()
            if why:
                exc.args = ('%s; and %s' % (exc, why),) + exc.args[1:]
            raise
        except BaseException:
            held.release()                  # one left behind: the next write there says Busy
            raise
        why = held.release()
        if why:
            raise Failure(why)
        return done

    def _lock_index(self, paths):
        """git's lock on this worktree's index, taken (an _IndexLock) before the write reads
        HEAD's commit or checks the files, so between those reads and its index update no git
        command writes that index:
        no `git add` stages one of its paths and no `git commit` commits an index that does not
        yet hold the write (which would put back what the write committed). Busy when another
        git command holds it for INDEX_WAIT seconds; Failure when git cannot name the index or
        the lock cannot be made. Nothing is written either way."""
        out, why = cmd.run_text(['rev-parse', '--path-format=absolute', '--git-path', 'index'],
                                self.root, env=_store_env())
        if not out:
            raise Failure('cannot find the index of %s: %s; nothing was written'
                          % (self.root, why or 'git rev-parse --git-path printed nothing'))
        held = _IndexLock(os.path.normpath(out))
        try:
            took = held.take(INDEX_WAIT)
        except OSError as exc:
            raise Failure('cannot take git\'s index lock %s: %s; nothing was written'
                          % (held.path, exc))
        if not took:
            raise Busy('%s not written in %s, and nothing was written: git\'s index lock %s '
                       'stayed held for %g s (another git command is writing that index; if none '
                       'runs there, a writer that was killed left a stale lock behind: once no '
                       'git command or store write is running, delete %s). '
                       'Repeat the write once it is gone'
                       % (', '.join(paths), self.root, held.path, INDEX_WAIT, held.path),
                       held.path)
        return held

    def _publish(self, snap, writes, message, branch, held):
        """publish, once the worktree is known: `branch` the PM data branch (None elsewhere) and
        `held` its index lock."""
        # HEAD before the files are checked: a commit of one of these paths that lands after the
        # check and before the first commit attempt is then seen by _publish_commit's compare.
        base = None
        if branch:
            try:
                base = self._head()
            except Failure as exc:
                raise Failure('%s; nothing was written' % exc)
            if self.clean:
                self._require_clean(sorted(self.path_of(stem) for stem in writes), held.index)
        now = self._read()
        for stem in writes:
            if now.get(stem) != snap.raw.get(stem):
                raise Conflict('%s.md changed since it was read' % stem)
        paths = [self.path_of(stem) for stem in sorted(writes)]
        blobs = {}
        if branch:                                   # objects first: a failure here writes nothing
            for stem, text in sorted(writes.items()):
                blobs[self.path_of(stem)] = (None if text is None
                                             else self._hash_text(text.encode('utf-8'), paths))
        self._write_files(writes, now, paths, branch)
        if not branch:
            return None
        try:
            return self._commit(blobs, message, paths, held, base)
        except Overtaken as exc:                     # the files must not outlive the refusal
            wrote = dict((self.path_of(stem), None if text is None else text.encode('utf-8'))
                         for stem, text in writes.items())
            before = dict((self.path_of(stem), now.get(stem)) for stem in writes)
            raise self._overtaken(paths, exc.changed, exc.head,
                                  self._put_back(exc.head, wrote, exc.changed, before))

    def _require_clean(self, paths, index):
        """Dirty, before anything is written, when one of `paths` differs from HEAD in the
        worktree or the index, or is there and not in HEAD (ignored files included): a change made
        here and not committed, which the write would delete or commit. When the worktree holds
        HEAD's content for each of them and only the index differs (_index_only: an IndexStale
        left unrepaired, or HEAD moved by a command that left the index alone), the Dirty carries
        the index repair instead of a compare: `git reset -q -- <paths>`, or a whole `git reset
        -q` when `index`, the index file, is missing (a path-limited reset would build an index
        of those paths alone). Failure when git does not answer. Read with no optional lock, so it
        never writes the index."""
        out, why = cmd.run(['--no-optional-locks', '-c', 'core.quotePath=false', 'status',
                            '--porcelain', '-z',
                            '--untracked-files=all', '--ignored', '--no-renames', '--'] + paths,
                           self.root, env=_store_env())
        if out is None:
            raise Failure('cannot read the status of %s in %s: %s; nothing was written'
                          % (', '.join(paths), self.root, why))
        entries = [e for e in cmd.text(out).split('\0') if len(e) > 3]
        dirty = sorted(set(e[3:] for e in entries))
        if not dirty:
            return
        if self._index_only(dirty):
            repair = ('git -C "%s" reset -q -- %s' % (self.root, quoted(dirty))
                      if os.path.exists(index) else 'git -C "%s" reset -q' % self.root)
            raise Dirty('%s not written in %s, and nothing was written: the index there is stale '
                        'for %s (the worktree holds what HEAD holds and the index does not: a '
                        'store write whose index update failed, or a command that moved HEAD and '
                        'left the index alone), so a plain `git commit` there would revert it; '
                        'repair the index first (the worktree files are left alone): %s'
                        % (', '.join(paths), self.root, ', '.join(dirty), repair),
                        dirty, repair=repair)
        if any(e[:2] in ('??', '!!') for e in entries):
            compare = 'git -C "%s" status --short -- %s' % (self.root, quoted(dirty))
        else:
            compare = 'git -C "%s" diff HEAD -- %s' % (self.root, quoted(dirty))
        raise Dirty('%s not written in %s, and nothing was written: %s differ%s from HEAD there '
                    '(a change made there and not committed: a hand edit, a staged change or a '
                    'record never committed), which this write would delete or commit over; '
                    'compare with HEAD first: %s'
                    % (', '.join(paths), self.root, ', '.join(dirty),
                       's' if len(dirty) == 1 else '', compare), dirty, compare)

    def _index_only(self, paths):
        """True when the worktree holds exactly HEAD's content for each of `paths` (as `git add`
        would hash it; no file where HEAD has none), so what differs from HEAD there is the index
        alone. False when one differs, or when git does not answer: the caller then names a
        change to compare, which is never wrong to look at."""
        out, _ = cmd.run(['ls-tree', '-z', '--full-tree', 'HEAD', '--'] + paths, self.root,
                         env=_store_env())
        if out is None:
            return False
        head = {}
        for entry in cmd.text(out).split('\0'):
            meta, tab, path = entry.partition('\t')
            if tab:
                head[path] = meta.split()[-1]
        present = [p for p in paths if os.path.isfile(os.path.join(self.root, *p.split('/')))]
        disk = {}
        if present:
            out, _ = cmd.run(['hash-object', '--'] + present, self.root, env=_store_env())
            shas = [] if out is None else cmd.text(out).split()
            if len(shas) != len(present):
                return False
            disk = dict(zip(present, shas))
        return all(head.get(p) == disk.get(p) for p in paths)

    def _put_back(self, head, wrote, changed, before):
        """After an Overtaken refusal, undo the files of the refused write, so that no reader and
        no later write sees its content: a path in `changed` (another commit changed it) gets
        the content `head` holds, any other path the bytes it held before the write (`before`),
        which HEAD may not hold: an uncommitted record stays one. A file is removed where that
        content is none. `wrote`, `before`: {path: bytes, or None for no file}. Only a file that
        still holds exactly what this write wrote is touched, and HEAD's content is fetched
        first, so no git call sits between that check and the write. The content goes in
        whole or not at all (_replace_file), so a path left is never a part of it. Returns
        the paths left as they are: changed since, or git or the disk failed."""
        want = dict(before)
        unknown = set()
        try:
            held = self._entries(head, changed)
        except Failure:
            held, unknown = {}, set(changed)
        for path in changed:
            kind, blob = (held.get(path, '').split() + [''] * 3)[1:3]
            want[path] = None
            if kind == 'blob':
                want[path], _ = cmd.run(['cat-file', 'blob', blob], self.root, env=_store_env())
            if path in held and want[path] is None:  # no blob there, or git did not give it
                unknown.add(path)
        left = []
        for path in sorted(wrote):
            full = os.path.join(self.root, *path.split('/'))
            try:
                mine = None
                if os.path.lexists(full):
                    with open(full, 'rb') as f:
                        mine = f.read()
                if path in unknown or mine != wrote[path]:
                    left.append(path)
                elif want[path] is not None:
                    _replace_file(full, want[path])
                elif mine is not None:
                    os.remove(full)
            except OSError:
                left.append(path)
        return left

    def _write_files(self, writes, before, paths, branch):
        """Write `writes`, each file whole or not at all (_replace_file); on an OSError put back
        the files already written (`before`: their bytes) and raise Failure naming the path it
        could not write (left as it was) and saying nothing was committed."""
        done = []
        failed = self.dir
        try:
            os.makedirs(self.dir, exist_ok=True)
            for stem, text in sorted(writes.items()):
                failed = self.path_of(stem)
                full = os.path.join(self.dir, stem + '.md')
                if text is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    _replace_file(full, text.encode('utf-8'))
                done.append(stem)
        except OSError as exc:
            left = self._restore(done, before)
            msg = 'cannot write %s (it is left as it was): %s; %s' % (
                failed, exc, 'the files already written were put back' if not left else
                'these were written and could not be put back: %s' % ', '.join(left))
            if branch:
                msg += ('; the write is not committed (`%s` lists any uncommitted record path)'
                        % CATCH_UP)
            raise Failure(msg)

    def _restore(self, stems, before):
        """Put back the bytes `before` held for each of `stems` (delete one it did not hold),
        whole or not at all (_replace_file), so one not put back holds the write's content
        whole. Returns the paths that could not be put back."""
        left = []
        for stem in stems:
            full = os.path.join(self.dir, stem + '.md')
            try:
                if before.get(stem) is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    _replace_file(full, before[stem])
            except OSError:
                left.append(self.path_of(stem))
        return left

    # -- committing in the PM data worktree -------------------------------------------------------

    def _uncommitted(self, paths, why):
        return Uncommitted('%s written but not committed in %s: %s. `%s` lists the uncommitted '
                           'record paths and `%s --apply %s` commits them'
                           % (', '.join(paths), self.root, why, CATCH_UP, CATCH_UP,
                              quoted(paths)), paths)

    def _overtaken(self, paths, changed, head, left=None):
        """The Overtaken for `paths`. `left` None: nothing was put back (as _publish_commit
        raises it, which is how `catch-up --apply` ends); else the paths _put_back left."""
        why = ('another commit changed %s on HEAD since this write began (HEAD is now %s)'
               % (', '.join(changed), head[:10]))
        back = ('put back (%s to what HEAD holds, any other to what it held before the write)'
                % ', '.join(changed))
        if left is not None and not left:
            return Overtaken(
                '%s not written in %s: %s, so the write was refused and each file it had written '
                'was %s; nothing of it remains. Read the record again '
                'and repeat the write if it is still wanted'
                % (', '.join(paths), self.root, why, back), paths, changed, '', head, left)
        compare = 'git -C "%s" diff HEAD -- %s' % (self.root, quoted(changed if left is None
                                                                     else left))
        if left is None:
            said = ('%s written but not committed in %s: %s, and committing the files would '
                    'overwrite that commit. Compare each with HEAD first (%s)'
                    % (', '.join(paths), self.root, why, compare))
        else:
            said = ('%s refused in %s: %s, and committing the files would overwrite that commit. '
                    'Each file the write had written was %s, except %s. '
                    'Left as it is, because it no longer held what this write wrote or could not '
                    'be put back: compare it with HEAD first (%s)'
                    % (', '.join(paths), self.root, why, back, ', '.join(left), compare))
        return Overtaken(
            '%s; `%s --apply` commits the worktree\'s files as they are, so run it only for a '
            'path whose file is what HEAD should hold' % (said, CATCH_UP),
            paths, changed, compare, head, left)

    def _git(self, args, paths, **kw):
        kw.setdefault('env', _store_env())
        out, why = cmd.run(args, self.root, **kw)
        if out is None:
            raise self._uncommitted(paths, 'git %s: %s' % (args[0], why))
        return cmd.text(out).strip()

    def _hash_text(self, data, paths):
        """The blob id of `data`, written to the object store before any record file is: a
        failure here is a plain Failure, with nothing written."""
        out, why = cmd.run(['hash-object', '-w', '--stdin'], self.root, stdin=data,
                           env=_store_env())
        if out is None:
            raise Failure('cannot write %s to the object store of %s: git hash-object: %s; '
                          'nothing was written' % (', '.join(paths), self.root, why))
        return cmd.text(out).strip()

    def _head(self):
        out, why = cmd.run_text(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], self.root,
                                env=_store_env())
        if not out:
            raise Failure('cannot read HEAD in %s: %s' % (self.root, why or 'no commit'))
        return out

    def _entries(self, rev, paths):
        """`{path: 'mode type blob'}` for each of `paths` that `rev` holds: what a retry compares
        to tell whether another commit changed this write's records."""
        out = self._git(['ls-tree', '-z', '--full-tree', rev, '--'] + sorted(paths), paths)
        found = {}
        for entry in out.split('\0'):
            meta, tab, path = entry.partition('\t')
            if tab:
                found[path] = meta
        return found

    def _tree(self, head, lines, paths):
        """The tree of `head` with `lines` (`update-index --index-info` input) applied, built in a
        temporary index."""
        tmp = kuser.mkdtemp('kit-backlog-')
        try:
            env = dict(_store_env(), GIT_INDEX_FILE=os.path.join(tmp, 'index'))
            self._git(['read-tree', head], paths, env=env)
            self._git(['update-index', '--index-info'], paths, env=env,
                      stdin=('\n'.join(lines) + '\n').encode('utf-8'))
            return self._git(['write-tree'], paths, env=env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _commit(self, blobs, message, paths, held, base=None):
        """Commit `blobs` ({path: blob id, or None to delete}) on HEAD as one commit: built in a
        temporary index, published by `update-ref HEAD <new> <old>` and rebuilt on a HEAD another
        commit moved (ATTEMPTS times). Then the real index takes the same entries for those paths
        alone. Returns the new commit, or None when HEAD already holds them. No ref moves until the
        whole commit exists, so a failure leaves no commit holding part of the write; it is
        raised as Uncommitted, never swallowed. `held`: git's index lock there (_lock_index),
        held by the caller. `base`: HEAD when the write began (publish reads it before it checks
        the files), or None for the HEAD the first attempt finds."""
        try:
            new, lines = self._publish_commit(blobs, message, paths, base)
        except Uncommitted:
            raise
        except Failure as exc:                       # HEAD unreadable: the files are still written
            raise self._uncommitted(paths, str(exc))
        self._sync_index(lines, new, paths, held)
        return new

    def _publish_commit(self, blobs, message, paths, base=None):
        """`(new commit or None, index lines)`: _commit's compare-and-swap loop. Every attempt,
        the first included, builds on a HEAD other than `base` only while the commits between
        left this write's paths as `base` held them; one that changed any of them stops the loop
        (Overtaken), so that commit is never overwritten."""
        subject = message.split('\n', 1)[0]
        last = ''
        for attempt in range(ATTEMPTS):
            head = self._head()
            zero = '0' * len(head)
            lines = ['100644 %s\t%s' % (sha, p) if sha else '0 %s\t%s' % (zero, p)
                     for p, sha in sorted(blobs.items())]
            tree = self._tree(head, lines, paths)
            if tree == self._git(['rev-parse', head + '^{tree}'], paths):
                return None, lines                   # HEAD holds them already: no empty commit
            if base is None:
                base = head
            elif head != base:
                was, now = self._entries(base, paths), self._entries(head, paths)
                changed = [p for p in paths if was.get(p) != now.get(p)]
                if changed:
                    raise self._overtaken(paths, changed, head)
            new = self._git(['commit-tree', tree, '-p', head, '-F', '-'], paths,
                            stdin=message.encode('utf-8'))
            r = cmd.call(['update-ref', '-m', 'backlog: ' + subject, 'HEAD', new, head], self.root,
                         env=_store_env())
            if r.rc == 0:
                return new, lines
            last = r.err or 'git update-ref exited %s' % r.rc
            now = self._head()
            if now == new:                           # rc None: timed out after the move
                return new, lines
            if now == head and 'cannot lock ref' not in last:
                raise self._uncommitted(paths, 'git update-ref HEAD: %s' % last)
            time.sleep(random.uniform(0.05, 0.25) * (attempt + 1))
        raise self._uncommitted(paths, 'HEAD moved or stayed locked under %d attempts (%s)'
                                % (ATTEMPTS, last))

    def _sync_index(self, lines, new, paths, held):
        """Give the real index `lines` for those paths only, so `git status` shows them clean,
        under git's index lock `held`, as git itself writes an index: the index is copied into
        the lock file (its modification time kept, which git's racy-entry check compares with),
        `update-index` writes that copy (GIT_INDEX_FILE), and the copy is moved over the index
        (_IndexLock.install). IndexStale when it cannot: the write stays committed, the index is
        as it was, and the error carries the repair: a reset of those paths, or of the whole
        index when there is no index file (a reset of some paths would build one of those alone,
        every other tracked file then a staged deletion)."""
        why = ''
        try:
            st = os.stat(held.index)
            shutil.copyfile(held.index, held.path)
            os.utime(held.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError as exc:
            why = 'cannot copy the index into its lock file: %s' % exc
        if not why:
            r = cmd.call(['update-index', '--index-info'], self.root,
                         env=dict(_store_env(), GIT_INDEX_FILE=held.path),
                         stdin=('\n'.join(lines) + '\n').encode('utf-8'))
            if r.rc != 0:
                why = r.err or 'git update-index exited %s' % r.rc
        if not why:
            why = held.install()
        if not why:
            return
        if os.path.exists(held.index):
            repair = 'git -C "%s" reset -q -- %s' % (self.root, quoted(paths))
        else:
            repair = 'git -C "%s" reset -q' % self.root
        raise IndexStale(
            '%s committed in %s: HEAD %s, but the real index of that worktree was not updated '
            '(%s), so it is stale for them and a plain `git commit` there would revert them. '
            'Repair the index (the worktree files are left alone): %s'
            % (', '.join(paths), self.root,
               'has the commit %s' % new[:10] if new else 'already holds them',
               ' '.join(why.split()), repair),
            commit=new, paths=paths, repair=repair)

    def pending(self):
        """`[(kind, path)]` of every path under the store folder whose worktree file differs from
        HEAD, in path order: `added`, `modified` or `deleted` for a record (`<dir>/<ID>.md`), `not
        a record` for anything else there. Failure when git does not answer."""
        rel = self.rel.replace('\\', '/')
        out, why = cmd.run(['-c', 'core.quotePath=false', 'status', '--porcelain', '-z',
                            '--untracked-files=all', '--no-renames', '--', rel + '/'], self.root,
                           env=_store_env())
        if out is None:
            raise Failure('cannot read the status of %s: %s' % (self.root, why))
        changed = set()
        for entry in cmd.text(out).split('\0'):
            if len(entry) > 3:
                changed.add(entry[3:])
        tracked, why = cmd.run(['ls-tree', '-r', '-z', '--name-only', 'HEAD', '--', rel + '/'],
                               self.root, env=_store_env())
        if tracked is None:
            raise Failure('cannot read HEAD in %s: %s' % (self.root, why))
        in_head = set(n for n in cmd.text(tracked).split('\0') if n)
        found = []
        for p in sorted(changed):
            on_disk = os.path.isfile(os.path.join(self.root, *p.split('/')))
            if not self.is_record_path(p):
                kind = 'not a record'
            elif p in in_head:
                kind = 'modified' if on_disk else 'deleted'
            elif on_disk:
                kind = 'added'
            else:
                continue                             # staged, then removed: nothing to commit
            found.append((kind, p))
        return found

    def is_record_path(self, path):
        """True for `<dir>/<ID>.md` directly in the store folder, `path` as git spells it."""
        prefix = self.rel.replace('\\', '/') + '/'
        stem = path[len(prefix):-3] if path.startswith(prefix) and path.endswith('.md') else ''
        return bool(stem) and PLAIN_ID.match(stem) is not None

    def commit_paths(self, paths, message):
        """Commit the worktree's state of `paths` (record paths, as git spells them; a missing
        file is a deletion) as one commit on HEAD, as a write does: `catch-up --apply`. Only in
        the PM data worktree and only record paths (Failure otherwise, before anything moves).
        Returns the new commit, or None when HEAD already holds them. It holds git's index lock
        there from before it reads the files (_lock_index: Busy, nothing committed)."""
        if not self.commit_branch():
            raise Failure('%s is not the PM data worktree: its store writes are not committed '
                          'here' % self.root)
        bad = [p for p in paths if not self.is_record_path(p)]
        if bad or not paths:
            raise Failure('not a record path of %s/: %s' % (self.rel, ', '.join(bad) or 'none given'))
        paths = sorted(set(paths))
        return self._locked(paths, lambda held: self._commit_paths(paths, message, held))

    def _commit_paths(self, paths, message, held):
        present = [p for p in paths if os.path.isfile(os.path.join(self.root, *p.split('/')))]
        blobs = dict.fromkeys(paths)
        if present:                                   # as `git add` would: the path's filters
            shas = self._git(['hash-object', '-w', '--'] + present, paths).split('\n')
            if len(shas) != len(present):
                raise self._uncommitted(paths, 'git hash-object printed %d ids for %d files'
                                        % (len(shas), len(present)))
            blobs.update(zip(present, shas))
        return self._commit(blobs, message, paths, held)

    def history(self, rid, snap):
        return _last_closing(self.root, rid, 'HEAD')

    def removal(self, rid, snap):
        """The newest commit on HEAD that deleted `<dir>/<rid>.md` (a Removal), or None."""
        return last_removal(self.root, 'HEAD', self.rel, rid)

    def uncommitted_removal(self, rid):
        """True when HEAD tracks `<dir>/<rid>.md` and the worktree no longer holds it: a write
        here deleted it (`close`, `retire`, `delete`, which commit nothing outside the PM data
        worktree)
        or a hand removed it. False on
        an unborn HEAD. Failure when git did not answer (killed, timed out): fail closed."""
        if not PLAIN_ID.match(rid):
            raise Usage('`%s` is not a plain id: as a git pathspec it could match other files' % rid)
        if os.path.exists(os.path.join(self.dir, rid + '.md')):
            return False
        r = cmd.call(['cat-file', '-e', 'HEAD:%s/%s.md' % (self.rel.replace('\\', '/'), rid)],
                     self.root, env=_store_env())
        if r.rc is None:
            raise Failure(r.err)
        return r.rc == 0

    def removed(self, snap):
        """`[(stem, Removal, last text)]` of every record a commit on HEAD deleted; none while the
        repository has no commit."""
        out, _why = cmd.run_text(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], self.root,
                                 env=_store_env())
        if not out:
            return []
        return removed_records(self.root, 'HEAD', self.rel)

    def touched(self, snap):
        return _touch_times(self.root, 'HEAD', self.rel)

    def landed(self, main_branch):
        """`(rev, folder)` holding the landed records: the folder on the main branch."""
        return main_branch, self.rel


def catch_up(backend, paths, session):
    """`backlog.py catch-up --apply PATH...`: commit `paths` in the PM data worktree as one commit
    (MainBackend.commit_paths), each named and each an uncommitted record path that
    MainBackend.pending lists. Usage for anything else, before anything moves. Returns the new
    commit, or None when HEAD already holds them."""
    if not isinstance(backend, MainBackend):
        raise Usage('`catch-up` is for a folder store; %s commits its own writes'
                    % backend.describe())
    if not paths:
        raise Usage('`catch-up --apply` takes the record paths to commit, each named; '
                    '`catch-up` lists them')
    paths = sorted(set(p.replace('\\', '/') for p in paths))
    pending = dict((p, kind) for kind, p in backend.pending())
    bad = [p for p in paths if p not in pending or pending[p] == 'not a record']
    if bad:
        raise Usage('not an uncommitted record path of %s: %s (`catch-up` lists them)'
                    % (backend.describe(), ', '.join(bad)))
    return backend.commit_paths(paths, 'backlog: catch up %d uncommitted record path%s\n\n'
                                'Session: %s\n' % (len(paths), '' if len(paths) == 1 else 's',
                                                   session or '-'))


def pm_data_live_store(root):
    """`(pm_root, named, None)`: the PM data worktree of `root`'s repository
    (at_landing.pm_data_root) and the "backlog".store its `.kit.json` writes, `named` None when it
    writes none (it then reads the same implicit ref as `root`, refs being shared by every
    worktree). `(None, None, None)` when the repository has no PM data record or `root` is that
    worktree; `(None, None, why)` when the worktree cannot be found; `(pm_root, None, why)` when it
    was found and its `.kit.json` cannot be read (`why` the error). Shared by default_store_refusal and preflight's backlog line (kitlib/preflight/defaultstore.py)."""
    from . import at_landing                         # at_landing imports this module
    pm_root, why = at_landing.pm_data_root(root)
    if pm_root is None:
        return None, None, why
    if os.path.normcase(os.path.normpath(pm_root)) == os.path.normcase(os.path.normpath(root)):
        return None, None, None
    from kitlib.config import ConfigError, load as load_config
    try:
        named = load_config(pm_root).written('backlog', 'store')
    except (ConfigError, OSError) as exc:
        return pm_root, None, str(exc) or type(exc).__name__
    return pm_root, named, None


def worktree_live_store(top, cfg):
    """`(pm_root, branch)` when a run from the checkout `top` opens the live store in the PM data
    worktree instead of its own: `top`'s `.kit.json` (`cfg`) names no "backlog".store, `top` is a
    linked worktree of its repository (git's own answer; the PM data worktree itself is excluded
    by pm_data_live_store), that repository's PM data worktree names a store of its own
    (pm_data_live_store, read without error), and `top` has a branch checked out, `branch`, the
    one the run acts as. None otherwise: the main checkout, a detached HEAD, a named store or no
    live store keep `top`'s own (the implicit ref, whose writes default_store_refusal refuses).
    The same resolution `-C <pm data> --as <branch>` spells out by hand (docs/backlog.md
    "The store a worktree opens")."""
    if cfg.written('backlog', 'store') is not None:
        return None
    if gitrepo.classify(top).kind != gitrepo.LINKED:
        return None
    try:
        pm_root, named, why = pm_data_live_store(top)
    except Exception:                                # noqa: BLE001 - no live store found: none
        return None
    if pm_root is None or not named or why:
        return None
    branch, _why = refs.current_branch(top)
    if not branch:
        return None
    return pm_root, branch


def unreadable_kit_json(pm_root, why):
    """The clause naming a PM data worktree whose `.kit.json` cannot be read (pm_data_live_store)."""
    return 'the .kit.json of the PM data worktree %s cannot be read: %s' % (
        pm_root.replace('\\', '/'), why)


def default_store_refusal(root):
    """Why a write to the implicit `ref` store is refused, and where to write instead: the PM data
    worktree and its store when the repository has one that names a store (pm_data_live_store),
    else the `.kit.json` setting that opts in. A PM data worktree naming no store reads the same
    implicit ref, so a write there is refused too and it is not offered. Records written there by
    default were lost to the live store once, so a ref store is written only when named."""
    head = ('%s is the default backlog store: this repository\'s .kit.json names no '
            '"backlog".store, so nothing was written' % STORE_REF)
    opt_in = 'set "backlog": {"store": "ref"} in .kit.json to write this store'
    try:
        pm_root, named, why = pm_data_live_store(root)
    except Exception as exc:                         # noqa: BLE001 - the advice is best effort
        pm_root, named, why = None, None, str(exc)
    if pm_root is None:
        where = '; the PM data worktree was not found (%s)' % why if why else ''
        return '%s%s; %s' % (head, where, opt_in)
    if why:
        return '%s; %s; %s' % (head, unreadable_kit_json(pm_root, why), opt_in)
    if named is None:
        return ('%s; the PM data worktree "%s" names no "backlog".store either, so a write there '
                'is refused too; %s' % (head, pm_root.replace('\\', '/'), opt_in))
    return ('%s; write to the live store in the PM data worktree (store %s) with -C "%s", or %s'
            % (head, named, pm_root.replace('\\', '/'), opt_in))


def open_backend(root, cfg, fetch=True, clean=False):
    """The backend `.kit.json` "backlog".store names (absent: the schema default, `ref`): `ref`
    the hidden ref, `branch` the `backlog` branch, `main` docs/backlog in `root`, any other path
    that folder in `root`. `clean`: a folder store's MainBackend writes over no uncommitted change
    (Dirty); a ref store ignores it. The `ref` store reached by the default alone (`.kit.json`
    does not name it) is read-only: its publish refuses (default_store_refusal)."""
    implicit = cfg.written('backlog', 'store') is None
    store = cfg.section('backlog')['store']
    remote = cfg.remote
    if store == 'ref':
        return RefBackend(root, remote, STORE_REF, STORE_REF, fetch=fetch, implicit=implicit)
    if store == 'branch':
        return RefBackend(root, remote, 'refs/heads/' + BRANCH,
                          'refs/remotes/%s/%s' % (remote, BRANCH),
                          no_remote_ref='refs/heads/' + BRANCH, fetch=fetch)
    return MainBackend(root, 'docs/backlog' if store == 'main' else store, clean=clean)


# --- the write loop -------------------------------------------------------------------------------

class Change(object):
    """What a plan decided: `writes` {stem: new text, or None to delete}, the commit subject, the
    lines to print, and the ruling it cites."""

    def __init__(self, writes, subject, lines=(), ruling=None):
        self.writes = dict(writes)
        self.subject = subject
        self.lines = list(lines)
        self.ruling = ruling


_PLAN_NOTES = contextvars.ContextVar('backlog_plan_notes', default=None)


@contextlib.contextmanager
def plan_notes():
    """Collect the lines a plan adds with add_plan_note while it runs (a list, in order, each
    once). Store.write and a dry run put them before the change's own lines."""
    notes = []
    token = _PLAN_NOTES.set(notes)
    try:
        yield notes
    finally:
        _PLAN_NOTES.reset(token)


def add_plan_note(line):
    """A line the running plan's caller prints with the change (plan.refuse_machine_paths's
    machine-config WARNING); dropped when no plan_notes collector is open."""
    notes = _PLAN_NOTES.get()
    if notes is not None and line not in notes:
        notes.append(line)


@contextlib.contextmanager
def notes_on_refusal(notes):
    """Re-raise a Refusal (any of kitlib.backlog.errors) raised inside with each line of `notes`
    its message does not already hold added on a line of its own, as commit.py adds its machine
    config WARNING to the stop lines: a plan that stops for another reason still prints it. The
    same exception object, so its type and attributes are kept."""
    try:
        yield
    except Refusal as exc:
        msg = str(exc)
        extra = ''.join('\n' + n for n in notes if n not in msg)
        if extra:
            exc.args = (msg + extra,) + tuple(exc.args[1:])
        raise


def with_plan_notes(change, notes):
    """`change` with `notes` it does not already print put before its lines."""
    change.lines = [n for n in notes if n not in change.lines] + change.lines
    return change


class Store(object):
    """A backend plus the schema rules. `before_publish(attempt)` is a test hook run between the
    plan and the publish of every attempt."""

    def __init__(self, backend, rules, session=None):
        self.backend = backend
        self.rules = rules
        self.session = session
        self.before_publish = None

    def load(self):
        return self.backend.load(self.rules)

    def history(self, rid, snap):
        return self.backend.history(rid, snap)

    def removal(self, rid, snap):
        return self.backend.removal(rid, snap)

    def uncommitted_removal(self, rid):
        return self.backend.uncommitted_removal(rid)

    def removed(self, snap):
        return self.backend.removed(snap)

    def touched(self, snap):
        return self.backend.touched(snap)

    def message(self, change):
        lines = [change.subject, '']
        lines.append('Session: %s' % (self.session or '-'))
        if change.ruling:
            lines.append('Ruling: PM %s' % change.ruling)
        return '\n'.join(lines) + '\n'

    def write(self, plan):
        """Run `plan(snapshot) -> Change` against a fresh snapshot and publish it by
        compare-and-swap, re-planning after every lost race. Returns (change, new tip)."""
        last = ''
        for attempt in range(ATTEMPTS):
            snap = self.load()
            require_readable(snap)
            with plan_notes() as notes, notes_on_refusal(notes):
                change = plan(snap)
            change = with_plan_notes(change, notes)
            if not change.writes:
                return change, snap.tip
            with notes_on_refusal(notes):
                check_change(snap, change)
            if self.before_publish is not None:
                self.before_publish(attempt)
            try:
                return change, self.backend.publish(snap, change.writes, self.message(change))
            except Conflict as exc:
                last = str(exc)
                if getattr(self.backend, 'fetch', True) is False:
                    self.backend.fetch = True        # the local copy was stale: re-read the remote
                time.sleep(random.uniform(0.05, 0.3) * (attempt + 1))
        raise Failure('the store moved under %d attempts in a row; nothing was written (%s)'
                      % (ATTEMPTS, last or 'compare-and-swap lost'))


def check_change(snap, change):
    """Usage when the change would add a finding: a FAIL `check` would report on the store after
    the change (check.fail_findings: every record and store-wide check bar the ledger's), or a new
    dangling `item:` blocker. Findings the store already had do not block a write."""
    from .check import fail_findings                 # check imports this module
    records = dict(snap.by_id)
    for stem, text in change.writes.items():
        if text is None:
            records.pop(stem, None)
            continue
        old = snap.by_id.get(stem)
        records[stem] = rmod.parse(old.path if old is not None else '%s.md' % stem, text, stem)
    after = Snapshot(list(records.values()), snap.tip, snap.where, snap.rules)
    before = {(f.check, f.path, f.message) for f in fail_findings(snap)}
    new = [f for f in fail_findings(after) if (f.check, f.path, f.message) not in before]
    # A new `item:` blocker naming nothing would hold its record forever (a WARN at rest only,
    # since `close` and `retire` rewrite their dependents).
    before = {(f.path, f.message) for f in dangling_items(snap.by_id)}
    new.extend(f for f in dangling_items(records) if (f.path, f.message) not in before)
    if new:
        raise Usage('the write would leave the store invalid; nothing was written:\n' +
                    '\n'.join('  %s: %s' % (f.path, f.message) for f in new))
