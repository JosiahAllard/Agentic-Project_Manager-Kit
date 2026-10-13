"""kitlib.hubsite.tunnel: the listener a tunnel program points at, and its gate.

  start(settings, api, static_dir=None, log=None, keys=None, now=None) -> httpd.Listener
        the tunnel listener on 127.0.0.1:settings.tunnel_port, bound and not started.
        TunnelConfigError when settings.access cannot serve; httpd.BindRefused when the port
        cannot be bound.
  check(access_settings)        None when the Access settings can serve, else TunnelConfigError
  gate(access_settings, keys, now=None, log=None, clock=None) -> the listener's Gate
  Gate      (request) -> auth.OWNER or the one refusal
            .stop()             shut it for good: every request from now on is refused
            .keys()             (True, '') when there are keys to check a token with, else
                                (False, one of access.REASONS or '')
  TunnelConfigError             .unset: nothing of hub.access is set at all
  HEADER                        the one header the Access token is read from
  VIA                           the source a write through this listener is recorded with

Nothing here runs, installs or configures a tunnel program. The kit listens on the loopback
address for one; whoever runs it points it at this port.

Who may pass. A request passes the gate only with a valid Cloudflare Access token
(kitlib.hubsite.access.verify: this team's signature, this audience, not expired, an email of the
allow-list) in HEADER, sent exactly once. There is no exception: not for a path, a method, a
static file or GET health, since httpd asks the gate before it reads any of them. A valid token
makes the caller the owner. Nothing else is read: a local token, a session cookie, a token in
the query or in a cookie are all no credential here, and a local token beside a valid Access
token changes nothing.

What a refused caller sees. One answer for every reason: 403 with the Api's error shape, code
`forbidden`, field `access`, the message REFUSED. It says nothing of why, sets no header, and is
the same for a missing token, a forged one and a key source that cannot be reached, so a
stranger learns nothing from it.

What the log says. `log('note' | 'error', REFUSAL_LINE % reason)` with access's reason
(`expired`, `bad-signature`, `keys-unavailable`, ...), NO_TOKEN, CLOSING for a request refused
because the gate was stopped, or GATE_ERROR for anything else that went wrong in the gate (then
with the exception's type). `(detail)` follows when access
gives one: its details are fixed words and numbers. The level is 'error' when the fault is this
server's (its keys, its clock, its settings, the gate itself) and 'note' when it is the token's.
One line a reason every SAY_EVERY_S seconds: a port open to the internet through a tunnel is
asked by strangers all day, and a line a request would bury the console. The next line of that
reason says how many were refused in between (MORE). No line holds a token, a header, a claim or
a value of the settings.

The listener differs from the local one in this and no more: its gate; no Host rule (the tunnel
hands on the public host name); writes need an `https` Origin that names that Host; no pairing
routes, as this port hands out no local session; the source of a write; and the wording of an
error. Every write through it is recorded with the source VIA: the listener is made with
via_header=False, so the X-Hub-Via header a client sends on this port is never read, and VIA is
httpd.TUNNEL_VIA, the one source the local listener never records for a client that claims it.
An error answer through it is worded as for an agent (the listener is made with
agent_words=True): the valid token's caller is the owner for what a request may do, and is still
told the fixed phrase of api.AGENT_SAYS, never the store's folder or a file path, since this
port's answers leave the machine. start() installs no route at all: kitlib.hubsite.serve gives
it its health route, which answers fixed words only on this port.

Closing. Gate.stop() shuts the gate for good and stops its key source (access.KeyCache.stop), so
a request held in the gate on a key fetch is refused with the one answer at once and no longer
counts as in flight; kitlib.hubsite.serve.Hub.close calls it before anything else. A stop admits
nobody: a token whose check ended after the stop is refused too, and so is every later request.

The limits. A stop cannot end the thread of a key fetch that is running (a daemon, named
hub-access-keys): it ends when its fetcher does (the docstring of access.fetch_keys says how
long that can take), and its answer is dropped. So that one thread may outlive the listener and
Hub.close. The gate does not fetch keys until a token arrives: start() makes
the key source and asks it nothing, and Gate.keys() says "no keys" until the first request that
carried a token made a good fetch.
"""
import threading
import time

