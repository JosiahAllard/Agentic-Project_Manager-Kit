"""Where each launched session's peer pipe was seen (docs/launcher.md "Pipe addresses").

Every live session publishes a registry record, <config folder>/sessions/<pid>.json, holding its
pid, sessionId, startedAt (ms), procStart (or procStartFt: either may carry the process creation
time), entrypoint, messagingSocketPath and the name the launcher gave it. A secret key file sits
beside it; nothing here opens it.

  registry_dirs(settings)   the registry folders of every configured account, each once
  records(dirs)             (folder, record) of every <digits>.json that reads as an object
  proc_start(rec)           the record's creation time as text, or None
  is_live(rec, ctime)       its pid runs now with that creation time (a stopped session's record
                            can stay behind, so a record alone is never proof)
  find(dirs, name, after_ms, ctime)   the newest live record of that name started at or after
                            after_ms, or None
  record(...)               poll for a just-started session's record and write its pipes.json
                            entry; the line to say when it is not found in time
  remove(settings, name)    drop a stopped session's entry

pipes.json, in launcher.state_dir: {name: {pid, session_id, pipe, proc_start, entrypoint,
recorded_at}}. It is a hint and an audit, never the truth: the pipe changes on every restart, so a
sender resolves the live record at send time. No token is ever written. Two launchers writing at
once can drop one entry (the last os.replace wins); the sender's registry scan still finds it.
"""
import os
import re

from kitlib.launcher import state
from kitlib.launcher.settings import LaunchError

PIPES = 'pipes.json'
REGISTRY = 'sessions'           # the registry folder under each account's config folder
POLL_S = 1.0
TRIES = 20                      # 20 polls, 1 s apart: at most about 20 s after the window opens
NOT_YET = 'pipe not recorded yet: the hub resolves it at send time'
_RECORD = re.compile(r'^[0-9]{1,10}\.json\Z')


def registry_dirs(settings):
    """The registry folders that exist: the default config folder's, then each account's and
    org's, the same folder (a junction, a repeated entry) listed once."""
    cands = [settings.default_folder()]
    for n in settings.account_numbers():
        cands += settings.account_dirs(n)
    for n in sorted(settings.orgs, key=int):
        cands.append(settings.org_dir(n))
    seen, out = set(), []
    for c in cands:
        d = os.path.join(c, REGISTRY)
        if not os.path.isdir(d):
            continue
        key = os.path.normcase(os.path.realpath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def records(dirs):
    """[(folder, record)] of every <digits>.json in dirs that reads as a JSON object."""
    out = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not _RECORD.match(n):
                continue
            try:
                rec = state.read_json(os.path.join(d, n))
            except LaunchError:
                continue
            if isinstance(rec, dict):
                out.append((d, rec))
    return out


def proc_start(rec):
    """The record's process creation time as text: procStart, else procStartFt; None when
    neither is a whole number (or its text)."""
    for k in ('procStart', 'procStartFt'):
        v = rec.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            return str(v)
    return None


def is_live(rec, ctime):
    """True when the record's pid runs now and ctime(pid) (the creation time, or None) equals its
    proc_start."""
    pid = rec.get('pid')
    start = proc_start(rec)
    if type(pid) is not int or not 0 < pid <= 0xffffffff or start is None:
        return False
    try:
        now = ctime(pid)
    except (OSError, ValueError):
        return False
    return now is not None and str(now) == start


def find(dirs, name, after_ms, ctime):
    """The live record named `name` whose startedAt is at or after after_ms; the newest
    startedAt when several are; None when there is none."""
    hits = []
    for _d, rec in records(dirs):
        started = rec.get('startedAt')
        if rec.get('name') != name or isinstance(started, bool) or not isinstance(started, (int, float)):
            continue
        if started >= after_ms and is_live(rec, ctime):
            hits.append(rec)
    return max(hits, key=lambda r: r['startedAt']) if hits else None


def entry(rec, now):
    """The pipes.json entry of a registry record: no token, no free text but the names."""
    return {'pid': rec.get('pid'), 'session_id': rec.get('sessionId'),
            'pipe': rec.get('messagingSocketPath'), 'proc_start': proc_start(rec),
            'entrypoint': rec.get('entrypoint'), 'recorded_at': now.isoformat(timespec='seconds')}


def record(settings, name, after_ms, ctime, sleep, now, tries=TRIES, poll_s=POLL_S):
    """Poll the registry for the session `name` started at or after after_ms (epoch ms) and
    write its pipes.json entry: None when written (or when launcher.state_dir is unset: there is
    nowhere to write), else the one line to say. ctime(pid) is the process creation time, sleep(s)
    waits, now() is the aware time recorded."""
    try:
        p = state.path(settings, PIPES)
    except LaunchError:
        return None
    for i in range(max(1, tries)):
        rec = find(registry_dirs(settings), name, after_ms, ctime)
        if rec is not None:
            try:
                data = state.read_json(p) or {}
                data[name] = entry(rec, now())
                state.write_json(p, data)
            except (OSError, LaunchError) as e:
                return 'pipe not recorded (%s): the hub resolves it at send time' % e
            return None
        if i < tries - 1:
            sleep(poll_s)
    return NOT_YET


def remove(settings, name):
    """Drop the pipes.json entry of `name`: True when one was removed. Nothing to do (no
    state_dir, no file, no entry) is False; a file that cannot be read or written is a
    LaunchError or an OSError for the caller to say."""
    try:
        p = state.path(settings, PIPES)
    except LaunchError:
        return False
    data = state.read_json(p)
    if not data or name not in data:
        return False
    del data[name]
    state.write_json(p, data)
    return True


def read(settings):
    """pipes.json as {name: entry}; {} when there is none or no state_dir."""
    try:
        return state.read_json(state.path(settings, PIPES)) or {}
    except LaunchError:
        return {}


def launch_ms(when):
    """An aware datetime as epoch milliseconds (the registry's startedAt unit)."""
    return when.timestamp() * 1000.0
