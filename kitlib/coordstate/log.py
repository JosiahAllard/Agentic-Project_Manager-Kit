"""A coordinator's event log (docs/coord-state.md): `events-<session>.jsonl` in its coordinator
folder, `<agent temp>/<repo slug>-coordinator/` (the folder its state note is in; agent temp is
AGENT_TEMP, else TEMP, else the home folder), one JSON line per event a kit tool saw.

The log is opt-in per session: it exists only after `coord_state.py start`, and note() appends
only to a log that exists, so a session that never started one pays one environment read and one
folder listing per call, and gets no file. `stop` renames the log aside (OFF); `start` puts it
back. The session is the launcher's PM_KIT_SESSION_NAME (kitlib.sessionmark), which every tool
call, subagent and detached landing of that session inherits; a name that is not a plain file
name part (SAFE) is no session.

A line is `{"v": 1, "kind": ..., "utc": ..., <fields>}`; a field that is None is left out. Appends
hold the OS lock (kitlib.fs.treelock) on LOCK beside the log, so lines from concurrent tools never
interleave, and write the whole line in one write. A busy lock is tried every PAUSE seconds for
WAIT seconds; one try can add treelock's READ_TRIES * READ_SLEEP (0.5 s) while it reads the
holder's record, so a note waits at most BOUND seconds. A log at MAX_BYTES takes no more lines
(Full); `coord_state.py show` compacts it past COMPACT_AT (compact). A line a crash cut short is
skipped on read.

note() is the tools' call and is best effort: it never raises (KeyboardInterrupt and SystemExit
aside) and never changes what the tool does or says; no session, no log, a busy lock, a full log
or a failed write: no line.

  agent_temp(environ)                the agent temp folder
  session_name(environ)              PM_KIT_SESSION_NAME when it is SAFE, else None
  files(folder, session)             Files: the log, its OFF name, its lock, judgments, state note
  locate(environ, session)           the folder whose log is on for `session`, or None
  entry(kind, fields, now)           the line's fields, in order
  append(path, fields, wait)         append one line under the lock; Busy, Full, or OSError (a
                                     log that is gone is FileNotFoundError: never re-created)
  note(kind, environ, now, **fields) the whole step for a tool: the log's path, or None
  note_submit(outcome, ...)          land.py / gate.py land --submit: a land_submit event
  note_backlog(verb, id, branch)     after a backlog write: a backlog event
  read_events(path)                  (events, skipped lines, bytes read)
  compact(path, keep, seen, wait)    keep only the `keep` lines of what was read, under the lock
  seed(files)                        add a hand-written state note to the judgments file
  start(environ, slug)               turn the log on; Started
  stop(environ)                      turn it off; the OFF path, or None when it was not on
"""
import collections
import datetime
import glob
import json
import os
import re
import time

from kitlib import sessionmark

VERSION = 1
FOLDER_SUFFIX = '-coordinator'
EVENTS = 'events-%s.jsonl'
OFF = 'events-%s.jsonl.off'
LOCK = '.events-%s.lock'
JUDGMENTS = 'judgments-%s.md'
STATE = 'state-%s.md'
SAFE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
MAX_BYTES = 512 * 1024
COMPACT_AT = 128 * 1024
WAIT = 0.2
PAUSE = 0.02
BOUND = WAIT + 0.5 + PAUSE          # the lock wait's worst case, in seconds (module docstring)
NOTE_HEAD = '# Coordinator state: '     # how a note show rendered begins (kitlib.coordstate.render)

Files = collections.namedtuple('Files', 'folder events off lock judgments state')
Started = collections.namedtuple('Started', 'files already resumed seeded')


class LogError(Exception):
    """The log cannot be turned on, or a tool's own call cannot write it (said to the caller)."""


class Busy(LogError):
    """The log's lock stayed held for the whole wait."""


class Full(LogError):
    """The log is at MAX_BYTES."""


def agent_temp(environ=None):
    env = os.environ if environ is None else environ
    return env.get('AGENT_TEMP') or env.get('TEMP') or os.path.expanduser('~')


def session_name(environ=None):
    env = os.environ if environ is None else environ
    name = (env.get(sessionmark.SESSION_NAME_ENV) or '').strip()
    return name if SAFE.match(name) else None


