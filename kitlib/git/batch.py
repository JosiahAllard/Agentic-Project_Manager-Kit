"""One git process for many reads (ported from the PM kit's tools/_git.py, TOOLS-GIT-BATCH).

A tool that asks git one question per item pays one process start per item, and on Windows under
load that start is the run time. Each helper here answers a whole list in one git process:

  cat_file(specs, cwd)        every `<rev>:<path>` / `<hash>` object, one `cat-file --batch -z`
  commits(revs, cwd)          which revs name a commit, one `cat-file --batch-check -z`
  log_records(out)            split a `git log --format=%x00<header> --name-only` reply per commit
  chunks(items)               split an argument list so one command line stays under the Windows limit
  ancestry(rev, cwd)          rev's first-parent chain and every ancestor, one `rev-list --parents`
  fixture_env(environ)        GIT_CONFIG_* entries that turn off auto gc and maintenance (tests)
  unquote_path(name)          a C-quoted diff header path back to text
  CatFile(root, check=False)  one long-lived `cat-file --batch` asked lazily, one request at a time

Requests go NUL-separated (`-z`, git 2.38+), so a path with a space or newline is one request.
Every git child goes through cmd.call (scrubbed environment, timeout).
"""
import os
import re
import subprocess
import threading

from kitlib.proc import job as _job

from . import cmd

_POPEN = subprocess.Popen      # the fake answers only while no test has swapped Popen for a stub

# `<oid> <type> <size>\n` - sha1 or sha256.
_FOUND = re.compile(rb'([0-9a-f]{40,64}) (\w+) (\d+)\n')

# A Windows command line is capped at 32,767 characters; keep a chunk well under it.
ARG_BUDGET = 16000


def cat_file(specs, cwd, timeout=None, env=None):
    """`({spec: (type, bytes) or None}, None)` for every spec in ONE `git cat-file --batch -z`, or
    `(None, why)`. None marks a spec git answered `missing` or `ambiguous`. An empty list starts
    no process. `timeout` None waits as long as git takes (a large batch is slow, not hung)."""
    specs = list(specs)
    if not specs:
        return {}, None
    reqs = [s.encode('utf-8') for s in specs]
    if any(b'\0' in r for r in reqs):
        return None, 'a spec carries a NUL, which `cat-file --batch -z` cannot take'
    data, why = cmd.run(['cat-file', '--batch', '-z'], cwd, stdin=b'\0'.join(reqs) + b'\0',
                        timeout=timeout, env=env)
    if data is None:
        return None, why
    out, pos = {}, 0
    for spec, req in zip(specs, reqs):
        m = _FOUND.match(data, pos)
        if m:
            end = m.end() + int(m.group(3))
            if len(data) < end + 1:
                return None, 'git cat-file --batch was cut short inside `%s`' % spec
            out[spec] = (m.group(2).decode('ascii'), data[m.end():end])
            pos = end + 1                            # the LF after every object's content
        elif data.startswith(req + b' ', pos):       # `<request> missing` / `ambiguous`
            nl = data.find(b'\n', pos + len(req) + 1)
            if nl < 0:
                return None, 'git cat-file --batch stopped inside its answer for `%s`' % spec
            out[spec] = None
            pos = nl + 1
        else:
            return None, "cannot parse git cat-file's reply for `%s` at byte %d" % (spec, pos)
    return out, None


def commits(revs, cwd, timeout=None, env=None):
    """`({rev: full_hash or None}, None)`: which of `revs` name a commit (a tag is peeled), in ONE
    `git cat-file --batch-check -z` over `<rev>^{commit}`, or `(None, why)`. A rev containing a
    line break fails the whole call: its `missing` echo would shift every later answer."""
    revs = list(dict.fromkeys(revs))
    if not revs:
        return {}, None
    bad = [r for r in revs if '\n' in r or '\r' in r or '\0' in r]
    if bad:
        return None, 'a rev carries a line break or NUL (%r), which would misalign every later answer' % bad[0]
    reqs = [('%s^{commit}' % r).encode('utf-8') for r in revs]
    data, why = cmd.run(['cat-file', '--batch-check', '-z'], cwd, stdin=b'\0'.join(reqs) + b'\0',
                        timeout=timeout, env=env)
    if data is None:
        return None, why
    lines = data.split(b'\n')
    if len(lines) < len(revs):
        return None, 'git cat-file --batch-check answered %d of %d revs' % (len(lines), len(revs))
    out = {}
    for rev, line in zip(revs, lines):
        m = _FOUND.match(line + b'\n')
        out[rev] = m.group(1).decode('ascii') if m and m.group(2) == b'commit' else None
    return out, None


def log_records(out):
    """`[(header, [path, ...])]` from a `git log --format=%x00<header> --name-only` reply (text).
    NUL separates records: a caller's `str.strip()` eats the 0x1c-0x1f separators."""
    records = []
    for chunk in out.split('\x00')[1:]:
        lines = chunk.split('\n')
        records.append((lines[0], [ln for ln in lines[1:] if ln.strip()]))
    return records


