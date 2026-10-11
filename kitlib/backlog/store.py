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
import os
import random
import re
import shutil
import tempfile
import time

from kitlib import pmdata
from kitlib.git import batch, cmd, refs
from kitlib.git import repo as gitrepo

from . import record as rmod
from .errors import Failure, Usage

STORE_REF = 'refs/backlog/main'
STORE_DIR = 'backlog'
BRANCH = 'backlog'
ATTEMPTS = 5
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


def records_in_tree(root, rev, rel):
    """`(records, raw, None)`, or `(None, None, why)`: every `*.md` directly under `rel` in the
    tree of `rev`, parsed, with `raw` mapping each stem to its bytes."""
    out, why = cmd.run(['ls-tree', '-z', '--full-tree', rev, '--', rel + '/'], root)
    if out is None:
        return None, None, '`git ls-tree` failed: %s' % why
    entries = []
    for item in out.split(b'\0'):
        meta, _, path = item.partition(b'\t')
        parts = meta.decode('ascii', 'replace').split()
        path = path.decode('utf-8', 'replace')
        name = path[len(rel) + 1:]
        if len(parts) == 3 and parts[1] == 'blob' and '/' not in name and name.endswith('.md'):
            entries.append((name[:-3], path, parts[2]))
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
    `local_ref` is the store."""

    def __init__(self, root, remote, remote_ref, local_ref, no_remote_ref=None, fetch=True):
        self.root = root
        self.remote_ref = remote_ref
        self.fetch = fetch
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
        tmp = tempfile.mkdtemp(prefix='kit-backlog-')
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
        """The new tip; Conflict when the store is no longer at snap.tip."""
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
        """`<short sha> <subject>` of the last commit that closed or retired `rid`, or None."""
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

    def touched(self, snap):
        """`{stem: epoch}` of the last store commit that wrote each record."""
        if not snap.tip:
            return {}
        return _touch_times(self.root, snap.tip, STORE_DIR)

    def landed(self, main_branch):
        """`(rev, folder)` holding the published records without a fetch: the local ref."""
        return self.local_ref, STORE_DIR


def _last_closing(root, rid, rev):
    pattern = '^(closes|retires) %s: ' % re.escape(rid)
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


class MainBackend(object):
    """`<dir>/<ID>.md` in a worktree. A write writes the files; the caller's branch carries them
    to main through a landing. There is no compare-and-swap on the files: a write checks that
    each record it rewrites still holds the bytes it read, then writes, so it is safe only where
    one writer works - it refuses anywhere but a linked worktree (a main checkout is the
    landers').

    In the PM data branch's worktree (commit_branch) the write also commits exactly the record
    paths it wrote, as one commit on HEAD by compare-and-swap (`update-ref HEAD <new> <old>`,
    rebuilt on a HEAD that moved), and sets those paths in the real index; nothing else in that
    worktree is staged or committed. Elsewhere it commits nothing."""

    def __init__(self, root, rel):
        self.root = root
        self.rel = rel.strip('/')
        self.dir = os.path.join(root, *self.rel.split('/'))
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
        r = cmd.call(['symbolic-ref', '-q', 'HEAD'], self.root)
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
        return self._commit(blobs, message, paths)

    def _write_files(self, writes, before, paths, branch):
        """Write `writes`; on an OSError put back the files already written (`before`: their
        bytes) and raise Failure saying nothing was committed."""
        done = []
        try:
            os.makedirs(self.dir, exist_ok=True)
            for stem, text in sorted(writes.items()):
                full = os.path.join(self.dir, stem + '.md')
                if text is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    with open(full, 'wb') as f:
                        f.write(text.encode('utf-8'))
                done.append(stem)
        except OSError as exc:
            left = self._restore(done, before)
            msg = 'cannot write %s: %s; %s' % (
                self.dir, exc, 'the files already written were put back' if not left else
                'these were written and could not be put back: %s' % ', '.join(left))
            if branch:
                msg += ('; the write is not committed (`%s` lists any uncommitted record path)'
                        % CATCH_UP)
            raise Failure(msg)

    def _restore(self, stems, before):
        """Put back the bytes `before` held for each of `stems` (delete one it did not hold).
        Returns the paths that could not be put back."""
        left = []
        for stem in stems:
            full = os.path.join(self.dir, stem + '.md')
            try:
                if before.get(stem) is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    with open(full, 'wb') as f:
                        f.write(before[stem])
            except OSError:
                left.append(self.path_of(stem))
        return left

    # -- committing in the PM data worktree -------------------------------------------------------

    def _uncommitted(self, paths, why):
        return Failure('%s written but not committed in %s: %s. `%s` lists the uncommitted record '
                       'paths and `%s --apply` commits them'
                       % (', '.join(paths), self.root, why, CATCH_UP, CATCH_UP))

    def _git(self, args, paths, **kw):
        out, why = cmd.run(args, self.root, **kw)
        if out is None:
            raise self._uncommitted(paths, 'git %s: %s' % (args[0], why))
        return cmd.text(out).strip()

    def _hash_text(self, data, paths):
        return self._git(['hash-object', '-w', '--stdin'], paths, stdin=data)

    def _head(self):
        out, why = cmd.run_text(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], self.root)
        if not out:
            raise Failure('cannot read HEAD in %s: %s' % (self.root, why or 'no commit'))
        return out

    def _tree(self, head, lines, paths):
        """The tree of `head` with `lines` (`update-index --index-info` input) applied, built in a
        temporary index."""
        tmp = tempfile.mkdtemp(prefix='kit-backlog-')
        try:
            env = dict(cmd.scrubbed_env(), GIT_INDEX_FILE=os.path.join(tmp, 'index'))
            self._git(['read-tree', head], paths, env=env)
            self._git(['update-index', '--index-info'], paths, env=env,
                      stdin=('\n'.join(lines) + '\n').encode('utf-8'))
            return self._git(['write-tree'], paths, env=env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _commit(self, blobs, message, paths):
        """Commit `blobs` ({path: blob id, or None to delete}) on HEAD as one commit: built in a
        temporary index, published by `update-ref HEAD <new> <old>` and rebuilt on a HEAD another
        commit moved (ATTEMPTS times). Then the real index takes the same entries for those paths
        alone. Returns the new commit, or None when HEAD already holds them. No ref moves until the
        whole commit exists, so a failure leaves no commit holding part of the write; it is
        raised (_uncommitted), never swallowed."""
        subject = message.split('\n', 1)[0]
        last = ''
        for attempt in range(ATTEMPTS):
            head = self._head()
            zero = '0' * len(head)
            lines = ['100644 %s\t%s' % (sha, p) if sha else '0 %s\t%s' % (zero, p)
                     for p, sha in sorted(blobs.items())]
            tree = self._tree(head, lines, paths)
            if tree == self._git(['rev-parse', head + '^{tree}'], paths):
                self._sync_index(lines, None, paths)
                return None
            new = self._git(['commit-tree', tree, '-p', head, '-F', '-'], paths,
                            stdin=message.encode('utf-8'))
            r = cmd.call(['update-ref', '-m', 'backlog: ' + subject, 'HEAD', new, head], self.root)
            if r.rc == 0:
                break
            last = r.err or 'git update-ref exited %s' % r.rc
            now = self._head()
            if now == new:                           # rc None: timed out after the move
                break
            if now == head and 'cannot lock ref' not in last:
                raise self._uncommitted(paths, 'git update-ref HEAD: %s' % last)
            time.sleep(random.uniform(0.05, 0.25) * (attempt + 1))
        else:
            raise self._uncommitted(paths, 'HEAD moved or stayed locked under %d attempts (%s)'
                                    % (ATTEMPTS, last))
        self._sync_index(lines, new, paths)
        return new

    def _sync_index(self, lines, new, paths):
        """Give the real index `lines` for those paths only, so `git status` shows them clean."""
        why = ''
        for attempt in range(ATTEMPTS):
            r = cmd.call(['update-index', '--index-info'], self.root,
                         stdin=('\n'.join(lines) + '\n').encode('utf-8'))
            if r.rc == 0:
                return
            why = r.err or 'git update-index exited %s' % r.rc
            if 'index.lock' not in why:
                break
            time.sleep(random.uniform(0.05, 0.25) * (attempt + 1))
        raise Failure('%s committed%s in %s, but its index was not updated (%s): `git status` '
                      'shows them staged backwards until `git reset -q -- <path>` for each'
                      % (', '.join(paths), ' as %s' % new[:10] if new else '', self.root, why))

    def pending(self):
        """`[(kind, path)]` of every path under the store folder whose worktree file differs from
        HEAD, in path order: `added`, `modified` or `deleted` for a record (`<dir>/<ID>.md`), `not
        a record` for anything else there. Failure when git does not answer."""
        rel = self.rel.replace('\\', '/')
        out, why = cmd.run(['-c', 'core.quotePath=false', 'status', '--porcelain', '-z',
                            '--untracked-files=all', '--no-renames', '--', rel + '/'], self.root)
        if out is None:
            raise Failure('cannot read the status of %s: %s' % (self.root, why))
        changed = set()
        for entry in cmd.text(out).split('\0'):
            if len(entry) > 3:
                changed.add(entry[3:])
        tracked, why = cmd.run(['ls-tree', '-r', '-z', '--name-only', 'HEAD', '--', rel + '/'],
                               self.root)
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
        Returns the new commit, or None when HEAD already holds them."""
        if not self.commit_branch():
            raise Failure('%s is not the PM data worktree: its store writes are not committed '
                          'here' % self.root)
        bad = [p for p in paths if not self.is_record_path(p)]
        if bad or not paths:
            raise Failure('not a record path of %s/: %s' % (self.rel, ', '.join(bad) or 'none given'))
        paths = sorted(set(paths))
        present = [p for p in paths if os.path.isfile(os.path.join(self.root, *p.split('/')))]
        blobs = dict.fromkeys(paths)
        if present:                                   # as `git add` would: the path's filters
            shas = self._git(['hash-object', '-w', '--'] + present, paths).split('\n')
            if len(shas) != len(present):
                raise self._uncommitted(paths, 'git hash-object printed %d ids for %d files'
                                        % (len(shas), len(present)))
            blobs.update(zip(present, shas))
        return self._commit(blobs, message, paths)

    def history(self, rid, snap):
        return _last_closing(self.root, rid, 'HEAD')

    def removal(self, rid, snap):
        """The newest commit on HEAD that deleted `<dir>/<rid>.md` (a Removal), or None."""
        return last_removal(self.root, 'HEAD', self.rel, rid)

    def uncommitted_removal(self, rid):
        """True when HEAD tracks `<dir>/<rid>.md` and the worktree no longer holds it: a write
        here deleted it (`close`, `retire`, which commit nothing outside the PM data worktree)
        or a hand removed it. False on
        an unborn HEAD. Failure when git did not answer (killed, timed out): fail closed."""
        if not PLAIN_ID.match(rid):
            raise Usage('`%s` is not a plain id: as a git pathspec it could match other files' % rid)
        if os.path.exists(os.path.join(self.dir, rid + '.md')):
            return False
        r = cmd.call(['cat-file', '-e', 'HEAD:%s/%s.md' % (self.rel.replace('\\', '/'), rid)],
                     self.root)
        if r.rc is None:
            raise Failure(r.err)
        return r.rc == 0

    def touched(self, snap):
        return _touch_times(self.root, 'HEAD', self.rel)

    def landed(self, main_branch):
        """`(rev, folder)` holding the landed records: the folder on the main branch."""
        return main_branch, self.rel


def open_backend(root, cfg, fetch=True, store=None):
    """The backend `.kit.json` "backlog".store names (absent: the schema default, `ref`), or
    `store` when given (`migrate --to`): `ref` the hidden ref, `branch` the `backlog` branch,
    `main` docs/backlog in `root`, any other path that folder in `root`."""
    store = store or cfg.section('backlog')['store']
    remote = cfg.remote
    if store == 'ref':
        return RefBackend(root, remote, STORE_REF, STORE_REF, fetch=fetch)
    if store == 'branch':
        return RefBackend(root, remote, 'refs/heads/' + BRANCH,
                          'refs/remotes/%s/%s' % (remote, BRANCH),
                          no_remote_ref='refs/heads/' + BRANCH, fetch=fetch)
    return MainBackend(root, 'docs/backlog' if store == 'main' else store)


# --- the write loop -------------------------------------------------------------------------------

class Change(object):
    """What a plan decided: `writes` {stem: new text, or None to delete}, the commit subject, the
    lines to print, and the ruling it cites."""

    def __init__(self, writes, subject, lines=(), ruling=None):
        self.writes = dict(writes)
        self.subject = subject
        self.lines = list(lines)
        self.ruling = ruling


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
            change = plan(snap)
            if not change.writes:
                return change, snap.tip
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