def files(folder, session):
    return Files(folder, os.path.join(folder, EVENTS % session), os.path.join(folder, OFF % session),
                 os.path.join(folder, LOCK % session), os.path.join(folder, JUDGMENTS % session),
                 os.path.join(folder, STATE % session))


def _folders_with(temp, name):
    hits = glob.glob(os.path.join(glob.escape(temp), '*' + FOLDER_SUFFIX, glob.escape(name)))
    hits = [h for h in hits if os.path.isfile(h)]
    return [os.path.dirname(h) for h in sorted(hits, key=_mtime, reverse=True)]


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def locate(environ=None, session=None):
    """The coordinator folder holding `session`'s log (default: this session's), the newest when
    more than one does; None when there is no session or no log is on."""
    session = session or session_name(environ)
    if not session or not SAFE.match(session):
        return None
    found = _folders_with(agent_temp(environ), EVENTS % session)
    return found[0] if found else None


def entry(kind, fields, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    line = collections.OrderedDict([('v', VERSION), ('kind', kind),
                                    ('utc', now.astimezone(datetime.timezone.utc)
                                     .strftime('%Y-%m-%dT%H:%M:%SZ'))])
    for key in sorted(fields):
        if fields[key] is not None and key not in line:
            line[key] = fields[key]
    return line


def _locked(path, wait):
    """The log's lock, taken: Busy when it stayed held for `wait` seconds."""
    from kitlib.fs import treelock
    folder = os.path.dirname(path)
    name = os.path.basename(path)
    session = name[len('events-'):-len('.jsonl')] if name.endswith('.jsonl') else name
    lock = treelock.TreeLock(os.path.join(folder, LOCK % session), what='append a coordinator event')
    deadline = time.monotonic() + wait
    while True:
        try:
            return lock.acquire()
        except treelock.TreeBusy:
            if time.monotonic() >= deadline:
                raise Busy('%s stayed held for %s s' % (lock.path, wait))
        time.sleep(PAUSE)


def append(path, fields, wait=WAIT):
    data = (json.dumps(fields, separators=(',', ':')) + '\n').encode('utf-8')
    lock = _locked(path, wait)
    try:
        size = os.path.getsize(path)
        if size + len(data) > MAX_BYTES:
            raise Full('%s is at its %d-byte cap; coord_state.py show compacts it' % (path, MAX_BYTES))
        if size and _last_byte(path) != b'\n':
            data = b'\n' + data          # a crash cut the last line short: this one stays whole
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, 'O_BINARY', 0))
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    finally:
        lock.release()
    return path


def _last_byte(path):
    with open(path, 'rb') as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1)


def note(kind, environ=None, now=None, **fields):
    """Append one `kind` event to this session's log when it is on: its path, else None. Never
    raises (module docstring)."""
    try:
        session = session_name(environ)
        folder = locate(environ, session)
        if folder is None:
            return None
        return append(files(folder, session).events, entry(kind, fields, now), WAIT)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:          # a busy lock, a full log, a failed write: the tool goes on
        return None


def note_submit(outcome, branches, tips, reviewed_by=None, environ=None):
    """land.py --submit and gate.py land --submit, once the submission is said: a `land_submit`
    event when it was taken (code 0), naming its output file. Never raises."""
    try:
        if getattr(outcome, 'code', None) != 0:
            return None
        facts = getattr(outcome, 'facts', None) or {}
        if isinstance(reviewed_by, (list, tuple)):
            reviewed_by = ', '.join(str(r) for r in reviewed_by) or None
        return note('land_submit', environ, branches=list(branches or ()), tips=list(tips or ()),
                    reviewed_by=reviewed_by, output=facts.get('output'))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return None


def note_backlog(verb, record_id=None, branch=None, environ=None):
    """backlog.py, after a write it published: a `backlog` event. Never raises."""
    return note('backlog', environ, verb=verb, id=record_id, branch=branch)


def read_events(path):
    """`(events, skipped, size)`: each line that is a JSON object with a kind, oldest first, each
    given its line's place as `_n`; `skipped` counts the rest (a line a crash cut short); `size`
    is the bytes up to the end of the last whole line, for compact."""
    with open(path, 'rb') as fh:
        raw = fh.read()
    events, skipped = _parse(raw)
    return events, skipped, raw.rfind(b'\n') + 1