def log_records_names(text):
    """`[(header, [path, ...])]` from `git -c core.quotePath=false log --name-only
    --format=%x01<header>%x02` (text): non-ASCII names come out as written. A name with a line
    break, tab or quote is still quoted by git; none of the kit's paths has one."""
    records = []
    for chunk in text.split('\x01')[1:]:
        header, _sep, rest = chunk.partition('\x02')
        records.append((header, [ln for ln in rest.split('\n') if ln.strip()]))
    return records


def chunks(items, budget=ARG_BUDGET):
    """`items` in consecutive lists whose joined length stays under `budget` characters."""
    batch, size = [], 0
    for item in items:
        if batch and size + len(item) + 1 > budget:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item) + 1
    if batch:
        yield batch


def ancestry(rev, cwd, timeout=None, env=None):
    """`((first_parent_chain, ancestors), None)` for `rev`, both sets of full hashes holding
    `rev`'s own commit, from ONE `git rev-list --parents`; or `(None, why)`. `x in ancestors` is
    `git merge-base --is-ancestor x <rev>` for a full hash x."""
    if '\n' in rev or '\r' in rev or '\0' in rev or rev.startswith('-'):
        return None, 'not a rev: %r' % rev
    data, why = cmd.run(['rev-list', '--parents', rev, '--'], cwd, timeout=timeout, env=env)
    if data is None:
        return None, why
    parents, tip = {}, None
    for line in data.decode('ascii', 'replace').splitlines():
        hashes = line.split()
        if not hashes:
            continue
        if tip is None:
            tip = hashes[0]
        parents[hashes[0]] = hashes[1:]
    if tip is None:
        return None, 'git rev-list %s listed no commits' % rev
    chain, at = set(), tip
    while at and at not in chain:
        chain.add(at)
        at = (parents.get(at) or [None])[0]
    return (chain, set(parents)), None


# Every commit in a throwaway repository otherwise starts a `git maintenance run --auto` child.
# Measured 2026-10-01 in the PM kit's tests (TOOLS-GIT-SPAWN-DIET): 3,075 git processes, 555 of
# them started by git itself; 209 with this.
FIXTURE_CONFIG = (('maintenance.auto', 'false'), ('gc.auto', '0'))


def fixture_env(environ=None):
    """The GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> entries (git 2.31+) adding
    FIXTURE_CONFIG after any the environment already carries; `{}` when it carries all of them.
    For tests: merge into the environment once, and every git a test starts inherits it."""
    env = os.environ if environ is None else environ
    try:
        n = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        n = 0
    have = {(env.get('GIT_CONFIG_KEY_%d' % i), env.get('GIT_CONFIG_VALUE_%d' % i)) for i in range(n)}
    out = {}
    for key, value in FIXTURE_CONFIG:
        if (key, value) in have:
            continue
        out['GIT_CONFIG_KEY_%d' % n] = key
        out['GIT_CONFIG_VALUE_%d' % n] = value
        n += 1
    if out:
        out['GIT_CONFIG_COUNT'] = str(n)
    return out


