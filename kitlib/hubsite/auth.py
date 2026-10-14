"""kitlib.hubsite.auth: who a request on the LOCAL listener is.

  folder(home=None)                 <machine config dir>/hub, where the token files live
  ensure_tokens(folder)             make the folder and the two token files when absent;
                                    TokenFileError for one that exists and cannot serve
  read_token(folder, role)          a role's token as a client sends it, or None
  parse_cookies(text)               a Cookie header as {name: value}
  cookie_name(port)                 the session cookie's name on that listener
  cookie_header(value, port)        the Set-Cookie value of a paired browser
  sessions_file(port)               the name of the listener's sessions file (SESSIONS_FILE)
  Credentials(folder, port=port, persist=False, log=None)
                                    .role_for(headers, cookies) -> OWNER, AGENT or None
                                    .resolve(headers, cookies) -> (role, renewed session or None)
                                    .new_pairing_code() / .pair(code) for the PC's browser
                                    .unpair_all() ends every browser session; .notes

Two roles, each a token file: OWNER_FILE and AGENT_FILE, sent as `Authorization: Bearer <token>`.
A browser cannot send that header from a link, so the owner's browser pairs once: an owner-only
caller asks for a pairing code, the browser redeems it once, and gets a session cookie
(HttpOnly, SameSite=Strict). Browsers send this cookie to any server on the loopback address
whatever its port; that limit is accepted.

Sessions. A session lives SESSION_TTL_S on the wall clock. `resolve` renews one that is used with
less than half of that left: its expiry moves to a full SESSION_TTL_S from now and the caller
sends the cookie again (cookie_header) so the browser's copy moves too; `role_for` never renews.
A session ends with its lifetime, with unpair_all, and with a new owner token: a session is held
as a SHA-256 HMAC (key=the owner token, msg=the cookie value), never as the value itself, and
a cookie is looked up under the owner token read at that request, so replacing owner.token
ends every browser at once. No readable owner token, or one token in both files, is no cookie role
and no pairing. Pairing codes live in this process's memory only.

With persist=True (a listener's port is needed) the sessions are also kept in
sessions_file(port) beside the token files, so a server restart keeps the browser paired:
{"v": 1, "sessions": [{"mac": <64 lowercase hex>, "exp": <unix seconds>}, ...]}. Reading the file
gives nothing usable (a preimage of a random value) and writing an entry that matches needs the
owner token, which already makes the writer the owner. It is read once, when the Credentials is
made. An expired entry is dropped. A file that cannot be read loads no session, a bad entry is
skipped, an expiry past one lifetime from the load is held to that, and only the MAX_SESSIONS
latest-ending are kept; each of those four cases is one line in `.notes`. The file is never
deleted (the next write replaces it). It is written whole at a pairing, a renewal and
unpair_all: a temp file beside it, then os.replace. A write that fails leaves the session good in
this process and logs one `error` line saying it will not survive a restart. A clock stepped back
lengthens a session by the step; that is accepted.

Every comparison of a secret is hmac.compare_digest. Whatever cannot be checked is no role:
an absent, short or unreadable token file, two token files holding the same token, a second
Authorization header, a scheme other than Bearer. An Authorization header that does not resolve
is never rescued by a cookie.

This module is the local listener's only. It never reads the `Cf-Access-Jwt-Assertion` header:
on the local port that header is client-supplied and means nothing. It opens no socket.
"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from kitlib.config import user

OWNER = 'owner'
AGENT = 'agent'
ROLES = (OWNER, AGENT)
FOLDER_NAME = 'hub'
OWNER_FILE = 'owner.token'
AGENT_FILE = 'agent.token'
TOKEN_FILES = {OWNER: OWNER_FILE, AGENT: AGENT_FILE}
COOKIE = 'hub_session'
TOKEN_BYTES = 32             # the random bytes of a generated token
MIN_TOKEN = 43               # characters, a generated token's length; a shorter file grants nothing
MAX_TOKEN = 512
PAIR_TTL_S = 300             # a pairing code not redeemed by then is gone
SESSION_TTL_S = 30 * 86400   # a session's life: the server-side expiry and the cookie's Max-Age
MAX_CODES = 8                # outstanding pairing codes; the oldest goes first
MAX_SESSIONS = 32
SESSIONS_FILE = 'browser-sessions-%d.json'   # % the listener's port, beside the token files
SESSIONS_VERSION = 1
MAX_SESSIONS_BYTES = 1 << 16                 # a sessions file past this is not read

_MAC = re.compile(r'[0-9a-f]{64}\Z')


class TokenFileError(Exception):
    """A token file exists and cannot serve as a credential. The message names the file."""


def folder(home=None):
    """The credential folder: FOLDER_NAME in the machine config folder writers use
    (kitlib.config.user.DIR_NAME), under `home` (default the user's). Never the retired folder."""
    return os.path.join(os.path.dirname(user.locate(home).new), FOLDER_NAME)


def cookie_name(port=None):
    """The session cookie's name on the listener at `port`: COOKIE_<port>, so two hubs on one
    host (cookies are not split by port) do not overwrite each other. None: COOKIE."""
    return COOKIE if port is None else '%s_%d' % (COOKIE, port)


def sessions_file(port):
    """The name of the sessions file of the listener at `port`, in the token folder."""
    return SESSIONS_FILE % port


def ensure_tokens(folder):
    """Make `folder` and each token file that is absent, with a fresh random token, and return
    {role: path}. A token file that exists and read_token cannot use (empty, shorter than
    MIN_TOKEN, not ASCII, a directory) is a TokenFileError naming it: it is never replaced and
    never adopted. A new file is written whole beside its place and renamed in, so a crash
    leaves no empty token file; the rename never replaces a file another process made first
    (that process's token is then the one in use). A hard kill can leave a `<file>.<hex>.tmp`
    beside it: harmless, never read as a token, and nothing sweeps it. OSError propagates; a
    failure to remove the temp file never hides it."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for role in ROLES:
        path = os.path.join(folder, TOKEN_FILES[role])
        if not os.path.lexists(path):
            tmp = '%s.%s.tmp' % (path, secrets.token_hex(8))
            try:
                with open(tmp, 'x', encoding='ascii', newline='\n') as f:
                    f.write(secrets.token_urlsafe(TOKEN_BYTES) + '\n')
                os.rename(tmp, path)       # Windows: fails when `path` exists, never replaces
            except FileExistsError:
                pass
            finally:
                try:
                    os.remove(tmp)         # gone already after a rename that worked
                except OSError:
                    pass
        if read_token(folder, role) is None:
            raise TokenFileError(
                'the hub token file %s holds no usable token (one line of %d to %d ASCII '
                'characters, no spaces): delete it to have a new one made, or put a token in it'
                % (path, MIN_TOKEN, MAX_TOKEN))
        out[role] = path
    return out


def read_token(folder, role):
    """The token in `role`'s file under `folder`, stripped, or None when the file is absent,
    unreadable, not ASCII, or holds fewer than MIN_TOKEN or more than MAX_TOKEN characters."""
    try:
        with open(os.path.join(folder, TOKEN_FILES[role]), 'rb') as f:
            raw = f.read(MAX_TOKEN + 64)
    except (OSError, KeyError):
        return None
    try:
        token = raw.decode('ascii').strip()
    except UnicodeDecodeError:
        return None
    if not MIN_TOKEN <= len(token) <= MAX_TOKEN or any(c.isspace() for c in token):
        return None
    return token


def _same(a, b):
    """True when the two strings are equal, compared in constant time; False for a non-string
    and for a string that has no UTF-8 form (a lone surrogate), which no secret here is."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    try:
        a, b = a.encode('utf-8'), b.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(a, b)


def _values(headers, name):
    """Every value of the header `name` in `headers` (a mapping or an http.client message),
    names compared case-blind."""
    if not headers:
        return []
    want = name.lower()
    return [v for k, v in headers.items() if isinstance(k, str) and k.lower() == want]


def _bearer(headers):
    """(present, token): whether an Authorization header was sent at all, and its Bearer token,
    None when the header is doubled, not a string, not Bearer, or empty."""
    vals = _values(headers, 'authorization')
    if not vals:
        return False, None
    if len(vals) != 1 or not isinstance(vals[0], str):
        return True, None
    scheme, _, token = vals[0].strip().partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token:
        return True, None
    return True, token


def parse_cookies(text):
    """A Cookie header's `name=value; name=value` as a dict; the first of a repeated name wins;
    a part without `=` is skipped. Values are not unquoted: the hub's own cookie needs none."""
    out = {}
    for part in (text or '').split(';'):
        name, eq, value = part.partition('=')
        name = name.strip()
        if eq and name and name not in out:
            out[name] = value.strip()
    return out


def cookie_header(value, port=None):
    """The Set-Cookie header value that gives a browser the session `value` (Credentials.pair)
    on the listener at `port` (cookie_name)."""
    return '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict' % (
        cookie_name(port), value, SESSION_TTL_S)


def _mac(key, value):
    """A session's stored form: the SHA-256 HMAC of `value` under the owner token `key`, as 64
    lowercase hex digits; None for a value with no UTF-8 form."""
    try:
        msg = value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return hmac.new(key.encode('ascii'), msg, hashlib.sha256).hexdigest()


class Credentials(object):
    """The local listener's credentials: the token files in `folder`, read at every request (a
    replaced token file takes effect at once), this process's pairing codes, and the browser
    sessions (module docstring, "Sessions"). `clock` is the wall clock in seconds. `port` is the
    listener's: `.cookie` is cookie_name(port), the one cookie role_for reads. `persist` keeps
    the sessions in sessions_file(port) in `folder` (ValueError without an int port). `log(level,
    text)` gets the line of a sessions write that failed. `.notes` holds the lines the load of
    the sessions file left, for the console. Safe to call from several threads."""

    def __init__(self, folder, clock=time.time, port=None, persist=False, log=None):
        if persist and (isinstance(port, bool) or not isinstance(port, int)):
            raise ValueError('a Credentials that keeps its sessions needs the listener port, '
                             'not %r' % (port,))
        self.folder = folder
        self.port = port
        self.cookie = cookie_name(port)
        self.notes = []
        self.path = os.path.join(folder, sessions_file(port)) if persist else None
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._codes = collections.OrderedDict()       # code -> expiry
        self._sessions = collections.OrderedDict()    # _mac(owner token, value) -> expiry
        if self.path is not None:
            self._load()

    def role_for(self, headers, cookies=None):
        """OWNER, AGENT or None for a request with these headers and cookies ({name: value}).
        A Bearer token decides alone when an Authorization header is present; else the session
        cookie may give OWNER. No other header is read. Never renews a session (resolve)."""
        return self._resolve(headers, cookies, False)[0]

    def resolve(self, headers, cookies=None):
        """(role, renewed): role_for's role, and the session value when this request's cookie
        renewed its session (less than half of SESSION_TTL_S was left), else None. The caller
        sends cookie_header(renewed, self.port) with its answer."""
        return self._resolve(headers, cookies, True)

    def _resolve(self, headers, cookies, renew):
        present, token = _bearer(headers)
        if present:
            return (self._token_role(token) if token else None), None
        value = cookies.get(self.cookie) if cookies else None
        if not isinstance(value, str) or not value:
            return None, None
        key = self._owner_key()
        mac = _mac(key, value) if key is not None else None
        if mac is None:
            return None, None
        now = self._clock()
        with self._lock:
            self._expire(now)
            found = None
            for k in self._sessions:
                if hmac.compare_digest(k, mac):
                    found = k
            if found is None:
                return None, None
            if not renew or self._sessions[found] - now >= SESSION_TTL_S / 2:
                return OWNER, None
            self._sessions[found] = int(now + SESSION_TTL_S)
            self._sessions.move_to_end(found)
            self._save()
        return OWNER, value

    def _owner_key(self):
        """The owner token sessions are bound to, or None when it is unreadable or the agent's
        file holds the same token (the agent could then make a session of its own)."""
        owner, agent = read_token(self.folder, OWNER), read_token(self.folder, AGENT)
        if owner is None or (agent is not None and _same(owner, agent)):
            return None
        return owner

    def _token_role(self, token):
        owner, agent = read_token(self.folder, OWNER), read_token(self.folder, AGENT)
        is_owner, is_agent = _same(token, owner), _same(token, agent)
        if owner is not None and agent is not None and _same(owner, agent):
            return None            # one token for both roles: the agent would be the owner
        if is_owner:
            return OWNER
        return AGENT if is_agent else None

    def _live(self, table, value, take=False):
        """True when `value` is an unexpired key of `table`; with `take` it is removed, so a
        second look fails. Expired entries are dropped on every look."""
        now = self._clock()
        with self._lock:
            for k in [k for k, exp in table.items() if exp <= now]:
                del table[k]
            found = None
            for k in table:
                if _same(k, value):
                    found = k
            if found is not None and take:
                del table[found]
            return found is not None

    def _add(self, table, ttl, cap):
        value = secrets.token_urlsafe(32)
        with self._lock:
            table[value] = self._clock() + ttl
            while len(table) > cap:
                table.popitem(last=False)
        return value

    def new_pairing_code(self):
        """A fresh one-time pairing code, good for PAIR_TTL_S. The caller has already checked
        that the request is the owner's: whoever holds a code becomes the owner's browser."""
        return self._add(self._codes, PAIR_TTL_S, MAX_CODES)

    def pair(self, code):
        """Redeem `code`: the session value to send as cookie_header(value, self.port), or None
        when the code is unknown, expired or already redeemed, or no owner token can bind the
        session (_owner_key). A code works once. Past MAX_SESSIONS the session renewed or made
        longest ago ends."""
        if not isinstance(code, str) or not code or not self._live(self._codes, code, take=True):
            return None
        key = self._owner_key()
        if key is None:
            return None
        value = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._expire(now)
            self._sessions[_mac(key, value)] = int(now + SESSION_TTL_S)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            self._save()
        return value

    def unpair_all(self):
        """End every browser session and every pairing code not yet redeemed, here and, with
        persist, in the sessions file."""
        with self._lock:
            self._codes.clear()
            self._sessions.clear()
            self._save()

    # ---- the sessions file (persist) ----------------------------------------------------------

    def _expire(self, now):
        """Drop the expired sessions. Under self._lock."""
        for k in [k for k, exp in self._sessions.items() if exp <= now]:
            del self._sessions[k]

    def _say(self, level, text):
        if self._log is None:
            return
        try:
            self._log(level, text)
        except Exception:           # noqa: BLE001 - a log that fails must not fail a request
            pass

    def _save(self):
        """Write the sessions to self.path whole, through a temp file and os.replace. Under
        self._lock, so the file follows the order of the changes. A failure is logged, never
        raised: the sessions stay good in this process."""
        if self.path is None:
            return
        body = {'v': SESSIONS_VERSION,
                'sessions': [{'mac': k, 'exp': exp} for k, exp in self._sessions.items()]}
        tmp = '%s.%s.tmp' % (self.path, secrets.token_hex(8))
        try:
            with open(tmp, 'x', encoding='ascii', newline='\n') as f:
                f.write(json.dumps(body, separators=(',', ':')) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            self._say('error', 'the browser sessions could not be written to %s (%s): a browser '
                      'paired or renewed now will not survive a restart of the hub server'
                      % (self.path, e))
        finally:
            try:
                os.remove(tmp)         # gone already after a replace that worked
            except OSError:
                pass

    def _load(self):
        """Read self.path into the sessions (module docstring, "Sessions"). Never raises and
        never deletes the file; what it skips is a line in self.notes."""
        path = self.path
        try:
            with open(path, 'rb') as f:
                raw = f.read(MAX_SESSIONS_BYTES + 1)
        except FileNotFoundError:
            return
        except OSError as e:
            self.notes.append('the browser sessions file %s could not be read (%s): no browser '
                              'is paired from before; the next pairing replaces it'
                              % (path, e.strerror or e))
            return
        data = None
        if len(raw) <= MAX_SESSIONS_BYTES:
            try:
                data = json.loads(raw.decode('ascii'))
            except (UnicodeDecodeError, ValueError, RecursionError):
                data = None
        entries = None
        if isinstance(data, dict) and type(data.get('v')) is int \
                and data.get('v') == SESSIONS_VERSION:
            entries = data.get('sessions')
        if not isinstance(entries, list):
            self.notes.append('the browser sessions file %s is not one this hub reads (version '
                              '%d JSON of at most %d bytes): no browser is paired from before; '
                              'the next pairing replaces it'
                              % (path, SESSIONS_VERSION, MAX_SESSIONS_BYTES))
            return
        now = self._clock()
        limit = int(now + SESSION_TTL_S)
        kept, bad, held = [], 0, 0
        for entry in entries:
            mac = entry.get('mac') if isinstance(entry, dict) else None
            exp = entry.get('exp') if isinstance(entry, dict) else None
            if not isinstance(mac, str) or not _MAC.match(mac) or type(exp) is not int:
                bad += 1
                continue
            if exp <= now:
                continue
            if exp > limit:
                exp, held = limit, held + 1
            kept.append((mac, exp))
        if bad:
            self.notes.append('the browser sessions file %s has %d entr%s this hub cannot read: '
                              'skipped' % (path, bad, 'y' if bad == 1 else 'ies'))
        if held:
            self.notes.append('the browser sessions file %s has %d session%s ending more than '
                              '%d days from now: each held to that' % (
                                  path, held, '' if held == 1 else 's', SESSION_TTL_S // 86400))
        if len(kept) > MAX_SESSIONS:
            latest = sorted(range(len(kept)), key=lambda i: kept[i][1], reverse=True)
            self.notes.append('the browser sessions file %s holds %d sessions: the %d ending '
                              'latest are kept' % (path, len(kept), MAX_SESSIONS))
            kept = [kept[i] for i in sorted(latest[:MAX_SESSIONS])]
        with self._lock:
            for mac, exp in kept:
                self._sessions[mac] = exp
