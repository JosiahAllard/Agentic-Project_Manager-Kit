"""Claude Code's delivery of hub push notifications (docs/action-hub.md "Push notifications").

kitlib.hubsite.push decides what to send and when; this module sends it. Every live Claude Code
session publishes a registry record, <config folder>/sessions/<pid>.json (kitlib.launcher.pipes),
and listens on a named pipe for cross-session messages. A message is two JSON lines written to
that pipe: an auth frame carrying the session's peer token, then a user frame whose content is
wrapped in <cross-session-message from-mode="prompting">. The session takes it as its next
prompt. A live probe proved this with no sender endpoint, so the hub publishes no
registry entry of its own and gets no receipt back: `written` means the bytes reached the pipe.

  HubPush(settings)     ClaudeAdapter.push()'s object; settings is kitlib.launcher.settings
    deliver(recipient, text)   push.Sent(state, name, session, ambiguous); state is one of
                        push.WRITTEN, push.NOT_WRITTEN, push.UNCERTAIN, push.NO_SESSION
    is_pm(recipient)    the recipient is the PM's session (push sends the PM one message)
    resolve(recipient)  Target(folder, record, name, ambiguous), or None
  canonical_pipe(value) the lower-case pipe name the key file's name hashes; ValueError
  build_packet(token, source_pipe, text)   the two frames, as bytes
  write_once(pipe, pid, creation, packet, timeout)   one write by the hidden helper
                        (peer_worker.py) in a kill-on-close job; the state

Resolution: a launcher key is its sessions.json name; `pm` is coordinators.json's pm_session_id
matched against the record's sessionId, then the names PM_NAME and pm-session; anything else is
taken as a session name. Only a live record counts (its pid runs with the record's creation time:
launch.py stop leaves records behind). Several live records of one name: the one pipes.json names
when its pid and creation time match, else the newest creation time, flagged ambiguous.

The peer token is read from the key file at send time only, checked against the record's
creation time, put in the packet and dropped. It is never logged, returned or shown by repr.
"""
from collections import namedtuple
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import quote
import uuid

from kitlib.hubsite import push
from kitlib.launcher import pipes, state
from kitlib.launcher.settings import LaunchError

PIPE = re.compile(r'\\\\\.\\pipe\\(?:(LOCAL)\\)?(cc-msg-[A-Za-z0-9_-]+)\Z', re.I)
LOCAL_PIPE = re.compile(r'\\\\\.\\pipe\\(?:LOCAL\\)?[A-Za-z0-9_-]{1,160}\Z', re.I)
SOURCE_PREFIX = r'\\.\pipe\LOCAL\cc-msg-kit-hub-'
PM_NAMES = ('Project Manager', 'pm-session')    # kitlib.launcher.start.PM_NAME, stop.PM_RC_NAME
TOKEN_LEN = 32
MAX_PACKET_BYTES = 256 * 1024
MAX_RESULT_BYTES = 2048
WRITE_TIMEOUT_S = 5
WORKER = Path(__file__).resolve().with_name('peer_worker.py')
PYTHON_FLAGS = ('-I', '-B')

Target = namedtuple('Target', 'folder record name ambiguous')


def canonical_pipe(value):
    """The pipe name as the key file's name hashes it: `local` and the cc-msg-* name lower case.
    ValueError for anything that is not a local cc-msg-* pipe."""
    if type(value) is not str:
        raise ValueError('invalid-local-peer-pipe')
    match = PIPE.fullmatch(value)
    if match is None:
        raise ValueError('invalid-local-peer-pipe')
    return '\\\\.\\pipe\\' + ('local\\' if match[1] else '') + match[2].lower()