def unquote_path(name):
    """A path as git C-quotes it (a double-quoted name with backslash escapes and octal bytes)
    back to text; an unquoted name is returned as it is."""
    if not (len(name) >= 2 and name[0] == name[-1] == '"'):
        return name
    esc = {'n': 10, 't': 9, 'r': 13, 'a': 7, 'b': 8, 'f': 12, 'v': 11, '"': 34, '\\': 92}
    out, i = bytearray(), 1
    while i < len(name) - 1:
        c = name[i]
        if c == '\\' and i + 1 < len(name) - 1:
            nxt = name[i + 1]
            if nxt in '01234567':
                out.append(int(name[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            out.append(esc.get(nxt, ord(nxt) & 0xFF))
            i += 2
            continue
        out.extend(c.encode('utf-8'))
        i += 1
    return out.decode('utf-8', 'replace')


_HEX = frozenset(b'0123456789abcdef')


class CatFile:
    """`with CatFile(root) as cat: cat.read('HEAD', 'docs/a.md')` -> bytes or None.
    `CatFile(root, check=True)` runs `--batch-check`; `size(rev, path)` -> int or None.

    Request and answer alternate (one line written and flushed, its whole answer read), so nothing
    piles up in a pipe and callers can ask lazily. A missing object, a non-blob, a path git cannot
    take on one line, or a dead git answers None. A git that died sets `dead` (and `dead_reason`)
    and every later read answers None: a caller whose clean result rests on the reads checks
    `dead` before reporting clean. Nothing is printed (kitlib prints nothing).

    Every request and its whole answer must arrive within `timeout` seconds (default
    cmd.GIT_TIMEOUT; None waits forever), as cmd.call's contract says: a git that hangs is killed
    and counts as dead. The exchange runs on its own thread and the caller waits for it with a
    deadline. Git runs in its own job object (kill-on-close: a reader only reads), and a missed
    deadline ends the job, so the whole tree goes: under PowerShell `git` is a launcher whose
    child (the real git) holds the pipes, and killing only the launcher left that child running and
    the exchange blocked on its pipe. close() closes the pipes only once a timed-out exchange's
    thread has ended (closing them sooner would wait for it); if something outside the job still
    holds a pipe, the thread is left blocked and the pipes are left to it.

    Not thread-safe: requests and answers share one pipe, so one CatFile serves one thread."""

    def __init__(self, root, check=False, timeout=cmd.GIT_TIMEOUT):
        self.root = root
        self.check = check
        self.timeout = timeout
        self._p = None
        self._job = None
        self._stuck = None           # the thread of a timed-out exchange: it owns the pipes
        self.dead_reason = None

    @property
    def dead(self):
        return self.dead_reason is not None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _proc(self):
        if (self._p is None and not self.dead and cmd.fake_active()
                and subprocess.Popen is _POPEN):
            # Tests on the fake git: the same exchange, answered in process (no job, no git). A
            # test that stubbed Popen to play a hung or broken git keeps its stub.
            from kitlib.git import fake
            self._p = fake.BatchProcess(
                ('-C', self.root, 'cat-file', '--batch-check' if self.check else '--batch'),
                env=cmd.child_env())
        if self._p is None and not self.dead:
            job = None
            try:
                job = _job.Job(kill_on_close=True)
                self._p = job.start(
                    ('git', '-C', self.root, 'cat-file', '--batch-check' if self.check else '--batch'),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=cmd.child_env())
                self._job = job
            except OSError as e:
                if job is not None:
                    job.close()
                self._fail('could not start: %s' % e)
        return self._p

    def _ask(self, rev, path):
        """`(kind, size, body)` (body None in check mode), or None: no such object, or git dead."""
        spec = '%s:%s' % (rev, path)
        if '\n' in spec or '\r' in spec or '\0' in spec:
            return None
        p = self._proc()
        if p is None:
            return None
        result = {}

        def exchange():
            try:
                result['answer'] = self._exchange(p, spec)
            except BaseException as e:      # a reader that broke counts as a dead git, never a raise
                result['answer'] = (None, 'reading the answer for %s failed: %r' % (spec, e))

        if self.timeout is None:
            exchange()
        else:
            t = threading.Thread(target=exchange, name='kit-catfile', daemon=True)
            t.start()
            t.join(self.timeout)
            if t.is_alive():
                self._stuck = t
                self._kill(p)
                self._fail('no answer for %s within %s seconds; git was killed' % (spec, self.timeout))
                return None
        got, why = result['answer']
        if why:
            self._fail(why)
            return None
        return got

    def _exchange(self, p, spec):
        """One request and its whole answer: `(answer or None, None)` or `(None, why git is dead)`."""
        try:
            p.stdin.write(spec.encode('utf-8') + b'\n')
            p.stdin.flush()
            header = p.stdout.readline()
        except (OSError, ValueError):
            header = b''
        if not header:
            return None, 'no answer for %s' % spec
        parts = header.split()
        if (len(parts) != 3 or len(parts[0]) not in (40, 64) or not all(c in _HEX for c in parts[0])
                or not parts[2].isdigit() or parts[1] not in (b'blob', b'tree', b'commit', b'tag')):
            return None, None
        kind, size = parts[1], int(parts[2])
        if self.check:
            return (kind, size, None), None
        try:
            body = p.stdout.read(size + 1)          # the body, then git's trailing LF
        except (OSError, ValueError):
            body = b''
        if len(body) != size + 1:
            return None, 'short body for %s' % spec
        return (kind, size, body[:size]), None

    def _fail(self, why):
        if self.dead_reason is None:
            self.dead_reason = 'git cat-file in %s died: %s' % (self.root, why)
        self.close()

    def read(self, rev, path):
        """The blob's raw bytes, or None."""
        if self.check:
            raise ValueError('CatFile(check=True) has no bodies; use size()')
        got = self._ask(rev, path)
        if got is None:
            return None
        kind, _, body = got
        return body if kind == b'blob' else None

    def text(self, rev, path):
        """The blob as text (cmd.text), '' when there is no such blob."""
        data = self.read(rev, path)
        return '' if data is None else cmd.text(data)

    def size(self, rev, path):
        """The object's size in bytes, or None (`--batch-check` mode)."""
        if not self.check:
            raise ValueError('size() needs CatFile(check=True)')
        got = self._ask(rev, path)
        return None if got is None else got[1]

    def _kill(self, p):
        """End git's whole tree (its job), or git alone when it has no job."""
        if self._job is None or not self._job.terminate():
            try:
                p.kill()
            except OSError:
                pass

    def close(self):
        p, self._p = self._p, None
        if p is None:
            return
        stuck, self._stuck = self._stuck, None
        if stuck is None:
            self._close_pipes(p)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._kill(p)
            p.wait()
        if stuck is not None:
            # The stuck exchange holds a pipe's buffer lock until the pipe ends; closing the pipe
            # first would wait for it. Git's tree is gone now: the pipe ends unless something
            # outside the job holds it.
            stuck.join(cmd.KILL_GRACE)
            if not stuck.is_alive():
                self._close_pipes(p)
        job, self._job = self._job, None
        if job is not None:
            job.close()                      # kill-on-close: anything git left goes with it

    @staticmethod
    def _close_pipes(p):
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except (OSError, ValueError):
                pass
