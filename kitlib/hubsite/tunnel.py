"""kitlib.hubsite.tunnel: the listener a tunnel program points at, and its gate.

  start(settings, api, static_dir=None, log=None, keys=None, now=None) -> httpd.Listener
        the tunnel listener on 127.0.0.1:settings.tunnel_port, bound and not started.
        TunnelConfigError when settings.access cannot serve; httpd.BindRefused when the port
        cannot be bound.
  check(access_settings)        None when the Access settings can serve, else TunnelConfigError
  gate(access_settings, keys, now=None, log=None, clock=None) -> the listener's gate
  TunnelConfigError             .unset: nothing of hub.access is set at all
  HEADER                        the one header the Access token is read from

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
(`expired`, `bad-signature`, `keys-unavailable`, ...), NO_TOKEN, or GATE_ERROR for anything else
that went wrong in the gate (then with the exception's type). `(detail)` follows when access
gives one: its details are fixed words and numbers. The level is 'error' when the fault is this
server's (its keys, its clock, its settings, the gate itself) and 'note' when it is the token's.
One line a reason every SAY_EVERY_S seconds: a port open to the internet through a tunnel is
asked by strangers all day, and a line a request would bury the console. The next line of that
reason says how many were refused in between (MORE). No line holds a token, a header, a claim or
a value of the settings.

The listener differs from the local one in this and no more: its gate; no Host rule (the tunnel
hands on the public host name); writes need an `https` Origin that names that Host; no pairing
routes, as this port hands out no local session. start() installs no route at all:
kitlib.hubsite.serve gives it its health route.

The limits. access.KeyCache has no close: the thread of a key fetch in flight (a daemon) may
outlive the listener by up to access.FETCH_DEADLINE_S, and a request waiting on that fetch is
answered only when it ends. The gate does not tell whether the keys can be fetched until a
token arrives: start() makes the key source and asks it nothing.
"""
import threading
import time

from . import access, auth, httpd

HEADER = 'Cf-Access-Jwt-Assertion'
ORIGIN_SCHEME = 'https'                 # a browser reaches the tunnel's public name over https
REFUSED = 'this listener needs a valid Cloudflare Access token'
NO_TOKEN = 'no-token'
GATE_ERROR = 'gate-error'
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


def gate(access_settings, keys, now=None, log=None, clock=None):
    """The tunnel listener's gate (the module docstring): auth.OWNER for a request whose HEADER
    holds a valid Access token for `access_settings`, else the one refusal. `keys` has get(kid)
    (an access.KeyCache); `now()` is the Unix time a token's times are checked against and
    `clock()` the one the log lines are spaced by (time.time and time.monotonic). Whatever
    goes wrong in here is the refusal, never a pass and never an exception."""
    team, aud, emails = access_settings.team, access_settings.aud, access_settings.emails
    now = now or time.time
    lines = _Lines(log, clock or time.monotonic)

    def check_request(request):
        reason, detail = NO_TOKEN, ''
        try:
            token = request.header(HEADER)      # absent, empty or sent twice: no token
            if token:
                access.verify(token, keys, team, aud, emails, now())
                return auth.OWNER
        except access.AccessError as e:
            reason, detail = e.reason, e.detail
        except Exception as e:
            reason, detail = GATE_ERROR, type(e).__name__
        lines.say(reason, detail)
        return httpd.refusal(403, 'forbidden', REFUSED, 'access')
    return check_request


def start(settings, api, static_dir=None, log=None, keys=None, now=None):
    """The tunnel listener for `settings`: an httpd.Listener on the loopback address at
    settings.tunnel_port with gate(), bound and not started, with no route of its own.
    TunnelConfigError, before any port is bound or key source made, when settings.access cannot
    serve; httpd.BindRefused when the port cannot be bound. `keys(team)` makes the key source
    (None: access.KeyCache, which fetches nothing until a token asks for a key); `now` is the
    gate's."""
    check(settings.access)
    source = (keys or access.KeyCache)(settings.access.team)
    return httpd.Listener((httpd.LOOPBACK, settings.tunnel_port), api,
                          gate(settings.access, source, now=now, log=log),
                          static_dir=static_dir, host_rule=None, origin_scheme=ORIGIN_SCHEME,
                          log=log)