def encode_frame(value):
    """One compact JSON line, UTF-8."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'


def source_pipe():
    """A fresh sender address nobody listens on: the hub takes no receipt."""
    return SOURCE_PREFIX + secrets.token_hex(8)


def build_packet(token, source, text):
    """The auth frame, then the user frame carrying text as a cross-session message from
    `source` (a pipe name) in prompting mode. Text holding < or > is a ValueError, so it cannot
    close the wrapper."""
    if type(token) is not str or len(token) != TOKEN_LEN:
        raise ValueError('no usable peer token')
    if type(text) is not str or '<' in text or '>' in text:
        raise ValueError('push text must not hold < or >')
    sender = 'uds:' + quote(source, safe=':_/.-')
    msg_id = str(uuid.uuid4())
    frame = {'msgV': 1, 'msg_id': msg_id, 'uuid': msg_id, 'type': 'user', 'priority': 'next',
             'from': sender, 'message': {'role': 'user', 'content':
             '<cross-session-message from="' + sender + '" from-mode="prompting">\n' + text
             + '\n</cross-session-message>'}}
    return encode_frame({'type': 'auth', 'token': token}) + encode_frame(frame)


def key_path(folder, rec):
    pipe = canonical_pipe(rec.get('messagingSocketPath'))
    return os.path.join(folder, '%d.%s.key' % (rec['pid'], hashlib.sha256(pipe.encode('utf-8')).hexdigest()))


def read_token(folder, rec):
    """The record's peer token from its key file; ValueError (naming no secret) when the key is
    missing, unreadable, of another process or not a token."""
    try:
        with open(key_path(folder, rec), encoding='utf-8-sig') as f:
            key = json.load(f)
    except (OSError, ValueError):
        raise ValueError('no readable key file') from None
    start = pipes.proc_start(rec)
    facts = [key[k] for k in ('procStart', 'procStartFt') if isinstance(key, dict) and k in key]
    if start is None or not facts or any(str(v) != start for v in facts):
        raise ValueError('the key file is not this process\'s')
    token = key.get('peerToken')
    if type(token) is not str or len(token) != TOKEN_LEN:
        raise ValueError('no usable peer token')
    return token


def _usable(rec):
    if rec.get('peerProtocol') != 1:
        return False
    try:
        canonical_pipe(rec.get('messagingSocketPath'))
    except ValueError:
        return False
    return LOCAL_PIPE.fullmatch(rec['messagingSocketPath']) is not None


def _read_result(stream, completed, limit=MAX_RESULT_BYTES):
    data = bytearray()
    try:
        while len(data) <= limit:
            chunk = stream.read(min(4096, limit + 1 - len(data)))
            if not chunk:
                completed.put(('read', bytes(data)))
                return
            data.extend(chunk)
        completed.put(('fault', None))
    except Exception:
        completed.put(('fault', None))


def _write_input(stream, data, completed):
    try:
        view = memoryview(data)
        while view:
            count = stream.write(view)
            if not count:
                raise OSError('closed input')
            view = view[count:]
        stream.close()
        completed.put(('write', None))
    except Exception:
        completed.put(('fault', None))


def _cleanup(job, process, threads):
    """End the helper's job first (kill-on-close), then reap it and its pumps. False when that
    could not be confirmed."""
    ok = True
    try:
        job.close()
    except OSError:
        ok = False
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return False
        for t in threads:
            if t.ident is not None:
                t.join(timeout=1)
        ok = not any(t.is_alive() for t in threads)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    ok = False
    return ok


def _decode(raw):
    """The one JSON object a helper wrote, or None."""
    if raw.count(b'\n') != 1 or not raw.endswith(b'\n'):
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def write_once(pipe, pid, creation, packet, timeout=WRITE_TIMEOUT_S):
    """Write packet once to the pipe served by pid with that creation time, through the helper
    in a kill-on-close job, and return the state: push.WRITTEN (all bytes taken), push.NOT_WRITTEN
    (a bad target, no helper, or the pipe's server is not that process: nothing was sent), else
    push.UNCERTAIN (a write was tried and its end is unknown; never re-sent at once)."""
    from kitlib.proc.job import Job
    if (type(pipe) is not str or LOCAL_PIPE.fullmatch(pipe) is None
            or type(pid) is not int or not 0 < pid <= 0xffffffff
            or type(creation) is not int or not 0 < creation < 2 ** 64
            or type(packet) is not bytes or not 0 < len(packet) <= MAX_PACKET_BYTES
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10):
        return push.NOT_WRITTEN
    if not WORKER.is_file():
        return push.NOT_WRITTEN
    meta = encode_frame({'pipe': pipe, 'pid': pid, 'creation': creation, 'length': len(packet)})
    if len(meta) > MAX_RESULT_BYTES:
        return push.NOT_WRITTEN
    job, process, threads = None, None, []
    try:
        job = Job(kill_on_close=True)
        process = job.start([sys.executable, *PYTHON_FLAGS, str(WORKER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                            bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        if job is not None:
            _cleanup(job, process, threads)
        return push.NOT_WRITTEN             # no helper ran: nothing was sent
    result = push.UNCERTAIN
    try:
        result = _pump(process, meta + packet, len(packet), timeout, threads)
    except Exception:
        result = push.UNCERTAIN
    finally:
        if not _cleanup(job, process, threads) and result == push.NOT_WRITTEN:
            result = push.UNCERTAIN         # a helper not seen to end: its write is unknown
    return result


def _pump(process, data, size, timeout, threads):
    """Feed the helper its request and read its one result line, within timeout; the state."""
    completed = queue.Queue(maxsize=2)
    threads += [threading.Thread(target=_write_input, args=(process.stdin, data, completed),
                                 name='hub-push-input', daemon=True),
                threading.Thread(target=_read_result, args=(process.stdout, completed),
                                 name='hub-push-result', daemon=True)]
    for t in threads:
        t.start()
    deadline, results = time.monotonic() + timeout, {}
    while len(results) < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return push.UNCERTAIN
        try:
            kind, got = completed.get(timeout=remaining)
        except queue.Empty:
            return push.UNCERTAIN
        if kind == 'fault' or kind in results:
            return push.UNCERTAIN
        results[kind] = got
    try:
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return push.UNCERTAIN
    said = _decode(results['read'])
    if process.returncode or said is None:
        return push.UNCERTAIN
    if said == {'state': 'not-written'}:
        return push.NOT_WRITTEN
    if (set(said) == {'state', 'bytes_written'} and said['state'] == 'written'
            and type(said['bytes_written']) is int and said['bytes_written'] == size):
        return push.WRITTEN
    return push.UNCERTAIN


class HubPush(object):
    """Delivery of hub push notifications to Claude Code sessions, for kitlib.hubsite.push."""

    def __init__(self, settings, ctime=None, writer=None):
        from kitlib.proc import process
        self.settings = settings
        self._ctime = ctime or process.proc_ctime
        self._write = writer or write_once

    def __repr__(self):
        return '<HubPush>'

    def _sessions(self):
        try:
            data = state.read_sessions(self.settings)
        except LaunchError:
            return {}
        return data if isinstance(data, dict) else {}

    def _name(self, recipient):
        """The session name a recipient key stands for: its sessions.json name, else itself."""
        entry = self._sessions().get(recipient)
        if isinstance(entry, dict) and isinstance(entry.get('name'), str) and entry['name']:
            return entry['name']
        return recipient

    def _pm_session_id(self):
        try:
            data = state.read_coordinators(self.settings)
        except LaunchError:
            return None
        sid = (data or {}).get('pm_session_id') if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid or sid.startswith('<'):
            return None                     # unset, or the example file's placeholder
        return sid

    def is_pm(self, recipient):
        return recipient == push.PM_KEY or self._name(recipient) in PM_NAMES

    def _live(self):
        return [(d, r) for d, r in pipes.records(pipes.registry_dirs(self.settings))
                if _usable(r) and pipes.is_live(r, self._ctime)]

    def _pick(self, hits, name):
        """One of several live records: pipes.json's when its pid and creation time match one,
        else the newest creation time, ambiguous."""
        if len(hits) == 1:
            return Target(hits[0][0], hits[0][1], name, False)
        hint = pipes.read(self.settings).get(name)
        if isinstance(hint, dict):
            for d, r in hits:
                if r.get('pid') == hint.get('pid') and pipes.proc_start(r) == hint.get('proc_start'):
                    return Target(d, r, name, False)
        d, r = max(hits, key=lambda h: int(pipes.proc_start(h[1])))
        return Target(d, r, name, True)

    def resolve(self, recipient):
        live = self._live()
        if recipient == push.PM_KEY:
            sid = self._pm_session_id()
            hits = [h for h in live if sid is not None and h[1].get('sessionId') == sid]
            if hits:
                return self._pick(hits, hits[0][1].get('name') or PM_NAMES[0])
            for name in PM_NAMES:
                hits = [h for h in live if h[1].get('name') == name]
                if hits:
                    return self._pick(hits, name)
            return None
        name = self._name(recipient)
        hits = [h for h in live if h[1].get('name') == name]
        return self._pick(hits, name) if hits else None

    def deliver(self, recipient, text):
        target = self.resolve(recipient)
        if target is None:
            return push.Sent(push.NO_SESSION, None if recipient == push.PM_KEY else self._name(recipient),
                             None, False)
        rec = target.record
        session = rec.get('sessionId') if isinstance(rec.get('sessionId'), str) else None
        try:
            packet = build_packet(read_token(target.folder, rec), source_pipe(), text)
        except ValueError:
            return push.Sent(push.NOT_WRITTEN, target.name, session, target.ambiguous)
        try:
            said = self._write(rec['messagingSocketPath'], rec['pid'], int(pipes.proc_start(rec)), packet)
        finally:
            packet = None                   # the token goes no further
        return push.Sent(said, target.name, session, target.ambiguous)