def _parse(raw):
    events, skipped = [], 0
    for n, line in enumerate(raw.split(b'\n')):
        if not line.strip():
            continue
        try:
            ev = json.loads(line.decode('utf-8'))
        except ValueError:
            skipped += 1
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get('kind'), str):
            skipped += 1
            continue
        ev['_n'] = n
        events.append(ev)
    return events, skipped


def compact(path, keep, seen, wait=WAIT):
    """Under the log's lock, keep of its first `seen` bytes (what read_events read) only the
    lines whose `_n` is in `keep`, and every byte appended since, whole: a temp file beside it,
    then a replace, so a reader sees the old log or the new one. False, nothing written, when the
    log is now shorter than `seen` (another compaction ran first); else True."""
    from kitlib.fs import replace
    lock = _locked(path, wait)
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
        if len(raw) < seen:
            return False
        lines = raw[:seen].split(b'\n')
        old = b''.join(line + b'\n' for n, line in enumerate(lines) if n in keep and line.strip())
        tmp = path + '.tmp'
        with open(tmp, 'wb') as fh:
            fh.write(old + raw[seen:])
        replace.replace(tmp, path)
    finally:
        lock.release()
    return True


def seed(f):
    """Add this session's state note to its judgments file when it was written by hand (it does
    not begin with NOTE_HEAD), since `show` overwrites the note: True when added. A note the
    judgments file already holds is left, so a start, a stop, another start and a show add it
    once."""
    try:
        with open(f.state, encoding='utf-8', errors='replace') as src:
            text = src.read()
    except FileNotFoundError:
        return False
    if not text.strip() or text.startswith(NOTE_HEAD):
        return False
    try:
        with open(f.judgments, encoding='utf-8', errors='replace') as src:
            held = src.read()
    except FileNotFoundError:
        held = ''
    if text.strip() in held:
        return False
    with open(f.judgments, 'a', encoding='utf-8', newline='\n') as dst:
        dst.write('\n' if held and not held.endswith('\n') else '')
        dst.write(text if text.endswith('\n') else text + '\n')
    return True


def start(environ=None, slug=None):
    """Turn this session's log on. A log already on stays as it is; a stopped one (OFF) is put
    back; else an empty log is made in the folder holding this session's state note, or in
    `<agent temp>/<slug>-coordinator`. A state note written by hand is added to the judgments
    file (seed). LogError when there is no session, or no folder and no slug."""
    session = session_name(environ)
    if not session:
        raise LogError('%s is not set to a session name (letters, digits, ".", "_", "-"), so this '
                       'session has no log; it is set by the kit launcher'
                       % sessionmark.SESSION_NAME_ENV)
    temp = agent_temp(environ)
    on = locate(environ, session)
    if on:
        return Started(files(on, session), True, False, False)
    found = (_folders_with(temp, OFF % session) or _folders_with(temp, STATE % session)
             or _folders_with(temp, JUDGMENTS % session))
    if found:
        folder = found[0]
    elif slug and SAFE.match(slug):
        folder = os.path.join(temp, slug + FOLDER_SUFFIX)
    else:
        raise LogError('no coordinator folder holds a note of %s under %s, and no usable --slug '
                       'was given' % (session, temp))
    f = files(folder, session)
    os.makedirs(folder, exist_ok=True)
    seeded = seed(f)
    if os.path.isfile(f.off):
        from kitlib.fs import replace
        replace.replace(f.off, f.events)
        return Started(f, False, True, seeded)
    fd = os.open(f.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0))
    os.close(fd)
    return Started(f, False, False, seeded)


def stop(environ=None):
    """Turn this session's log off: renamed to OFF under its lock (an older OFF replaced). The
    OFF path, or None when no log was on."""
    session = session_name(environ)
    folder = locate(environ, session)
    if folder is None:
        return None
    from kitlib.fs import replace
    f = files(folder, session)
    lock = _locked(f.events, WAIT * 10)
    try:
        replace.replace(f.events, f.off)
    finally:
        lock.release()
    return f.off