from . import access, auth, httpd

HEADER = 'Cf-Access-Jwt-Assertion'
ORIGIN_SCHEME = 'https'                 # a browser reaches the tunnel's public name over https
REFUSED = 'this listener needs a valid Cloudflare Access token'
VIA = httpd.TUNNEL_VIA                  # the source every write through this listener is recorded with
NO_TOKEN = 'no-token'
GATE_ERROR = 'gate-error'
CLOSING = 'closing'                     # refused because Gate.stop() was called: the hub is closing
SAY_EVERY_S = 60                        # one log line a reason in this many seconds
REFUSAL_LINE = 'the tunnel listener refused a request: %s'
MORE = '; %d more were refused for this reason since the last line'

# why the listener does not start: fixed words, for the console and for GET health. None holds
# a value of the settings.
UNSET = 'no Access settings are set (hub.access)'
NO_TEAM = 'no Access team name is set (hub.access.team)'
NO_AUD = 'no Access audience tag is set (hub.access.aud)'
NO_EMAILS = 'no allowed email is set (hub.access.emails)'
BAD_TEAM = 'the Access team name is not one DNS label (hub.access.team)'
BAD_EMAILS = 'the allowed emails are not a list of addresses (hub.access.emails)'
BAD_ACCESS = 'the Access settings cannot be used (hub.access)'

_CONFIG = {'bad-team': BAD_TEAM, 'no-audience': NO_AUD, 'bad-allow-list': BAD_EMAILS,
           'no-allowed-emails': NO_EMAILS}
_PROBE = 'malformed'                    # access.verify's reason for a token that is no text
# reasons that are a fault of this server, not of the token: an 'error' line
_OURS = frozenset(_CONFIG) | {'bad-clock', 'bad-key', GATE_ERROR}
_OURS_PREFIX = 'keys-'


class TunnelConfigError(Exception):
    """The Access settings cannot serve the tunnel listener; str() is the fixed phrase. `unset`
    is True when nothing of hub.access is set, which is how a hub with no tunnel runs."""

    def __init__(self, text, unset=False):
        Exception.__init__(self, text)
        self.unset = unset


def check(access_settings):
    """None when `access_settings` (settings.Access) can serve; TunnelConfigError otherwise.
    What is missing is named first. What is set is then put to access.verify itself, with no
    token: only when its own checks of the team, the audience and the allow-list pass does it
    get as far as the token, so this never calls good what verify would refuse on every
    request."""
    team, aud, emails = access_settings.team, access_settings.aud, access_settings.emails
    if not team and not aud and not emails:
        raise TunnelConfigError(UNSET, unset=True)
    for value, phrase in ((team, NO_TEAM), (aud, NO_AUD), (emails, NO_EMAILS)):
        if not value:
            raise TunnelConfigError(phrase)
    try:
        access.verify(None, {}, team, aud, emails, 0)
    except access.AccessError as e:
        if e.reason != _PROBE:
            raise TunnelConfigError(_CONFIG.get(e.reason, BAD_ACCESS)) from None
        return None
    raise TunnelConfigError(BAD_ACCESS)     # verify let no token pass: never call that good


class _Lines(object):
    """The gate's log lines: one a reason every SAY_EVERY_S on `clock`, with the count of the
    refusals in between. say() never raises."""

    def __init__(self, log, clock):
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()
        self._said = {}                 # reason -> [clock time of its last line, refusals since]

    def say(self, reason, detail):
        if self._log is None:
            return
        try:
            with self._lock:
                now = self._clock()
                state = self._said.get(reason)
                if state is not None and 0 <= now - state[0] < SAY_EVERY_S:
                    state[1] += 1
                    return
                skipped = state[1] if state is not None else 0
                self._said[reason] = [now, 0]
            text = REFUSAL_LINE % reason
            if detail:
                text += ' (%s)' % detail
            if skipped:
                text += MORE % skipped
            ours = reason in _OURS or reason.startswith(_OURS_PREFIX)
            self._log('error' if ours else 'note', text)
        except Exception:               # a console that is gone must not open or break the gate
            pass


