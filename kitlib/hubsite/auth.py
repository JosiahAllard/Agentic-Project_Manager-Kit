"""kitlib.hubsite.auth: who a request on the LOCAL listener is.

  folder(home=None)                 <machine config dir>/hub, where the token files live
  ensure_tokens(folder)             make the folder and the two token files when absent
  read_token(folder, role)          a role's token as a client sends it, or None
  parse_cookies(text)               a Cookie header as {name: value}
  cookie_header(value)              the Set-Cookie value of a paired browser
  Credentials(folder)               .role_for(headers, cookies) -> OWNER, AGENT or None
                                    .new_pairing_code() / .pair(code) for the PC's browser

Two roles, each a token file: OWNER_FILE and AGENT_FILE, sent as `Authorization: Bearer <token>`.
A browser cannot send that header from a link, so the owner's browser pairs once: an owner-only
caller asks for a pairing code, the browser redeems it once, and gets a session cookie
(HttpOnly, SameSite=Strict). Codes and sessions live in this process's memory only: a server
restart ends every session, and the owner pairs again.

Every comparison of a secret is hmac.compare_digest. Whatever cannot be checked is no role:
an absent, short or unreadable token file, two token files holding the same token, a second
Authorization header, a scheme other than Bearer. An Authorization header that does not resolve
is never rescued by a cookie.

This module is the local listener's only. It never reads the `Cf-Access-Jwt-Assertion` header:
on the local port that header is client-supplied and means nothing. It opens no socket.
"""
import collections
import hmac
import os
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
MIN_TOKEN = 32               # characters; a shorter token file grants nothing
MAX_TOKEN = 512
PAIR_TTL_S = 300             # a pairing code not redeemed by then is gone
SESSION_TTL_S = 30 * 86400
MAX_CODES = 8                # outstanding pairing codes; the oldest goes first
MAX_SESSIONS = 32


def folder(home=None):
    """The credential folder: FOLDER_NAME in the machine config folder writers use
    (kitlib.config.user.DIR_NAME), under `home` (default the user's). Never the retired folder."""
    return os.path.join(os.path.dirname(user.locate(home).new), FOLDER_NAME)


def ensure_tokens(folder):
    """Make `folder` and each token file that is absent, with a fresh random token, and return
    {role: path}. A file that exists is left as it is, whatever it holds. OSError propagates."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for role in ROLES:
        path = os.path.join(folder, TOKEN_FILES[role])
        try:
            with open(path, 'x', encoding='ascii', newline='\n') as f:
                f.write(secrets.token_urlsafe(32) + '\n')
        except FileExistsError:
            pass
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
    """True when the two strings are equal, compared in constant time; False for a non-string."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


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


def cookie_header(value):
    """The Set-Cookie header value that gives a browser the session `value` (Credentials.pair)."""
    return '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict' % (COOKIE, value, SESSION_TTL_S)


class Credentials(object):
    """The local listener's credentials: the token files in `folder`, read at every request (a
    replaced token file takes effect at once), and this process's pairing codes and sessions.
    `clock` is a monotonic clock in seconds. Safe to call from several threads."""

    def __init__(self, folder, clock=time.monotonic):
        self.folder = folder
        self._clock = clock
        self._lock = threading.Lock()
        self._codes = collections.OrderedDict()       # code -> expiry
        self._sessions = collections.OrderedDict()    # session value -> expiry

    def role_for(self, headers, cookies=None):
        """OWNER, AGENT or None for a request with these headers and cookies ({name: value}).
        A Bearer token decides alone when an Authorization header is present; else the session
        cookie may give OWNER. No other header is read."""
        present, token = _bearer(headers)
        if present:
            return self._token_role(token) if token else None
        value = cookies.get(COOKIE) if cookies else None
        if isinstance(value, str) and value and self._live(self._sessions, value):
            return OWNER
        return None

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
        """Redeem `code`: the session value to send as cookie_header(value), or None when the
        code is unknown, expired or already redeemed. A code works once."""
        if not isinstance(code, str) or not code or not self._live(self._codes, code, take=True):
            return None
        return self._add(self._sessions, SESSION_TTL_S, MAX_SESSIONS)