class Gate(object):
    """The tunnel listener's gate (the module docstring), as gate() makes it. Called with a
    request it answers auth.OWNER when HEADER holds a valid Access token, else the one refusal.
    Whatever goes wrong in a call is the refusal, never a pass and never an exception."""

    def __init__(self, access_settings, keys, now=None, log=None, clock=None):
        self._team, self._aud = access_settings.team, access_settings.aud
        self._emails = access_settings.emails
        self._keys = keys
        self._now = now or time.time
        self._lines = _Lines(log, clock or time.monotonic)
        self._stopped = threading.Event()

    def __call__(self, request):
        reason, detail = NO_TOKEN, ''
        try:
            if not self._stopped.is_set():
                token = request.header(HEADER)      # absent, empty or sent twice: no token
                if token:
                    access.verify(token, self._keys, self._team, self._aud, self._emails,
                                  self._now())
                    # a stop() that came while the token was being checked refuses it too
                    if not self._stopped.is_set():
                        return auth.OWNER
        except access.AccessError as e:
            reason, detail = e.reason, e.detail
        except Exception as e:
            reason, detail = GATE_ERROR, type(e).__name__
        if self._stopped.is_set():
            reason, detail = CLOSING, ''
        self._lines.say(reason, detail)
        return httpd.refusal(403, 'forbidden', REFUSED, 'access')

    def stop(self):
        """Shut the gate for good (the listener is closing): True. From now on every request
        gets the one refusal, a valid token included, and a request held on a key fetch is
        refused at once when the key source has a stop() (access.KeyCache has). It waits for
        nothing and never raises."""
        self._stopped.set()
        try:
            stop = getattr(self._keys, 'stop', None)
            if callable(stop):
                stop()
        except Exception:               # the gate is shut whatever its key source does
            pass
        return True

    def keys(self):
        """(True, '') when the key source has keys to check a token with, else (False, why).
        `why` is one of access.REASONS (the key source's `error`, when it is one) or ''. Only a
        ready() that answers True says there are keys: a key source with no ready() (a dict)
        cannot say it has any, so it is (False, why) like one that has fetched none. It asks
        ready() and nothing else: no fetch is started and none is waited for. Never raises."""
        try:
            if self._stopped.is_set():
                return False, ''
            ready = getattr(self._keys, 'ready', None)
            if callable(ready) and ready() is True:
                return True, ''
            why = getattr(self._keys, 'error', '')
            return False, why if isinstance(why, str) and why in access.REASONS else ''
        except Exception:
            return False, ''


def gate(access_settings, keys, now=None, log=None, clock=None):
    """The tunnel listener's Gate for `access_settings`. `keys` has get(kid) (an access.KeyCache,
    which also has the stop() and ready() the Gate's own stop() and keys() use; with no ready()
    the Gate checks tokens all the same and keys() never says there are keys); `now()` is the
    Unix time a token's times are checked against and `clock()` the one the log lines are spaced
    by (time.time and time.monotonic)."""
    return Gate(access_settings, keys, now=now, log=log, clock=clock)


def start(settings, api, static_dir=None, log=None, keys=None, now=None):
    """The tunnel listener for `settings`: an httpd.Listener on the loopback address at
    settings.tunnel_port with gate(), bound and not started, with no route of its own. Its `via`
    is VIA and it reads no VIA_HEADER, so every write through it is recorded as VIA; it is made
    with agent_words=True, so an error answer through it is the agent's wording for the owner
    too. TunnelConfigError, before any port is bound or key source made, when settings.access
    cannot serve; httpd.BindRefused when the port cannot be bound. `keys(team)` makes the key
    source (None: access.KeyCache, which fetches nothing until a token asks for a key); `now` is
    the gate's."""
    check(settings.access)
    source = (keys or access.KeyCache)(settings.access.team)
    return httpd.Listener((httpd.LOOPBACK, settings.tunnel_port), api,
                          gate(settings.access, source, now=now, log=log),
                          static_dir=static_dir, host_rule=None, origin_scheme=ORIGIN_SCHEME,
                          via=VIA, via_header=False, agent_words=True, log=log)
